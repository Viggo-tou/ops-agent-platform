"""Regression test for _resolve_develop_repo_url multi-origin override.

Critical invariant: when task.source_name is None (the default for ALL
existing API callers and ALL pre-existing DB tasks), the resolver MUST
behave bytewise-identically to the pre-multi-origin codebase. The new
explicit_source branch is strictly additive.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


def _make_orchestrator_stub(settings: object) -> PrimaryOrchestrator:
    """Build a minimal PrimaryOrchestrator instance for resolver-only testing.

    We bypass __init__ since it requires DB / sandbox / agents and instead
    attach just the attributes the resolver actually reads.
    """
    orch = PrimaryOrchestrator.__new__(PrimaryOrchestrator)
    orch.tool_gateway = SimpleNamespace(settings=settings)
    return orch


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "sandbox_repo_url": None,
        "repository_url": None,
        "knowledge_source_path": None,
        "knowledge_source_specs": None,
        "knowledge_source_name": "",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _task(**overrides):
    base = {
        "translation_json": None,
        "source_name": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _plan(**overrides):
    base = {"provider": None}
    base.update(overrides)
    return SimpleNamespace(**base)


# ---- legacy resolution chain (source_name=None) regression ---------------


def test_legacy_translation_repo_url_wins():
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/env/path"))
    task = _task(translation_json={"repo_url": "/from/translation"})
    plan = _plan()
    assert orch._resolve_develop_repo_url(task=task, plan=plan) == "/from/translation"


def test_legacy_falls_to_settings_when_no_translation():
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/env/default"))
    task = _task()  # source_name=None, translation_json=None
    plan = _plan()
    assert orch._resolve_develop_repo_url(task=task, plan=plan) == "/env/default"


def test_legacy_returns_none_when_nothing_configured():
    orch = _make_orchestrator_stub(_settings())
    assert orch._resolve_develop_repo_url(task=_task(), plan=_plan()) is None


def test_plan_provider_repo_url_wins_over_settings():
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/env/loser"))
    task = _task()
    plan = _plan(provider={"source_path": "/from/plan"})
    assert orch._resolve_develop_repo_url(task=task, plan=plan) == "/from/plan"


# ---- new explicit_source override path ----------------------------------


def test_explicit_source_name_overrides_when_registry_resolves(monkeypatch):
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/legacy/should-not-win"))
    task = _task(source_name="my-upload")

    # Stub the registry resolver.
    import app.services.repository_registry as rr
    monkeypatch.setattr(
        rr, "resolve_path_by_name",
        lambda name: "/registry/uploaded" if name == "my-upload" else None,
    )

    assert orch._resolve_develop_repo_url(task=task, plan=_plan()) == "/registry/uploaded"


def test_explicit_source_falls_back_when_registry_unknown(monkeypatch):
    """Critical: source_name set but registry doesn't resolve → fall back to legacy."""
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/legacy/wins"))
    task = _task(source_name="never-uploaded")

    import app.services.repository_registry as rr
    monkeypatch.setattr(rr, "resolve_path_by_name", lambda name: None)

    assert orch._resolve_develop_repo_url(task=task, plan=_plan()) == "/legacy/wins"


def test_explicit_source_falls_back_when_registry_raises(monkeypatch):
    """Critical: registry exception MUST NOT break legacy resolution."""
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/legacy/wins"))
    task = _task(source_name="boom")

    import app.services.repository_registry as rr

    def _raise(_name):
        raise RuntimeError("registry corrupt")

    monkeypatch.setattr(rr, "resolve_path_by_name", _raise)

    assert orch._resolve_develop_repo_url(task=task, plan=_plan()) == "/legacy/wins"


def test_blank_source_name_does_not_consult_registry(monkeypatch):
    """Empty/whitespace source_name skips the new branch entirely."""
    orch = _make_orchestrator_stub(_settings(knowledge_source_path="/legacy/wins"))

    import app.services.repository_registry as rr
    called = {"n": 0}

    def _track(_name):
        called["n"] += 1
        return "/should-not-be-returned"

    monkeypatch.setattr(rr, "resolve_path_by_name", _track)

    for blank in ("", "   ", None):
        task = _task(source_name=blank)
        assert orch._resolve_develop_repo_url(task=task, plan=_plan()) == "/legacy/wins"

    assert called["n"] == 0
