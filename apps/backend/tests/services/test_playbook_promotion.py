from __future__ import annotations

from types import SimpleNamespace

from app.services.playbook_promotion import (
    build_playbook_promotion_candidate,
    write_playbook_promotion_candidate,
)


def _task(**overrides):
    data = {
        "id": "task-p69-17",
        "request_text": "Develop Jira issue P69-17.",
        "scenario": "jira_issue_develop",
        "source_name": "handymanapp",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _plan():
    return {
        "domain_playbook_id": "android_job_default_address",
        "required_contracts": [
            {"contract_id": "saved_home_address_loaded"},
            {"contract_id": "job_location_prefilled"},
        ],
    }


def _clean_state():
    return {
        "diff": "diff --git a/Foo.kt b/Foo.kt\n+val x = 1\n",
        "files_changed": ["app/src/main/java/com/example/handyman/JobPostingFragment.kt"],
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True},
        "acceptance_check_done": True,
        "symbol_graph": {"passed": True},
    }


def test_clean_jira_approval_builds_draft_clean_candidate():
    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=_clean_state(),
        approval_action="jira.transition_issue",
        approval_id="approval-1",
    )

    assert candidate["schema_version"] == "playbook_promotion_candidate.v1"
    assert candidate["status"] == "draft_clean"
    assert candidate["promotion_eligible"] is True
    assert candidate["issue_key"] == "P69-17"
    assert candidate["domain_playbook_id"] == "android_job_default_address"
    assert candidate["evidence"]["hard_gates"]["compile"] == "passed"
    assert candidate["evidence"]["required_contract_ids"] == [
        "saved_home_address_loaded",
        "job_location_prefilled",
    ]
    assert "do_not_use_as_deterministic_recipe" in candidate["guardrails"]


def test_semantic_high_approval_builds_blocked_candidate():
    state = _clean_state()
    state["semantic_review"] = {
        "status": "failed",
        "high_severity_count": 1,
        "findings": [{"category": "unbound_field"}],
    }
    state["semantic_review_blocked"] = {"reason": "semantic_review_unresolved_high"}

    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=state,
        approval_action="semantic_review_unresolved_high",
        approval_id="approval-2",
    )

    assert candidate["status"] == "draft_blocked"
    assert candidate["promotion_eligible"] is False
    assert "semantic_review_unresolved_high" in candidate["promotion_blockers"]
    assert candidate["next_action"] == "keep_as_advisory_draft_until_blockers_resolved"


def test_blocking_reservation_blocks_promotion_candidate():
    state = _clean_state()
    state["reservations"] = ["Needs human policy review."]
    state["reservations_detailed"] = [
        {
            "text": "Needs human policy review.",
            "severity": "policy",
            "auto_fixable": False,
            "blocking": True,
        }
    ]

    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=state,
        approval_action="jira.transition_issue",
        approval_id="approval-3",
    )

    assert candidate["promotion_eligible"] is False
    assert "blocking_reservation" in candidate["promotion_blockers"]
    assert candidate["evidence"]["reservations"] == ["Needs human policy review."]
    assert candidate["evidence"]["reservations_detailed"][0]["blocking"] is True


def test_write_candidate_to_task_workspace(tmp_path):
    settings = SimpleNamespace(agent_workspace_root=str(tmp_path))
    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=_clean_state(),
        approval_action="jira.transition_issue",
        approval_id="approval-1",
    )

    artifact_path = write_playbook_promotion_candidate(
        task=_task(),
        candidate=candidate,
        settings=settings,  # type: ignore[arg-type]
    )

    assert artifact_path.endswith("playbook_promotion_candidate.json")
    written = tmp_path / "task-p69-17" / "memory" / "playbook_promotion_candidate.json"
    assert written.is_file()
    assert "android_job_default_address" in written.read_text(encoding="utf-8")
