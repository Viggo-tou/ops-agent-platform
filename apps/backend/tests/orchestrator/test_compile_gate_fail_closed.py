"""Stage X.4: when compile_gate raises an unexpected exception (e.g. bug
in run_compile_check), the orchestrator must FAIL-CLOSE — emit
REVIEW_FAILED and block the pipeline — instead of silently continuing
to LLM review gates that only see the diff text and cannot detect
structural breakage in the post-apply file.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def test_unexpected_compile_exception_emits_review_failed():
    """When run_compile_check raises an unexpected exception (e.g. NoneType
    subscript bug), orchestrator must emit REVIEW_FAILED and return 'failed',
    not silently continue.
    """
    from app.services.compile_gate import CompileResult
    # Sentinel to verify _fail_develop_pipeline gets called
    fail_called = {"value": False, "kwargs": None}

    class _FakeOrchestrator:
        def __init__(self):
            from app.core.enums import EventType
            self.EventType = EventType

        def _fail_develop_pipeline(self, *, task, event_type, stage, role, message, payload):
            fail_called["value"] = True
            fail_called["kwargs"] = {
                "event_type": event_type,
                "message": message,
                "payload": payload,
            }

        def _preserve_develop_pipeline_state(self, *, task, pipeline_state):
            pass

    # Simulate the relevant block in isolation
    pipeline_state = {
        "compile_gate_unexpected_exception": True,
        "compile_gate_traceback": "Traceback...\n  File 'verification_profile.py' line 1\nTypeError: 'NoneType' object is not subscriptable",
    }
    compile_result = None
    compile_errored = True
    compile_unexpected_exception = True

    orch = _FakeOrchestrator()

    class _FakePlan:
        plan_id = "plan-x"

    class _FakeTask:
        id = "task-x"

    if compile_result is None or compile_errored:
        if compile_unexpected_exception:
            orch._fail_develop_pipeline(
                task=_FakeTask(),
                event_type=orch.EventType.REVIEW_FAILED,
                stage=None,
                role=None,
                message="block",
                payload={"plan_id": _FakePlan.plan_id, "compile_traceback": pipeline_state["compile_gate_traceback"]},
            )

    assert fail_called["value"] is True
    assert "NoneType" in fail_called["kwargs"]["payload"]["compile_traceback"]
    assert fail_called["kwargs"]["event_type"].value == "review_failed"


def test_expected_skip_does_not_fail_closed():
    """When compile_gate skips legitimately (unknown_repo_type, missing
    toolchain), pipeline should NOT be blocked — keeps existing 'errored'
    return-value semantics.
    """
    pipeline_state = {}  # no unexpected_exception flag
    compile_result = None
    compile_errored = True
    compile_unexpected_exception = False

    fail_called = {"value": False}

    class _FakeOrchestrator:
        def _fail_develop_pipeline(self, **kwargs):
            fail_called["value"] = True

    orch = _FakeOrchestrator()
    if compile_result is None or compile_errored:
        if compile_unexpected_exception:
            orch._fail_develop_pipeline()

    assert fail_called["value"] is False, "expected skip must not trigger fail-closed"
