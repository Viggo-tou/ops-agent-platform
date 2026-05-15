"""Learning-loop playbook promotion candidate artifacts.

This module does not promote playbooks or mutate harness recipes. It turns
observed pipeline outcomes into quarantined draft evidence so repeated clean
runs can later be promoted deliberately, with tests and human/auditable gates.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
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


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _single_line(value: object, *, limit: int = 600) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 1)] + "..."


def _quality_rule_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


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


def _quality_rule_candidates(pipeline_state: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract advisory quality rules from reviewer output.

    These are not deterministic recipes. They are draft checklist items that
    become promotion candidates only after repeated verified runs.
    """
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in pipeline_state.get("reservations_detailed") or []:
        if not isinstance(item, dict):
            continue
        text = _single_line(item.get("text"), limit=700)
        if not text:
            continue
        key = f"reservation:{_quality_rule_key(text)}"
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            {
                "source": "reservation",
                "severity": str(item.get("severity") or "bug"),
                "text": text,
                "auto_fixable": bool(item.get("auto_fixable")),
                "blocking": bool(item.get("blocking")),
            }
        )

    semantic = pipeline_state.get("semantic_review")
    if isinstance(semantic, dict):
        for finding in semantic.get("findings") or []:
            if not isinstance(finding, dict):
                continue
            description = _single_line(finding.get("description"), limit=700)
            if not description:
                continue
            key = f"semantic:{_quality_rule_key(description)}"
            if key in seen:
                continue
            seen.add(key)
            rules.append(
                {
                    "source": "semantic_review",
                    "severity": str(finding.get("severity") or "medium"),
                    "category": str(finding.get("category") or "general"),
                    "file": str(finding.get("file") or ""),
                    "text": description,
                    "suggested_fix": _single_line(finding.get("suggested_fix"), limit=700),
                }
            )
    return rules[:12]


def _quality_signal_summary(pipeline_state: dict[str, Any]) -> dict[str, Any]:
    reservations = [
        item for item in pipeline_state.get("reservations_detailed") or []
        if isinstance(item, dict) and str(item.get("text") or "").strip()
    ]
    blocking = [item for item in reservations if bool(item.get("blocking"))]
    auto_fixable = [item for item in reservations if bool(item.get("auto_fixable"))]
    semantic = pipeline_state.get("semantic_review")
    semantic_payload = semantic if isinstance(semantic, dict) else {}
    semantic_high = _safe_int(semantic_payload.get("high_severity_count"))
    semantic_completeness = _safe_int(
        semantic_payload.get("completeness_pct"),
        default=-1,
    )
    semantic_passed = (
        semantic_payload.get("passed") is True
        or str(semantic_payload.get("status") or "").lower() == "passed"
    )

    score = 100
    if semantic_payload:
        if semantic_passed and semantic_completeness >= 0:
            score = min(score, semantic_completeness)
        elif semantic_high > 0 and semantic_completeness >= 0:
            score = min(score, semantic_completeness)
        else:
            # Non-blocking or contradicted semantic reports should stay visible
            # without permanently poisoning a repeated-run promotion rollup.
            score = min(score, max(80, semantic_completeness if semantic_completeness >= 0 else 80))
    score -= min(45, len(blocking) * 20 + len(auto_fixable) * 5 + max(0, len(reservations) - len(auto_fixable) - len(blocking)) * 3)
    score = max(0, min(100, score))

    return {
        "quality_score": score,
        "semantic_review_passed": semantic_passed,
        "semantic_completeness_pct": (
            semantic_completeness if semantic_completeness >= 0 else None
        ),
        "semantic_high_severity_count": semantic_high,
        "reservations_count": len(reservations),
        "blocking_reservations_count": len(blocking),
        "auto_fixable_reservations_count": len(auto_fixable),
        "quality_rule_candidate_count": len(_quality_rule_candidates(pipeline_state)),
    }


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
    quality_signals = _quality_signal_summary(state)

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
            "quality_signals": quality_signals,
            "quality_rule_candidates": _quality_rule_candidates(state),
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


def _candidate_match_key(candidate: dict[str, Any]) -> tuple[str, str, str]:
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    return (
        str(candidate.get("source_name") or ""),
        str(candidate.get("domain_playbook_id") or ""),
        str(evidence.get("diff_sha256") or ""),
    )


def _load_candidate_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def load_playbook_promotion_candidates(
    *,
    settings: Settings | None = None,
) -> list[dict[str, Any]]:
    resolved = settings or get_settings()
    base = Path(resolved.agent_workspace_root).resolve()
    if not base.is_dir():
        return []
    candidates: list[dict[str, Any]] = []
    for path in base.glob("*/memory/playbook_promotion_candidate.json"):
        try:
            relative = path.resolve().relative_to(base)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].startswith("_"):
            continue
        payload = _load_candidate_file(path)
        if payload is not None:
            candidates.append(payload)
    return candidates


def build_playbook_promotion_rollup(
    *,
    candidate: dict[str, Any],
    settings: Settings | None = None,
    min_verified_approvals: int = 3,
) -> dict[str, Any]:
    """Aggregate repeated clean candidates for the same domain and diff.

    The rollup is an auditable proposal only. It never mutates a playbook or
    deterministic recipe by itself.
    """
    key = _candidate_match_key(candidate)
    loaded = load_playbook_promotion_candidates(settings=settings)
    by_task: dict[str, dict[str, Any]] = {}
    for item in [*loaded, candidate]:
        if _candidate_match_key(item) != key:
            continue
        if item.get("status") != "draft_clean" or item.get("promotion_eligible") is not True:
            continue
        task_id = str(item.get("task_id") or "")
        if not task_id:
            continue
        by_task[task_id] = item

    matches = list(by_task.values())
    quality_scores: list[int] = []
    semantic_pass_count = 0
    quality_rules: dict[str, dict[str, Any]] = {}
    for item in matches:
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        signals = evidence.get("quality_signals") if isinstance(evidence.get("quality_signals"), dict) else {}
        if "quality_score" in signals:
            quality_scores.append(_safe_int(signals.get("quality_score"), default=0))
        if signals.get("semantic_review_passed") is True:
            semantic_pass_count += 1
        for rule in evidence.get("quality_rule_candidates") or []:
            if not isinstance(rule, dict):
                continue
            text = _single_line(rule.get("text"), limit=700)
            if not text:
                continue
            rule_key = f"{rule.get('source') or 'unknown'}:{_quality_rule_key(text)}"
            existing = quality_rules.setdefault(
                rule_key,
                {
                    **rule,
                    "text": text,
                    "seen_count": 0,
                    "evidence_task_ids": [],
                },
            )
            existing["seen_count"] = int(existing.get("seen_count") or 0) + 1
            task_id = str(item.get("task_id") or "")
            if task_id and task_id not in existing["evidence_task_ids"]:
                existing["evidence_task_ids"].append(task_id)

    verified_count = len(matches)
    quality_floor = min(quality_scores) if quality_scores else None
    quality_avg = round(sum(quality_scores) / len(quality_scores), 2) if quality_scores else None
    blockers: list[str] = []
    if verified_count < min_verified_approvals:
        blockers.append("needs_more_verified_approvals")
    if quality_floor is not None and quality_floor < 85:
        blockers.append("quality_floor_below_85")
    ready = not blockers
    return {
        "schema_version": "playbook_promotion_rollup.v1",
        "status": "ready_for_human_promotion" if ready else "needs_more_evidence",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_name": key[0],
        "domain_playbook_id": key[1],
        "diff_sha256": key[2],
        "min_verified_approvals": int(min_verified_approvals),
        "verified_approval_count": verified_count,
        "promotion_evidence_task_ids": sorted(by_task),
        "semantic_review_pass_count": semantic_pass_count,
        "quality_score_floor": quality_floor,
        "quality_score_avg": quality_avg,
        "quality_rule_candidates": sorted(
            quality_rules.values(),
            key=lambda item: (
                int(item.get("seen_count") or 0),
                str(item.get("source") or ""),
                str(item.get("text") or ""),
            ),
            reverse=True,
        )[:12],
        "promotion_blockers": blockers,
        "next_action": (
            "open_human_review_to_promote_playbook_or_recipe"
            if ready
            else "collect_more_verified_approval_runs_before_promotion"
        ),
        "guardrails": [
            "draft_only",
            "do_not_mutate_playbooks_automatically",
            "require_tests_before_recipe_promotion",
        ],
    }


def write_playbook_promotion_rollup(
    *,
    task: Any,
    rollup: dict[str, Any],
    settings: Settings | None = None,
) -> str:
    workspace = TaskWorkspace.for_task(str(getattr(task, "id", "")), settings or get_settings())
    workspace.write_memory_artifact("playbook_promotion_rollup.json", rollup)
    return str(workspace.root / "memory" / "playbook_promotion_rollup.json")
