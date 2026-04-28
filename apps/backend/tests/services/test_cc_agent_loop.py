from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.base import Base
from app.models.knowledge_document import KnowledgeDocument
from app.schemas.evidence import EvidenceItem
from app.services import cc_agent_loop
from app.services.cc_agent import CCFileMatch, CCToolResult
from app.services.cc_agent_loop import CCAgentBudget, CCAgentResult
from app.services.knowledge import KnowledgeService, SourceSpec

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _tool_result(tool: str) -> CCToolResult:
    return CCToolResult(
        tool=tool,  # type: ignore[arg-type]
        args={"pattern": "*.js"} if tool != "read" else {"path": "src/Login.js", "line_range": None},
        matches=[CCFileMatch("src/Login.js", 12)],
        raw_text="function handleLogin() {}\n" if tool == "read" else "src/Login.js:12:handleLogin\n",
        duration_ms=5,
    )


def test_agent_first_provider_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(
        cc_agent_loop,
        "_call_decision_provider",
        lambda provider, **kwargs: '{"action":{"tool":"glob","args":{"pattern":"*.js"}},"thought":"find files"}',
    )
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_glob", lambda *args, **kwargs: _tool_result("glob"))

    result = cc_agent_loop.run_cc_agent(
        "where is login?",
        cwd=REPO_ROOT,
        budget=CCAgentBudget(max_rounds=1),
        provider_chain=["claude_code", "codex"],
    )

    assert result.decision_model == "claude_code"
    assert result.evidence_items[0].source == "cc_glob"


def test_agent_falls_back_to_codex_on_claude_fail(monkeypatch) -> None:
    def fake_provider(provider: str, **kwargs) -> str:
        if provider == "claude_code":
            raise RuntimeError("no auth")
        return '{"action":{"tool":"grep","args":{"pattern":"Login"}},"thought":"search"}'

    monkeypatch.setattr(cc_agent_loop, "_call_decision_provider", fake_provider)
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_grep", lambda *args, **kwargs: _tool_result("grep"))

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=1))

    assert result.decision_model == "codex"
    assert result.fallback_reason == "budget_exhausted"


def test_agent_falls_back_to_minimax_when_both_clis_fail(monkeypatch) -> None:
    def fake_provider(provider: str, **kwargs) -> str:
        if provider in {"claude_code", "codex"}:
            raise RuntimeError("cli failed")
        return '{"action":{"tool":"grep","args":{"pattern":"Login"}},"thought":"search"}'

    monkeypatch.setattr(cc_agent_loop, "_call_decision_provider", fake_provider)
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_grep", lambda *args, **kwargs: _tool_result("grep"))

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=1))

    assert result.decision_model == "minimax"
    assert result.fallback_reason == "budget_exhausted"


def test_agent_returns_fallback_to_rag_when_all_providers_fail(monkeypatch) -> None:
    monkeypatch.setattr(
        cc_agent_loop,
        "_call_decision_provider",
        lambda provider, **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
    )

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=1))

    assert result.evidence_items == []
    assert result.fallback_reason == "all_providers_failed"


def test_agent_terminates_on_done_action(monkeypatch) -> None:
    responses = iter([
        '{"action":{"tool":"grep","args":{"pattern":"Login"}},"thought":"search"}',
        '{"done":true,"thought":"enough"}',
    ])
    monkeypatch.setattr(cc_agent_loop, "_call_decision_provider", lambda provider, **kwargs: next(responses))
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_grep", lambda *args, **kwargs: _tool_result("grep"))

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=3))

    assert result.fallback_reason is None
    assert result.rounds_run == 2
    assert len(result.evidence_items) == 1


def test_agent_terminates_on_budget_exhausted(monkeypatch) -> None:
    monkeypatch.setattr(
        cc_agent_loop,
        "_call_decision_provider",
        lambda provider, **kwargs: '{"action":{"tool":"grep","args":{"pattern":"Login"}},"thought":"search"}',
    )
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_grep", lambda *args, **kwargs: _tool_result("grep"))

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=2, max_tool_calls=8))

    assert result.rounds_run == 2
    assert result.fallback_reason == "budget_exhausted"


def test_agent_handles_invalid_json_then_recovers(monkeypatch) -> None:
    responses = iter(["not-json", '{"action":{"tool":"grep","args":{"pattern":"Login"}},"thought":"search"}'])
    monkeypatch.setattr(cc_agent_loop, "_call_decision_provider", lambda provider, **kwargs: next(responses))
    monkeypatch.setattr(cc_agent_loop.cc_tools, "cc_grep", lambda *args, **kwargs: _tool_result("grep"))

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=2))

    assert len(result.evidence_items) == 1


def test_agent_terminates_after_two_consecutive_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(cc_agent_loop, "_call_decision_provider", lambda provider, **kwargs: "not-json")

    result = cc_agent_loop.run_cc_agent("login", cwd=REPO_ROOT, budget=CCAgentBudget(max_rounds=3))

    assert result.fallback_reason == "invalid_json"
    assert result.evidence_items == []


def test_agent_evidence_items_have_correct_source_field() -> None:
    assert cc_agent_loop._tool_result_to_evidence(_tool_result("glob"))[0].source == "cc_glob"
    assert cc_agent_loop._tool_result_to_evidence(_tool_result("grep"))[0].source == "cc_grep"
    assert cc_agent_loop._tool_result_to_evidence(_tool_result("read"))[0].source == "cc_read"


def test_anthropic_not_in_default_chain(monkeypatch) -> None:
    settings = SimpleNamespace(cc_agent_provider_chain="claude_code,codex,anthropic,minimax")
    monkeypatch.setattr(cc_agent_loop, "get_settings", lambda: settings)

    assert cc_agent_loop._default_provider_chain() == ["claude_code", "codex", "minimax"]


def test_knowledge_retrieve_uses_cc_when_enabled(monkeypatch, db_session) -> None:
    source_dir = Path("repo")
    content = "function handleLogin() {\n  return true;\n}\n"
    _add_document(db_session, content)

    monkeypatch.setattr(KnowledgeService, "_resolve_source_specs", lambda self: [SourceSpec("repo", source_dir)])
    monkeypatch.setattr(
        cc_agent_loop,
        "run_cc_agent",
        lambda *args, **kwargs: CCAgentResult(
            evidence_items=[
                EvidenceItem(
                    id="e1",
                    source="cc_read",
                    file_path="src/Login.js",
                    line_start=1,
                    line_end=3,
                    snippet=content,
                    chunk_kind="line_window",
                )
            ],
            rounds_run=1,
            tool_calls_made=1,
            duration_ms=10,
            decision_model="claude_code",
        ),
    )

    service = KnowledgeService(db_session)
    service.settings.cc_agentic_enabled = True
    service.settings.knowledge_synthesis_enabled = False
    result = service.search_repositories(query="login auth", source_name="repo")

    assert result.evidence_items[0].source == "cc_read"
    assert result.answer_trace.strategy == "cc_agentic_retrieval"


def test_knowledge_retrieve_falls_back_to_rag_when_cc_returns_empty(monkeypatch, db_session) -> None:
    source_dir = Path("repo")
    content = "function handleLogin() { return true; }\n"
    _add_document(db_session, content)

    monkeypatch.setattr(KnowledgeService, "_resolve_source_specs", lambda self: [SourceSpec("repo", source_dir)])
    monkeypatch.setattr(
        cc_agent_loop,
        "run_cc_agent",
        lambda *args, **kwargs: CCAgentResult([], 1, 0, 10, "claude_code", "all_providers_failed"),
    )

    service = KnowledgeService(db_session)
    service.settings.cc_agentic_enabled = True
    service.settings.knowledge_synthesis_enabled = False
    result = service.search_repositories(query="handleLogin", source_name="repo")

    assert result.answer_trace.strategy == "repository_semantic_retrieval"
    assert result.evidence_items[0].source == "rag_lexical"


def _add_document(db_session, content: str) -> None:
    db_session.add(
        KnowledgeDocument(
            source_name="repo",
            relative_path="src/Login.js",
            title="Login.js",
            extension=".js",
            language="javascript",
            size_bytes=len(content.encode("utf-8")),
            line_count=len(content.splitlines()),
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            metadata_json={},
            content=content,
        )
    )
    db_session.commit()
