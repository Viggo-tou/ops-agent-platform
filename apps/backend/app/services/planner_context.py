from __future__ import annotations

import json
from typing import Any


def _trim(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)] + "..."


def _compact_translation(document: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    for key in (
        "normalized_request",
        "intent",
        "work_type",
        "objective",
        "issue_key",
        "issue_url",
    ):
        value = document.get(key)
        if isinstance(value, str) and value.strip():
            compact[key] = _trim(value, limit=260)

    for key, limit in (
        ("candidate_modules", 6),
        ("search_queries", 4),
        ("constraints", 4),
        ("requested_outputs", 4),
        ("missing_information", 4),
    ):
        values = document.get(key)
        if isinstance(values, list):
            cleaned = [
                _trim(value, limit=160)
                for value in values
                if isinstance(value, str) and value.strip()
            ][:limit]
            if cleaned:
                compact[key] = cleaned
    return compact


def _compact_repository_context(context: dict[str, object]) -> dict[str, object]:
    compact: dict[str, object] = {}
    answer = context.get("answer")
    if isinstance(answer, str) and answer.strip():
        compact["answer"] = _trim(answer, limit=500)

    answer_trace = context.get("answer_trace")
    if isinstance(answer_trace, dict):
        trace: dict[str, object] = {}
        for key in ("route_kind", "route_reason", "hallucination_risk", "token_coverage", "top_score"):
            value = answer_trace.get(key)
            if isinstance(value, str) and value.strip():
                trace[key] = _trim(value, limit=200)
            elif isinstance(value, (int, float)):
                trace[key] = value
        selected_sources = answer_trace.get("selected_sources")
        if isinstance(selected_sources, list):
            sources = [
                _trim(value, limit=80)
                for value in selected_sources
                if isinstance(value, str) and value.strip()
            ][:4]
            if sources:
                trace["selected_sources"] = sources
        if trace:
            compact["answer_trace"] = trace

    citations = context.get("citations")
    if isinstance(citations, list):
        compact_citations: list[dict[str, object]] = []
        for citation in citations[:4]:
            if not isinstance(citation, dict):
                continue
            relative_path = citation.get("relative_path")
            if not isinstance(relative_path, str) or not relative_path.strip():
                continue
            row: dict[str, object] = {"relative_path": _trim(relative_path, limit=220)}
            source_name = citation.get("source_name")
            if isinstance(source_name, str) and source_name.strip():
                row["source_name"] = _trim(source_name, limit=80)
            for key in ("line_start", "line_end", "score"):
                value = citation.get(key)
                if isinstance(value, (int, float)):
                    row[key] = value
            snippet = citation.get("snippet")
            if isinstance(snippet, str) and snippet.strip():
                row["snippet"] = _trim(snippet, limit=240)
            compact_citations.append(row)
        if compact_citations:
            compact["citations"] = compact_citations
    return compact


def _json_block(payload: dict[str, object]) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True)


def render_planner_context_packet(
    *,
    original_request: str,
    translation_document: dict[str, object] | None = None,
    issue_context: dict[str, object] | None = None,
    planning_knowledge_context: dict[str, object] | None = None,
) -> str:
    """Render planner inputs as one stable, sectioned prompt packet.

    The planner still produces one plan. This packet only structures the
    evidence it sees, so retrieval can grow without turning the prompt into a
    loose pile of prose.
    """
    lines: list[str] = [
        '<planner_context version="1">',
        "  <user_request>",
        _trim(original_request, limit=2000),
        "  </user_request>",
    ]

    if translation_document:
        lines.extend(
            [
                '  <semantic_translation format="json">',
                _json_block(_compact_translation(translation_document)),
                "  </semantic_translation>",
            ]
        )

    if issue_context:
        issue_payload = {
            "issue_key": _trim(issue_context.get("issue_key", ""), limit=80),
            "summary": _trim(issue_context.get("summary", ""), limit=240),
            "status": _trim(issue_context.get("issue_status", ""), limit=80),
            "issue_type": _trim(issue_context.get("issue_type", ""), limit=80),
            "priority": _trim(issue_context.get("priority", ""), limit=80),
            "description": _trim(issue_context.get("description", ""), limit=1200),
            "acceptance_criteria": _trim(issue_context.get("acceptance_criteria", ""), limit=800),
        }
        issue_payload = {k: v for k, v in issue_payload.items() if v}
        lines.extend(
            [
                '  <jira_issue_context format="json">',
                _json_block(issue_payload),
                "  </jira_issue_context>",
            ]
        )

    if planning_knowledge_context:
        lines.extend(
            [
                '  <repository_context role="read_only_grounding" format="json">',
                _json_block(_compact_repository_context(planning_knowledge_context)),
                "  </repository_context>",
            ]
        )

    lines.extend(
        [
            "  <planner_focus>",
            "Pick evidence-backed existing files for must_touch_files. Do not invent helper files unless the user or Jira issue names them. Generate structural acceptance tests only for concrete symbols already present in this context.",
            "  </planner_focus>",
            "</planner_context>",
        ]
    )
    return "\n".join(lines).strip()

