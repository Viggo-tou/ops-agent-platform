from __future__ import annotations

import json
from types import SimpleNamespace

from app.services.playbook_promotion import (
    build_playbook_promotion_candidate,
    build_playbook_promotion_rollup,
    write_playbook_promotion_candidate,
    write_playbook_promotion_rollup,
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


def _write_candidate_file(tmp_path, task_id: str, candidate: dict) -> None:
    path = tmp_path / task_id / "memory"
    path.mkdir(parents=True, exist_ok=True)
    (path / "playbook_promotion_candidate.json").write_text(
        json.dumps(candidate),
        encoding="utf-8",
    )


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
    assert candidate["evidence"]["quality_signals"]["quality_score"] == 100
    assert candidate["evidence"]["quality_rule_candidates"] == []
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
    assert candidate["evidence"]["quality_signals"]["quality_score"] == 80
    assert candidate["evidence"]["quality_rule_candidates"][0]["source"] == "reservation"


def test_candidate_extracts_semantic_quality_rule_without_recipe_promotion():
    state = _clean_state()
    state["semantic_review"] = {
        "status": "passed",
        "passed": True,
        "completeness_pct": 92,
        "high_severity_count": 0,
        "findings": [
            {
                "file": "JobPostingFlow.kt",
                "severity": "medium",
                "category": "map_state_sync",
                "description": "Map update can re-animate on every recomposition.",
                "suggested_fix": "Track the last rendered point before animateTo.",
            }
        ],
    }

    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=state,
        approval_action="jira.transition_issue",
        approval_id="approval-semantic",
    )

    assert candidate["status"] == "draft_clean"
    assert candidate["promotion_eligible"] is True
    assert candidate["evidence"]["quality_signals"]["quality_score"] == 92
    assert candidate["evidence"]["quality_rule_candidates"] == [
        {
            "source": "semantic_review",
            "severity": "medium",
            "category": "map_state_sync",
            "file": "JobPostingFlow.kt",
            "text": "Map update can re-animate on every recomposition.",
            "suggested_fix": "Track the last rendered point before animateTo.",
        }
    ]


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


def test_rollup_requires_repeated_verified_approvals(tmp_path):
    settings = SimpleNamespace(agent_workspace_root=str(tmp_path))
    base = build_playbook_promotion_candidate(
        task=_task(id="task-p69-17-a"),
        plan_json=_plan(),
        pipeline_state=_clean_state(),
        approval_action="jira.transition_issue",
        approval_id="approval-a",
    )
    second = {**base, "task_id": "task-p69-17-b"}
    _write_candidate_file(tmp_path, "task-p69-17-a", base)
    _write_candidate_file(tmp_path, "task-p69-17-b", second)

    rollup = build_playbook_promotion_rollup(
        candidate=second,
        settings=settings,  # type: ignore[arg-type]
        min_verified_approvals=3,
    )

    assert rollup["status"] == "needs_more_evidence"
    assert rollup["verified_approval_count"] == 2
    assert "needs_more_verified_approvals" in rollup["promotion_blockers"]
    assert rollup["next_action"] == "collect_more_verified_approval_runs_before_promotion"


def test_rollup_promotes_only_as_human_review_proposal(tmp_path):
    settings = SimpleNamespace(agent_workspace_root=str(tmp_path))
    state = _clean_state()
    state["semantic_review"] = {
        "status": "passed",
        "passed": True,
        "completeness_pct": 95,
        "high_severity_count": 0,
        "findings": [
            {
                "severity": "medium",
                "category": "map_update",
                "description": "Avoid repeated map animateTo calls from update.",
            }
        ],
    }
    first = build_playbook_promotion_candidate(
        task=_task(id="task-p69-17-a"),
        plan_json=_plan(),
        pipeline_state=state,
        approval_action="jira.transition_issue",
        approval_id="approval-a",
    )
    second = {**first, "task_id": "task-p69-17-b"}
    third = {**first, "task_id": "task-p69-17-c"}
    _write_candidate_file(tmp_path, "task-p69-17-a", first)
    _write_candidate_file(tmp_path, "task-p69-17-b", second)
    _write_candidate_file(tmp_path, "task-p69-17-c", third)

    rollup = build_playbook_promotion_rollup(
        candidate=third,
        settings=settings,  # type: ignore[arg-type]
        min_verified_approvals=3,
    )

    assert rollup["status"] == "ready_for_human_promotion"
    assert rollup["verified_approval_count"] == 3
    assert rollup["quality_score_floor"] == 95
    assert rollup["quality_rule_candidates"][0]["seen_count"] == 3
    assert "do_not_mutate_playbooks_automatically" in rollup["guardrails"]
    assert rollup["next_action"] == "open_human_review_to_promote_playbook_or_recipe"


def test_write_rollup_to_task_workspace(tmp_path):
    settings = SimpleNamespace(agent_workspace_root=str(tmp_path))
    candidate = build_playbook_promotion_candidate(
        task=_task(),
        plan_json=_plan(),
        pipeline_state=_clean_state(),
        approval_action="jira.transition_issue",
        approval_id="approval-1",
    )
    rollup = build_playbook_promotion_rollup(
        candidate=candidate,
        settings=settings,  # type: ignore[arg-type]
        min_verified_approvals=1,
    )

    artifact_path = write_playbook_promotion_rollup(
        task=_task(),
        rollup=rollup,
        settings=settings,  # type: ignore[arg-type]
    )

    assert artifact_path.endswith("playbook_promotion_rollup.json")
    written = tmp_path / "task-p69-17" / "memory" / "playbook_promotion_rollup.json"
    assert written.is_file()
    assert "playbook_promotion_rollup.v1" in written.read_text(encoding="utf-8")
