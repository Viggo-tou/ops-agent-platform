"""Continuation feature: previous_task_id augments request_text."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.services.tasks import TaskService  # noqa: E402


def _build(
    *,
    parent_request: str = "Initial request",
    parent_plan_json: dict | None = None,
    parent_result_json: dict | None = None,
    parent_id: str = "parent-1",
    parent_status: str = "failed",
    user_followup: str = "Follow up",
) -> str:
    return TaskService._build_continuation_request(
        parent_request=parent_request,
        parent_plan_json=parent_plan_json,
        parent_result_json=parent_result_json,
        parent_id=parent_id,
        parent_status=parent_status,
        user_followup=user_followup,
    )


def test_continuation_includes_parent_objective() -> None:
    output = _build(
        parent_plan_json={"objective": "Add Google Maps default address"},
        user_followup="fix the unresolved jobLocation references",
    )

    assert "Add Google Maps default address" in output
    assert "fix the unresolved jobLocation references" in output


def test_continuation_includes_failure_reason() -> None:
    output = _build(
        parent_result_json={
            "reason": "compile_gate_exhausted",
            "message": "Unresolved reference 'jobLocation'",
        }
    )

    assert "compile_gate_exhausted" in output
    assert "Unresolved reference 'jobLocation'" in output


def test_continuation_handles_missing_plan_or_result() -> None:
    output = _build(
        parent_plan_json=None,
        parent_result_json=None,
        parent_id="task-missing-context",
        user_followup="retry with the missing imports fixed",
    )

    # parent_id dashes are now replaced with spaces in the preamble
    # to prevent Jira issue regex from matching UUID segments. So the
    # rendered form is "task missing context".
    assert "task missing context" in output
    assert "retry with the missing imports fixed" in output


def test_continuation_truncates_long_parent_request() -> None:
    output = _build(parent_request="x" * 5000)

    parent_request_section = output.split("Parent original request:\n", 1)[1].split(
        "\n\nParent objective:",
        1,
    )[0]
    assert len(parent_request_section) == 1500


def test_continuation_truncates_long_failure_message() -> None:
    output = _build(parent_result_json={"reason": "x", "message": "y" * 2000})

    failure_message_section = output.split("Parent failure message: ", 1)[1].split(
        "\n\n[USER FOLLOWUP / NEW REQUEST]:",
        1,
    )[0]
    assert len(failure_message_section) == 600


def test_continuation_marks_user_followup_clearly() -> None:
    output = _build(user_followup="continue from the failed compile")

    assert "[USER FOLLOWUP / NEW REQUEST]" in output

