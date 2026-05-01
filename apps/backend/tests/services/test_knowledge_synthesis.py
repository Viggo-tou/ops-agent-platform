from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.schemas.knowledge import KnowledgeCitation  # noqa: E402
from app.services.knowledge import KnowledgeService  # noqa: E402
from app.services.knowledge_synthesis import (  # noqa: E402
    compute_question_entity_coverage,
    extract_question_entities,
    KnowledgeSynthesisError,
    KnowledgeSynthesizer,
)


def _settings(**overrides: object) -> Settings:
    values = {
        "minimax_api_key": "test-key",
        "knowledge_synthesis_enabled": True,
        "knowledge_synthesis_model": "minimax-text-01",
        "knowledge_synthesis_timeout_seconds": 3.0,
        "knowledge_synthesis_max_snippet_chars": 6000,
        # Pin source_name to the fixture's value so the test is independent
        # of any local OPS_AGENT_KNOWLEDGE_SOURCE_NAME env override.
        "knowledge_source_name": "handymanapp",
        "knowledge_source_path": None,
        "knowledge_upload_root": "__missing_upload_root__",
        # Disable retrieval-side LLM features so the test isolates the
        # synthesis path; both have their own dedicated tests.
        "knowledge_rerank_enabled": False,
        "knowledge_query_rewrite_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _citation(snippet: str = "login failure handler") -> KnowledgeCitation:
    return KnowledgeCitation(
        document_id="doc-1",
        source_name="handymanapp",
        title="auth.py",
        relative_path="src/auth.py",
        line_start=10,
        line_end=14,
        snippet=snippet,
        score=17.5,
        metadata={},
    )


def extract(question: str) -> list[str]:
    return extract_question_entities(question)


def test_extract_question_entities_finds_pascalcase_without_role_suffix() -> None:
    assert extract("PaginationControls and FirebaseAuth") == [
        "PaginationControls",
        "FirebaseAuth",
    ]
    assert extract("How does FormValidator work") == ["FormValidator"]
    assert extract("the RecyclerView pattern") == ["RecyclerView"]


def test_extract_question_entities_finds_allcaps_compounds() -> None:
    assert extract("the customer KYC and handyman KYC flows") == [
        "customer KYC",
        "handyman KYC",
    ]
    assert extract("uses OAuth login") == ["OAuth login"]


def test_extract_question_entities_finds_dotted_filenames() -> None:
    assert extract("Login.js, Dashboard.js, ServiceAnalytics.js") == [
        "Login.js",
        "Dashboard.js",
        "ServiceAnalytics.js",
    ]
    assert extract("nav_graph.xml routes") == ["nav_graph.xml"]


def test_extract_question_entities_excludes_standalone_words() -> None:
    assert extract("how does the page render") == []
    assert extract("Fragment lifecycle") == []
    assert extract("the API returns json") == []
    # This is intentionally treated as a compound entity: ALLCAPS alone is
    # ignored, but an adjacent lowercase domain noun gives the token context.
    assert extract("KYC validation logic") == ["KYC validation"]


def test_extract_question_entities_combines_rules_in_question_order() -> None:
    assert extract("Login.js calls FirebaseAuth.signIn during the customer KYC flow") == [
        "Login.js",
        "FirebaseAuth",
        "customer KYC",
    ]


def test_extract_question_entities_finds_phase_1_question_texts() -> None:
    phase_1_questions = [
        (
            "DASH B-09",
            "How does PaginationControls decide what page buttons to show?",
            1,
        ),
        (
            "DASH C-05",
            "How are the Firebase exports consumed across the login, dashboard, analytics, and support feedback pages, and where do those imports live?",
            3,
        ),
        (
            "DASH B-04",
            "How does the Support Feedback page reply to a ticket and create new tickets?",
            1,
        ),
        (
            "HAND C-09",
            "Which fragments consume the customer-side job list and details views in the handyman app?",
            2,
        ),
        (
            "HAND C-12",
            "How are the customer KYC and handyman KYC flows structured, and where do they diverge?",
            2,
        ),
    ]

    multifile_ready = 0
    for question_id, question, expected_min_entities in phase_1_questions:
        entities = extract(question)
        assert len(entities) >= expected_min_entities, question_id
        if len(entities) >= 2:
            multifile_ready += 1

    assert multifile_ready >= 3


def test_extract_question_entities_does_not_overfire_on_simple_a_tier_questions() -> None:
    assert extract("Where is HandymanLogin.kt") == ["HandymanLogin.kt"]
    assert (
        compute_question_entity_coverage(
            "Where is HandymanLogin.kt",
            "HandymanLogin.kt handles login.",
        )["multifile_mode_active"]
        is False
    )
    assert extract("Where is Firebase configured for this frontend, and what does that file export?") == [
        "Firebase",
    ]
    assert extract("Which file contains the service analytics page?") == []


def test_compute_question_entity_coverage_reports_list_diagnostic() -> None:
    coverage = compute_question_entity_coverage(
        "Compare Login.js, Dashboard.js, and ServiceAnalytics.js.",
        "Login.js and Dashboard.js are covered.",
    )

    assert coverage["entity_list_pattern_detected"] is True
    assert coverage["multifile_mode_active"] is True


def test_synthesize_success_returns_llm_text(monkeypatch: pytest.MonkeyPatch) -> None:
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": "Use src/auth.py lines 10-14."}}]}
    response.raise_for_status.return_value = None
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    client_factory = Mock(return_value=client)
    monkeypatch.setattr("app.services.knowledge_synthesis.httpx.Client", client_factory)

    result = KnowledgeSynthesizer(_settings()).synthesize(
        query="Where is login failure handled?",
        citations=[_citation()],
        hallucination_risk="low",
        route_kind="code_debug",
        language=None,
    )

    assert result == "Use src/auth.py lines 10-14."
    client_factory.assert_called_once_with(timeout=3.0)
    payload = client.post.call_args.kwargs["json"]
    assert payload["model"] == "minimax-text-01"
    system_prompt = payload["messages"][0]["content"]
    assert "<answer>" in system_prompt
    assert '<claim id="N">' in system_prompt
    assert "<claims>" in system_prompt
    assert "cite=[] confidence=low" in system_prompt
    assert "Citation indices are 1-indexed" in system_prompt


def test_synthesis_prompt_includes_multientity_coverage_block_for_two_entities() -> None:
    prompt = KnowledgeSynthesizer._build_system_prompt(
        use_chinese=False,
        mentioned_entities=["Login.js", "ServiceAnalytics.js", "Dashboard.js"],
    )

    assert "The user's question explicitly mentions the following code entities" in prompt
    assert "  1. Login.js" in prompt
    assert "  2. ServiceAnalytics.js" in prompt
    assert "  3. Dashboard.js" in prompt
    assert "Your answer MUST include at least one specific factual claim about each" in prompt
    assert "\"<entity_name>:\nnot covered by retrieved evidence.\"" in prompt


def test_synthesis_prompt_omits_multientity_coverage_block_for_single_focus() -> None:
    legacy_prompt = KnowledgeSynthesizer._build_system_prompt(use_chinese=False)
    single_entity_prompt = KnowledgeSynthesizer._build_system_prompt(
        use_chinese=False,
        mentioned_entities=["Login.js"],
    )

    assert "The user's question explicitly mentions the following code entities" not in single_entity_prompt
    assert single_entity_prompt == legacy_prompt


def test_synthesize_no_api_key_raises() -> None:
    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings(minimax_api_key=None)).synthesize(
            query="Where is login failure handled?",
            citations=[_citation()],
            hallucination_risk="low",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_http_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("POST", "https://api.minimaxi.com/v1/text/chatcompletion_v2")
    response = httpx.Response(500, request=request)
    mocked_response = Mock()
    mocked_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "server error",
        request=request,
        response=response,
    )
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = mocked_response
    monkeypatch.setattr("app.services.knowledge_synthesis.httpx.Client", Mock(return_value=client))

    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings()).synthesize(
            query="Where is login failure handled?",
            citations=[_citation()],
            hallucination_risk="low",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_empty_citations_raises() -> None:
    with pytest.raises(KnowledgeSynthesisError):
        KnowledgeSynthesizer(_settings()).synthesize(
            query="Where is login failure handled?",
            citations=[],
            hallucination_risk="high",
            route_kind="code_debug",
            language=None,
        )


def test_synthesize_respects_max_snippet_chars() -> None:
    synthesizer = KnowledgeSynthesizer(_settings(knowledge_synthesis_max_snippet_chars=12))
    evidence = synthesizer._format_evidence([_citation(snippet="x" * 50)])

    assert "x" * 12 in evidence
    assert "x" * 13 not in evidence
    assert "(truncated)" in evidence


def test_format_evidence_includes_card_before_content() -> None:
    citation = _citation(snippet="raw login code")
    citation.card_text = "**Purpose**: Explains login."
    evidence = KnowledgeSynthesizer(_settings())._format_evidence([citation])

    assert "[CARD]\n**Purpose**: Explains login.\n[CONTENT]\nraw login code" in evidence


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    source_root = BACKEND_ROOT / "tests" / "fixtures"
    content = "def login_failure_handler():\n    return 'login failure auth path'\n"
    session.add(
        KnowledgeDocument(
            source_name="handymanapp",
            relative_path="src/auth.py",
            title="auth.py",
            extension=".py",
            language="python",
            size_bytes=len(content.encode("utf-8")),
            line_count=2,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            metadata_json={},
            content=content,
        )
    )
    session.commit()
    try:
        yield session, source_root
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_search_repositories_falls_back_to_template_on_synth_error(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, source_root = db_session
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(side_effect=KnowledgeSynthesisError("provider down")),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_source_path=str(source_root))

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer_trace.answer_provider == "template"
    assert "I would start with" in result.answer
    assert result.claims == []
    assert result.ungrounded_claim_count == 0


def test_search_repositories_uses_minimax_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, source_root = db_session
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(return_value="LLM synthesized answer with handymanapp:src/auth.py (lines 1-2)."),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_source_path=str(source_root))

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer == "LLM synthesized answer with handymanapp:src/auth.py (lines 1-2)."
    assert result.answer_trace.answer_provider == "minimax"
    assert result.claims == []
    assert result.ungrounded_claim_count == 0


def test_search_repositories_extracts_claims_from_minimax_synthesis(
    monkeypatch: pytest.MonkeyPatch,
    db_session,
) -> None:
    session, source_root = db_session
    monkeypatch.setattr(
        "app.services.knowledge_synthesis.KnowledgeSynthesizer.synthesize",
        Mock(
            return_value=(
                "<answer><claim id=\"1\">The login failure handler is in src/auth.py.</claim></answer>\n"
                "<claims>\n"
                "1. cite=[1] confidence=high - The handler is in src/auth.py.\n"
                "</claims>"
            )
        ),
    )
    service = KnowledgeService(session)
    service.settings = _settings(knowledge_source_path=str(source_root))

    result = service.search_repositories(query="login failure", top_k=1)

    assert result.answer == "The login failure handler is in src/auth.py."
    assert result.claims[0].text == "The login failure handler is in src/auth.py."
    assert result.claims[0].citation_indices == [0]
    assert result.claims[0].confidence == "high"
    assert result.ungrounded_claim_count == 0
    assert result.answer_trace.answer_provider == "minimax"

