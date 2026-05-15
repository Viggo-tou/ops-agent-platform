from __future__ import annotations

from types import SimpleNamespace

from app.orchestrator.service import _should_promote_evidence_must_touch_to_plan


def test_evidence_scope_promotes_when_plan_has_no_targets() -> None:
    plan = SimpleNamespace(must_touch_files=[], expected_new_files=[])

    assert _should_promote_evidence_must_touch_to_plan(plan) is True


def test_evidence_scope_does_not_promote_over_expected_new() -> None:
    plan = SimpleNamespace(must_touch_files=[], expected_new_files=["database.rules.json"])

    assert _should_promote_evidence_must_touch_to_plan(plan) is False


def test_evidence_scope_does_not_promote_over_planner_must_touch() -> None:
    plan = SimpleNamespace(must_touch_files=["src/Login.kt"], expected_new_files=[])

    assert _should_promote_evidence_must_touch_to_plan(plan) is False
