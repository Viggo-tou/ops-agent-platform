from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.enums import EventSource, EventType, TaskStatus  # noqa: E402
from app.services.failure_diagnosis import (  # noqa: E402
    DiagnosisError,
    build_failure_context,
    parse_diagnosis_output,
    run_diagnosis,
)
from app.services.codegen import CodegenError  # noqa: E402


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "failure_diagnosis_enabled": True,
        "failure_diagnosis_timeout_seconds": 30.0,
        "failure_diagnosis_max_events": 30,
        "failure_diagnosis_keyfile_head_chars": 500,
        "sandbox_base_dir": "",
        "primary_agent_provider": "mock",
        "codegen_provider": None,
        "claude_code_command": "npx",
        "codex_command": "codex",
        "anthropic_api_key": None,
        "deepseek_api_key": None,
        "openai_api_key": None,
        "minimax_api_key": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(**overrides) -> SimpleNamespace:
    values = {
        "id": "task-failure",
        "session_id": "session-failure",
        "scenario": "jira_issue_develop",
        "request_text": "修复 P69-7",
        "plan_json": {"objective": "Fix jobData.js"},
        "latest_result_json": {
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "result": {
                "decision": "compile_repair_cap_exceeded",
                "message": "repair cap exceeded",
                "residual_compile_errors": [
                    {
                        "file": "src/data/jobData.js",
                        "error": "Error: Invalid package config D:\\sandbox\\package.json",
                    }
                ],
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _event(index: int, *, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=EventType.TOOL_FAILED if index % 2 else EventType.TOOL_SUCCEEDED,
        source=EventSource.ORCHESTRATOR,
        stage=None,
        role=None,
        tool_name="compile_gate.check",
        message=f"event {index}",
        payload_json=payload or {},
        created_at=index,
    )


class FakeDb:
    def __init__(self, events=None, approvals=None) -> None:
        self.events = list(events or [])
        self.approvals = list(approvals or [])
        self.scalars_calls = 0
        self.added = []

    def scalars(self, stmt):  # noqa: ANN001
        del stmt
        self.scalars_calls += 1
        return self.events if self.scalars_calls == 1 else self.approvals

    def get(self, model, task_id):  # noqa: ANN001
        del model, task_id
        return None

    def add(self, item) -> None:  # noqa: ANN001
        self.added.append(item)

    def flush(self) -> None:
        return None


class FailureDiagnosisTests(unittest.TestCase):
    def setUp(self) -> None:
        if os.name == "nt":
            original_mkdir = tempfile._os.mkdir

            def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
                original_mkdir(path, 0o777)

            tempfile._os.mkdir = mkdir_with_write_access
            try:
                self.root = Path(tempfile.mkdtemp(prefix="failure-diagnosis-"))
            finally:
                tempfile._os.mkdir = original_mkdir
        else:
            self.root = Path(tempfile.mkdtemp(prefix="failure-diagnosis-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_failurecontext_built_from_compile_repair_cap_task(self) -> None:
        events = [_event(i) for i in range(35, 0, -1)]
        task = _task()
        db = FakeDb(events=events)

        ctx = build_failure_context(
            task=task,
            db=db,
            settings=_settings(),
            failure_kind="compile_repair_cap_exceeded",
        )

        self.assertEqual(len(ctx.last_n_events), 30)
        self.assertEqual(ctx.failure_kind, "compile_repair_cap_exceeded")
        self.assertTrue(any("Invalid package config" in error for error in ctx.residual_errors))

    def test_sandbox_keyfiles_includes_package_json_when_present(self) -> None:
        sandbox = self.root / "task-failure"
        sandbox.mkdir()
        (sandbox / "package.json").write_text('{"name":"broken"}', encoding="utf-8")
        task = _task(latest_result_json={"result": {"diff_path": str(sandbox)}})

        ctx = build_failure_context(
            task=task,
            db=FakeDb(events=[]),
            settings=_settings(),
            failure_kind="compile_repair_cap_exceeded",
        )

        self.assertIn("package.json", ctx.sandbox_keyfiles)
        self.assertIn("broken", ctx.sandbox_keyfiles["package.json"])

    def test_sandbox_keyfiles_skips_dotenv_content(self) -> None:
        sandbox = self.root / "task-failure"
        sandbox.mkdir()
        (sandbox / ".env").write_text("SECRET=value", encoding="utf-8")
        task = _task(latest_result_json={"result": {"diff_path": str(sandbox)}})

        ctx = build_failure_context(
            task=task,
            db=FakeDb(events=[]),
            settings=_settings(),
            failure_kind="compile_repair_cap_exceeded",
        )

        self.assertEqual(ctx.sandbox_keyfiles[".env"], "<redacted: filename only>")
        self.assertNotIn("SECRET", str(ctx.sandbox_keyfiles))

    def test_diagnosis_output_parses_valid_llm_response(self) -> None:
        output = parse_diagnosis_output(
            '{"summary":"package.json is broken","root_cause":"Missing comma.",'
            '"likely_fix":"Fix package.json.","confidence":"high",'
            '"related_files":["package.json"]}'
        )
        self.assertEqual(output.confidence, "high")
        self.assertEqual(output.related_files, ["package.json"])

    def test_diagnosis_output_rejects_malformed_llm_response(self) -> None:
        with self.assertRaises(DiagnosisError):
            parse_diagnosis_output("not json")
        with self.assertRaises(DiagnosisError):
            parse_diagnosis_output('{"summary":"x","confidence":"certain"}')

    def test_run_diagnosis_returns_none_on_llm_timeout(self) -> None:
        def hang(**kwargs):  # noqa: ANN003
            del kwargs
            time.sleep(2)
            return None

        with patch("app.services.failure_diagnosis._run_diagnosis_sync", side_effect=hang), patch(
            "app.services.failure_diagnosis.record_event"
        ):
            result = run_diagnosis(
                task=_task(),
                db=FakeDb(events=[]),
                settings=_settings(failure_diagnosis_timeout_seconds=0.1),
                failure_kind="tool_failed_terminal",
            )
        self.assertIsNone(result)

    def test_run_diagnosis_skipped_when_disabled(self) -> None:
        with patch("app.services.failure_diagnosis._run_diagnosis_sync") as sync:
            result = run_diagnosis(
                task=_task(),
                db=FakeDb(events=[]),
                settings=_settings(failure_diagnosis_enabled=False),
                failure_kind="tool_failed_terminal",
            )
        self.assertIsNone(result)
        sync.assert_not_called()

    def test_provider_fallback_when_first_provider_fails(self) -> None:
        responses = iter(
            [
                CodegenError("claude failed"),
                '{"summary":"codex diagnosed it","root_cause":"Config error.",'
                '"likely_fix":"Fix config.","confidence":"medium","related_files":["package.json"]}',
            ]
        )

        def call_provider(**kwargs):  # noqa: ANN003
            value = next(responses)
            if isinstance(value, BaseException):
                raise value
            return value

        with patch("app.services.failure_diagnosis.CodeGenerator._resolve_provider_chain", return_value=["claude_code", "codex"]), patch(
            "app.services.failure_diagnosis._call_diagnosis_provider", side_effect=call_provider
        ), patch("app.services.failure_diagnosis.record_event"):
            result = run_diagnosis(
                task=_task(),
                db=FakeDb(events=[]),
                settings=_settings(),
                failure_kind="compile_repair_cap_exceeded",
            )

        self.assertIsNotNone(result)
        self.assertEqual(result.summary, "codex diagnosed it")

    def test_p69_7_style_failure_produces_correct_root_cause(self) -> None:
        sandbox = self.root / "task-failure"
        sandbox.mkdir()
        (sandbox / "package.json").write_text('{"scripts": {\n  "build": "vite"\n  "test": "vitest"\n}', encoding="utf-8")
        task = _task(latest_result_json={
            "status": TaskStatus.AWAITING_APPROVAL.value,
            "result": {
                "decision": "compile_repair_cap_exceeded",
                "diff_path": str(sandbox),
                "residual_compile_errors": [
                    {
                        "file": "src/data/jobData.js",
                        "error": "Error: Invalid package config D:\\sandbox\\package.json. at getPackageScopeConfig",
                    }
                ],
            },
        })
        llm_response = (
            '{"summary":"The task is blocked by a malformed package.json, not jobData.js.",'
            '"root_cause":"Node cannot read the sandbox package.json because it is malformed.",'
            '"likely_fix":"Open package.json and add the missing comma before rerunning the compile gate.",'
            '"confidence":"high","related_files":["package.json","src/data/jobData.js"]}'
        )

        with patch("app.services.failure_diagnosis.CodeGenerator._resolve_provider_chain", return_value=["mock"]), patch(
            "app.services.failure_diagnosis._call_diagnosis_provider", return_value=llm_response
        ), patch("app.services.failure_diagnosis.record_event"):
            result = run_diagnosis(
                task=task,
                db=FakeDb(events=[_event(1)]),
                settings=_settings(),
                failure_kind="compile_repair_cap_exceeded",
            )

        self.assertIsNotNone(result)
        self.assertIn("package.json", result.summary)
        self.assertIn("package.json", result.related_files)


if __name__ == "__main__":
    unittest.main()
