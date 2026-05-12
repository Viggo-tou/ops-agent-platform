"""Tests for planner-emitted acceptance_tests (Tier 1.3 wiring).

Pins three pieces:
  1. ``GeneratedPlanPayload`` accepts a populated ``acceptance_tests`` list.
  2. ``PrimaryAgentPlanner._sanitize_plan_payload`` drops malformed
     entries and unknown ``kind`` values rather than failing the whole
     plan.
  3. The planner system instructions reference ``acceptance_tests`` so
     downstream LLMs know to fill it.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import (  # noqa: E402
    GeneratedPlanPayload,
    PlanAcceptanceTest,
)
from app.agents.service import PrimaryAgentPlanner  # noqa: E402


def _base_payload(**overrides) -> dict:
    payload = {
        "objective": "Fix the failing arithmetic helper.",
        "request_summary": "Fix arithmetic.",
        "scenario": "jira_issue_develop",
        "change_summary": "Fix arithmetic helper.",
        "change_explanation": "Patch the helper to not crash on zero.",
        "risk_level": "medium",
        "requires_approval": False,
        "tools": [
            {
                "tool_name": "codegen.generate_patch",
                "permission_category": "write",
                "purpose": "produce the fix",
            }
        ],
        "steps": [
            {
                "step_id": "s1",
                "title": "Analyse",
                "kind": "analysis",
                "owner_role": "planner",
                "expected_output": "files identified",
                "success_criteria": "list of files",
            },
            {
                "step_id": "s2",
                "title": "Generate patch",
                "kind": "action",
                "owner_role": "action",
                "tool_name": "codegen.generate_patch",
                "expected_output": "diff",
                "success_criteria": "diff applies",
            },
            {
                "step_id": "s3",
                "title": "Apply",
                "kind": "action",
                "owner_role": "action",
                "expected_output": "tests pass",
                "success_criteria": "exit 0",
            },
            {
                "step_id": "s4",
                "title": "Review",
                "kind": "review",
                "owner_role": "reviewer",
                "expected_output": "approval",
                "success_criteria": "ok",
            },
        ],
        "final_output_contract": {
            "type": "diff",
            "required_fields": ["status"],
        },
    }
    payload.update(overrides)
    return payload


def test_schema_accepts_populated_acceptance_tests():
    payload = _base_payload(
        acceptance_tests=[
            {
                "kind": "diff_contains_pattern",
                "pattern": "def fix_arithmetic",
                "rationale": "issue names the helper",
            },
            {
                "kind": "import_added",
                "file": "app/lib/arith.py",
                "pattern": "math",
                "rationale": "fix uses math.isclose",
            },
        ]
    )
    plan = GeneratedPlanPayload.model_validate(payload)
    assert len(plan.acceptance_tests) == 2
    assert plan.acceptance_tests[0].kind == "diff_contains_pattern"
    assert plan.acceptance_tests[1].file == "app/lib/arith.py"


def test_schema_defaults_acceptance_tests_to_empty_list():
    plan = GeneratedPlanPayload.model_validate(_base_payload())
    assert plan.acceptance_tests == []


def test_acceptance_test_model_rejects_unknown_kind():
    import pydantic

    try:
        PlanAcceptanceTest(kind="hand_wave_check", pattern="x")
    except pydantic.ValidationError:
        pass
    else:
        raise AssertionError("PlanAcceptanceTest should reject unknown kind")


def test_sanitize_drops_unknown_kind_keeps_valid():
    raw = _base_payload(
        acceptance_tests=[
            {"kind": "unknown_kind", "pattern": "x"},
            {"kind": "diff_contains_pattern", "pattern": "def fix"},
            {"kind": "import_added", "file": "a.py", "pattern": "math"},
            "not a dict",
        ]
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    assert [t["kind"] for t in sanitized["acceptance_tests"]] == [
        "diff_contains_pattern",
        "import_added",
    ]
    assert sanitized["acceptance_tests"][1]["file"] == "a.py"


def test_sanitize_handles_missing_acceptance_tests_field():
    raw = _base_payload()
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    assert sanitized.get("acceptance_tests") == []


def test_sanitize_caps_at_ten_entries():
    raw = _base_payload(
        acceptance_tests=[
            {"kind": "diff_contains_pattern", "pattern": f"p{i}"}
            for i in range(15)
        ]
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    assert len(sanitized["acceptance_tests"]) == 10


def test_sanitize_normalises_optional_fields_to_none_when_blank():
    raw = _base_payload(
        acceptance_tests=[
            {
                "kind": "diff_contains_pattern",
                "pattern": "x",
                "file": "   ",
                "function": "",
                "scope": None,
            }
        ]
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    entry = sanitized["acceptance_tests"][0]
    assert entry["file"] is None
    assert entry["function"] is None
    assert entry["scope"] is None


def test_sanitize_strips_test_paths_from_must_touch_for_develop():
    """Class E counter-measure at planner level: the v8 task 4 planner
    emitted test files in must_touch (e.g. ['tests/.../tests.py',
    'django/db/models/enums.py']). The model then took that as license
    to produce a test-only diff. Sanitization should strip test paths
    for code-change scenarios."""
    raw = _base_payload(
        scenario="jira_issue_develop",
        must_touch_files=[
            "tests/model_inheritance_regress/tests.py",
            "tests/model_inheritance/test_abstract_inheritance.py",
            "django/db/models/fields/__init__.py",
            "tests/conftest.py",
            "django/db/models/enums.py",
            "package/test_helper.py",
        ],
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    must = sanitized["must_touch_files"]
    # Source files retained.
    assert "django/db/models/fields/__init__.py" in must
    assert "django/db/models/enums.py" in must
    # Test files dropped.
    assert all("tests/" not in p for p in must)
    assert "tests/conftest.py" not in must
    assert "package/test_helper.py" not in must


def test_sanitize_keeps_test_paths_in_must_touch_for_non_develop():
    """For scenarios that aren't bug-fix-shaped, the test-path filter
    is not applied — a test refactor or test-add task may legitimately
    touch test files."""
    raw = _base_payload(
        scenario="other_scenario",
        must_touch_files=[
            "tests/model_inheritance_regress/tests.py",
            "django/db/models/fields/__init__.py",
        ],
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    must = sanitized["must_touch_files"]
    # All retained.
    assert "tests/model_inheritance_regress/tests.py" in must
    assert "django/db/models/fields/__init__.py" in must


def test_sanitize_test_path_filter_handles_windows_separators():
    raw = _base_payload(
        scenario="jira_issue_develop",
        must_touch_files=[
            r"tests\foo\bar.py",
            r"src\real_fix.py",
        ],
    )
    sanitized = PrimaryAgentPlanner._sanitize_plan_payload(raw)
    must = sanitized["must_touch_files"]
    assert all("tests" not in p.replace("\\", "/").split("/") for p in must)


def test_planning_instructions_mention_acceptance_tests():
    text = PrimaryAgentPlanner._build_planning_instructions()
    assert "acceptance_tests" in text
    # Every allowed kind should be referenced so the planner knows the
    # closed set it can emit.
    for kind in (
        "diff_contains_pattern",
        "diff_contains_pattern_in_file",
        "function_signature_unchanged",
        "function_signature_changed",
        "no_new_file_outside",
        "import_added",
        "forbids_pattern_in_diff",
        "test_must_reference_existing_symbol",
    ):
        assert kind in text, f"missing kind {kind} in instructions"
