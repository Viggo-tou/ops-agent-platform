from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from unittest.mock import Mock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.core.enums import EventType  # noqa: E402
from app.agents.schemas import CodegenResult  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.llm_usage import LlmUsage  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.schemas.knowledge import KnowledgeCitation  # noqa: E402
from app.services.codegen import CodegenError, CodeGenerator  # noqa: E402
from app.services import llm_telemetry  # noqa: E402
from app.services.knowledge_synthesis import KnowledgeSynthesizer  # noqa: E402
from app.services.llm_telemetry import LlmCall, record_llm_call  # noqa: E402


@pytest.fixture()
def db_session() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _task(db: Session) -> Task:
    task = Task(
        id="task-1",
        actor_name="alice",
        title="Telemetry task",
        request_text="measure llm",
        scenario="process_question",
    )
    db.add(task)
    db.flush()
    return task


def _call(task_id: str | None = "task-1") -> LlmCall:
    return LlmCall(
        purpose="synthesis",
        provider="minimax",
        model="MiniMax-M2.7",
        input_tokens=100,
        output_tokens=25,
        latency_ms=321,
        success=True,
        prompt_fingerprint="abcdef1234",
        task_id=task_id,
        actor_name="alice",
    )


def _settings() -> Settings:
    return Settings(
        minimax_api_key="test-key",
        knowledge_synthesis_enabled=True,
        knowledge_synthesis_model="MiniMax-M2.7",
        knowledge_synthesis_timeout_seconds=3.0,
    )


def _citation() -> KnowledgeCitation:
    return KnowledgeCitation(
        document_id="doc-1",
        source_name="repo",
        title="a.py",
        relative_path="a.py",
        line_start=1,
        line_end=1,
        snippet="login handler",
        score=10,
        metadata={},
    )


def test_record_llm_call_writes_both_llm_usage_and_event(db_session: Session) -> None:
    _task(db_session)

    record_llm_call(db_session, _call())

    usage = db_session.scalars(select(LlmUsage)).one()
    event = db_session.scalars(select(Event)).one()
    assert usage.purpose == "synthesis"
    assert usage.provider_name == "minimax"
    assert usage.input_tokens == 100
    assert event.event_type == EventType.LLM_CALL
    assert event.payload_json["latency_ms"] == 321
    assert event.payload_json["provider"] == "minimax"


def test_record_llm_call_allows_taskless_events(db_session: Session) -> None:
    record_llm_call(db_session, _call(task_id=None))

    event = db_session.scalars(select(Event)).one()
    usage = db_session.scalars(select(LlmUsage)).one()
    assert event.task_id is None
    assert usage.task_id is None


def test_record_llm_call_swallows_db_errors_and_logs_warn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    db_session: Session,
) -> None:
    _task(db_session)
    llm_telemetry.reset_telemetry_failure_count_for_tests()

    def fail_record_usage(*args, **kwargs):  # noqa: ANN002,ANN003
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.cost_tracking.CostTracker.record_usage", fail_record_usage)

    with caplog.at_level(logging.WARNING, logger="app.services.llm_telemetry"):
        record_llm_call(db_session, _call())

    assert llm_telemetry.telemetry_failure_count() == 1
    assert any("llm_telemetry.cost_tracking_failed" in record.message for record in caplog.records)
    assert db_session.scalars(select(Event)).one().event_type == EventType.LLM_CALL


def test_record_llm_call_swallows_event_errors_and_logs_warn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    db_session: Session,
) -> None:
    _task(db_session)
    llm_telemetry.reset_telemetry_failure_count_for_tests()
    # record_llm_call now opens a sibling session; force the Event write to
    # raise by patching Session.add at the module class level so the sibling
    # session's add also fails on the Event row (after CostTracker's add).
    from sqlalchemy.orm import Session as _Session
    original_add = _Session.add
    state = {"event_added": False}

    def flaky_add(self, instance, *args, **kwargs):  # noqa: ANN001
        from app.models.event import Event
        if isinstance(instance, Event):
            state["event_added"] = True
            raise RuntimeError("event down")
        return original_add(self, instance, *args, **kwargs)

    monkeypatch.setattr(_Session, "add", flaky_add)

    with caplog.at_level(logging.WARNING, logger="app.services.llm_telemetry"):
        record_llm_call(db_session, _call())

    assert state["event_added"] is True
    assert llm_telemetry.telemetry_failure_count() == 1
    assert any("llm_telemetry.event_write_failed" in record.message for record in caplog.records)
    # CostTracker write committed successfully via the sibling session.
    assert db_session.scalars(select(LlmUsage)).one().purpose == "synthesis"


def test_provider_fallback_records_separate_events_with_fallback_step(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    _task(db_session)
    generator = CodeGenerator(db=db_session)
    monkeypatch.setattr(generator, "_resolve_provider_chain", lambda: ["anthropic", "minimax"])
    monkeypatch.setattr(
        generator,
        "_call_anthropic",
        lambda prompt: (_ for _ in ()).throw(CodegenError("OPS_AGENT_ANTHROPIC_API_KEY is not configured")),
    )
    monkeypatch.setattr(
        generator,
        "_call_minimax",
        lambda prompt, *, context_files: CodegenResult(
            diff="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a\n+b\n",
            summary="changed",
            files_changed=["a.py"],
            provider_name="minimax",
            model_name="MiniMax-M2.7",
            input_tokens=11,
            output_tokens=7,
        ),
    )

    result = generator.generate_patch(
        task_id="task-1",
        actor_name="alice",
        plan_json={"objective": "change a"},
        context_files={"a.py": "a\n"},
    )

    assert result.provider_name == "minimax"
    events = db_session.scalars(select(Event).order_by(Event.created_at)).all()
    assert [event.payload_json["provider"] for event in events] == ["anthropic", "minimax"]
    assert [event.payload_json["success"] for event in events] == [False, True]
    assert [event.payload_json["fallback_step"] for event in events] == [0, 1]


def test_synthesis_path_records_one_event_per_call(
    monkeypatch: pytest.MonkeyPatch,
    db_session: Session,
) -> None:
    _task(db_session)
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": "Use a.py."}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 9},
    }
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    monkeypatch.setattr("app.services.knowledge_synthesis.httpx.Client", Mock(return_value=client))

    answer = KnowledgeSynthesizer(_settings(), db=db_session, task_id="task-1", actor_name="alice").synthesize(
        query="Where is login?",
        citations=[_citation()],
        hallucination_risk="low",
        route_kind="code_debug",
        language=None,
    )

    assert answer == "Use a.py."
    event = db_session.scalars(select(Event)).one()
    assert event.payload_json["purpose"] == "synthesis"
    assert event.payload_json["input_tokens"] == 42
    assert event.payload_json["output_tokens"] == 9
