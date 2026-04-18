"""Rule-based must_touch_files fallback in build_fallback_plan_payload (T-038-B).

When the scenario is ``jira_issue_develop``, the request contains a
destructive verb, AND retrieval surfaced citation files, the planner
fallback must populate ``must_touch_files`` with those citation paths so
the spec-conformance gate can enforce that the patch actually edits them.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.service import build_fallback_plan_payload  # noqa: E402


def _knowledge(paths: list[str]) -> dict[str, object]:
    return {
        "citations": [
            {
                "source_name": "repo",
                "relative_path": path,
                "title": path,
            }
            for path in paths
        ]
    }


def test_must_touch_populated_for_destructive_develop_request() -> None:
    payload = build_fallback_plan_payload(
        request_text='Remove "Minij" from the legacy module.',
        scenario="jira_issue_develop",
        semantic_translation=None,
        planning_knowledge=_knowledge(["src/a.py", "src/b.py"]),
        issue_context={"summary": "Cleanup task"},
    )
    assert payload.must_touch_files == ["src/a.py", "src/b.py"]


def test_must_touch_empty_for_non_destructive_develop_request() -> None:
    payload = build_fallback_plan_payload(
        request_text="Add a new pagination helper module.",
        scenario="jira_issue_develop",
        semantic_translation=None,
        planning_knowledge=_knowledge(["src/a.py"]),
        issue_context={"summary": "Feature task"},
    )
    assert payload.must_touch_files == []


def test_must_touch_empty_when_no_citations() -> None:
    payload = build_fallback_plan_payload(
        request_text='Remove "Minij" everywhere.',
        scenario="jira_issue_develop",
        semantic_translation=None,
        planning_knowledge=None,
        issue_context={"summary": "Cleanup task"},
    )
    assert payload.must_touch_files == []


def test_must_touch_only_applies_to_develop_scenario() -> None:
    payload = build_fallback_plan_payload(
        request_text='Remove "Minij" from the module.',
        scenario="jira_issue_plan",
        semantic_translation=None,
        planning_knowledge=_knowledge(["src/a.py"]),
        issue_context={"summary": "Planning task"},
    )
    # plan scenario doesn't have must_touch_files semantics; field is absent
    # from that payload shape (defaults to empty list per schema).
    assert payload.must_touch_files == []


def test_must_touch_caps_at_six_entries() -> None:
    many = [f"src/f{i}.py" for i in range(10)]
    payload = build_fallback_plan_payload(
        request_text='Clean up "Minij" across all modules.',
        scenario="jira_issue_develop",
        semantic_translation=None,
        planning_knowledge=_knowledge(many),
        issue_context={"summary": "Cleanup task"},
    )
    assert len(payload.must_touch_files) == 6
    assert payload.must_touch_files == many[:6]
