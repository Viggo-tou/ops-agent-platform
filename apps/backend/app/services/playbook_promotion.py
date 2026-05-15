"""Learning-loop playbook promotion candidate artifacts.

This module does not promote playbooks or mutate harness recipes. It turns
observed pipeline outcomes into quarantined draft evidence so repeated clean
runs can later be promoted deliberately, with tests and human/auditable gates.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from app.core.config import Settings, get_settings
from app.services.task_workspace import TaskWorkspace


def _enum_value(value: object) -> str:
    if hasattr(value, "value"):
        return str(getattr(value, "value"))
    return str(value or "")


def _sha256_text(value: object) -> str:
    text = str(value or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _issue_key(request_text: str) -> str:
    match = re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", request_text or "")
    return match.group(0) if match else ""


def _contract_ids(plan_json: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for item in plan_json.get("required_contracts") or []:
        if isinstance(item, dict):
            cid = str(item.get("contract_id") or item.get("id") or "").strip()
            if cid and cid not in out:
                out.append(cid)
    return out


def _hard_gate_summary(pipeline_state: dict[str, Any]) -> dict[str, str]:
    compile_gate = pipeline_state.get("compile_gate")
    coverage = pipeline_state.get("contract_coverage_verdict")
    symbol_graph = pipeline_state.get("symbol_graph")
    return {
        "compile": (
            "passed"
            if isinstance(compile_gate, dict) and compile_gate.get("passed") is True
            else "unknown"
        ),
        "contract_coverage": (
            "passed"
            if isinstance(coverage, dict) and coverage.get("ok") is True
            else "unknown"
        ),
        "acceptance": "passed" if pipeline_state.get("acceptance_check_done") else "unknown",
        "symbol_graph": (
            "passed"
            if isinstance(symbol_graph, dict) and symbol_graph.get("passed") is True
            else "unknown"
        ),
    }


def _semantic_review_blockers(
    pipeline_state: dict[str, Any],
    *,
    approval_action: str,
) -> list[str]:
    blockers: list[str] = []
    semantic = pipeline_state.get("semantic_review")
    semantic_blocked = pipeline_state.get("semantic_review_blocked")
    if approval_action == "semantic_review_unresolved_high":
        blockers.append("semantic_review_unresolved_high")
    if isinstance(semantic, dict):
        high = int(semantic.get("high_severity_count") or 0)
        if high > 0:
            blockers.append("semantic_review_high_findings")
    if isinstance(semantic_blocked, dict):
        blockers.append(str(semantic_blocked.get("reason") or "semantic_review_blocked"))
    for item in pipeline_state.get("reservations_detailed") or []:
        if isinstance(item, dict) and item.get("blocking") is True:
            blockers.append("blocking_reservation")
    return sorted({b for b in blockers if b})


def build_playbook_promotion_candidate(
    *,
    task: Any,
    plan_json: dict[str, Any] | None,
    pipeline_state: dict[str, Any] | None,
    approval_action: str,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Build a quarantined promotion candidate from a pipeline outcome."""
    plan = dict(plan_json or {})
    state = dict(pipeline_state or {})
    request_text = str(getattr(task, "request_text", "") or "")
    domain_id = str(
        plan.get("domain_playbook_id")
        or (plan.get("provider") or {}).get("domain_playbook_id")
        or ""
    ).strip()
    files_changed = [
        str(item)
        for item in (
            state.get("files_changed")
            or state.get("patched_files")
            or []
        )
        if str(item or "").strip()
    ]
    diff = str(state.get("diff") or "")
    blockers = _semantic_review_blockers(state, approval_action=approval_action)
    gates = _hard_gate_summary(state)
    gates_clean = all(value == "passed" for value in gates.values())
    jira_ready = approval_action == "jira.transition_issue"
    promotion_eligible = jira_ready and gates_clean and not blockers
    status = "draft_clean" if promotion_eligible else "draft_blocked"

    return {
        "schema_version": "playbook_promotion_candidate.v1",
        "status": status,
        "promotion_eligible": promotion_eligible,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_id": str(getattr(task, "id", "") or ""),
        "issue_key": _issue_key(request_text),
        "scenario": str(getattr(task, "scenario", "") or ""),
        "source_name": str(getattr(task, "source_name", "") or ""),
        "domain_playbook_id": domain_id,
        "task_subtype": domain_id,
        "approval": {
            "id": approval_id or "",
            "action_name": approval_action,
        },
        "evidence": {
            "promotion_evidence_task_ids": [str(getattr(task, "id", "") or "")],
            "files_changed": files_changed,
            "diff_sha256": _sha256_text(diff),
            "required_contract_ids": _contract_ids(plan),
            "hard_gates": gates,
            "semantic_review": state.get("semantic_review") or {},
            "reservations": state.get("reservations") or [],
            "reservations_detailed": state.get("reservations_detailed") or [],
        },
        "promotion_blockers": blockers,
        "next_action": (
            "eligible_for_repeated-run_scoring"
            if promotion_eligible
            else "keep_as_advisory_draft_until_blockers_resolved"
        ),
        "guardrails": [
            "draft_only",
            "do_not_use_as_deterministic_recipe",
            "require_repeated_verified_approvals_before_promotion",
        ],
    }


def write_playbook_promotion_candidate(
    *,
    task: Any,
    candidate: dict[str, Any],
    settings: Settings | None = None,
) -> str:
    workspace = TaskWorkspace.for_task(str(getattr(task, "id", "")), settings or get_settings())
    workspace.write_memory_artifact("playbook_promotion_candidate.json", candidate)
    return str(workspace.root / "memory" / "playbook_promotion_candidate.json")
