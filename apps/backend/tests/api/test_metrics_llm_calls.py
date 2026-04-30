from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.models  # noqa: E402,F401
from app.core.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base, utcnow  # noqa: E402
from app.models.event import Event  # noqa: E402
from app.models.task import Task  # noqa: E402
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
    task = Task(
        id="task-1",
        actor_name="alice",
        title="Metrics task",
        request_text="measure calls",
        scenario="process_question",
    )
    session.add(task)
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def client(db_session: Session) -> TestClient:
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _record(
    db: Session,
    *,
    purpose: str,
    latency_ms: int,
    success: bool = True,
    fingerprint: str = "fp-1",
    fallback_step: int = 0,
    error_type: str | None = None,
) -> None:
    record_llm_call(
        db,
        LlmCall(
            purpose=purpose,
            provider="minimax",
            model="MiniMax-M2.7",
            input_tokens=10,
            output_tokens=5,
            latency_ms=latency_ms,
            success=success,
            fallback_step=fallback_step,
            error_type=error_type,
            prompt_fingerprint=fingerprint,
            task_id="task-1",
            actor_name="alice",
        ),
    )


def test_metrics_llm_calls_aggregates_by_purpose(client: TestClient, db_session: Session) -> None:
    _record(db_session, purpose="synthesis", latency_ms=100)
    _record(db_session, purpose="cc_agent", latency_ms=200, success=False, error_type="timeout")
    db_session.commit()

    response = client.get("/api/metrics/llm-calls?since_minutes=60")

    assert response.status_code == 200
    payload = response.json()
    assert payload["since_minutes"] == 60
    assert payload["by_purpose"]["synthesis"]["n"] == 1
    assert payload["by_purpose"]["cc_agent"]["success_rate"] == 0.0
    assert payload["error_type_distribution"] == {"timeout": 1}


def test_metrics_llm_calls_computes_p95_latency(client: TestClient, db_session: Session) -> None:
    for latency in [10, 20, 30, 40, 100]:
        _record(db_session, purpose="synthesis", latency_ms=latency, fingerprint=f"fp-{latency}")
    db_session.commit()

    response = client.get("/api/metrics/llm-calls?purpose=synthesis")

    assert response.status_code == 200
    stats = response.json()["by_purpose"]["synthesis"]
    assert stats["p50_ms"] == 30
    assert stats["p95_ms"] == 88


def test_metrics_llm_calls_filters_by_since_minutes(client: TestClient, db_session: Session) -> None:
    _record(db_session, purpose="synthesis", latency_ms=100)
    old_event = db_session.scalars(select(Event)).first()
    assert old_event is not None
    old_event.created_at = utcnow() - timedelta(minutes=120)
    _record(db_session, purpose="synthesis", latency_ms=200, fingerprint="new")
    db_session.commit()

    response = client.get("/api/metrics/llm-calls?since_minutes=60")

    assert response.status_code == 200
    stats = response.json()["by_purpose"]["synthesis"]
    assert stats["n"] == 1
    assert stats["p50_ms"] == 200


def test_metrics_llm_calls_estimates_cache_hit_pct(client: TestClient, db_session: Session) -> None:
    _record(db_session, purpose="synthesis", latency_ms=1000, fingerprint="same")
    _record(db_session, purpose="synthesis", latency_ms=100, fingerprint="same")
    db_session.commit()

    response = client.get("/api/metrics/llm-calls?purpose=synthesis")

    assert response.status_code == 200
    assert response.json()["by_purpose"]["synthesis"]["cache_hit_pct"] == 0.5
