"""Unit tests for _develop_sandbox_dir honouring sandbox_external_root.

Stage 25.7: ensure orchestrator sandbox-dir lookup matches sandbox.py creation path
when OPS_AGENT_SANDBOX_EXTERNAL_ROOT is configured.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


class SandboxDirLookupTests(TestCase):
    """Test that _develop_sandbox_dir respects sandbox_external_root."""

    def _make_task(self, task_id: str = "test-task-25-7") -> SimpleNamespace:
        return SimpleNamespace(id=task_id)

    def _make_orchestrator(
        self,
        sandbox_external_root: str | None,
        sandbox_base_dir: str = "data/sandboxes",
    ) -> PrimaryOrchestrator:
        orchestrator = PrimaryOrchestrator(db=Mock())
        orchestrator.tool_gateway.settings.sandbox_base_dir = sandbox_base_dir
        # Use object.__setattr__ to bypass any Pydantic validation on test settings
        object.__setattr__(
            orchestrator.tool_gateway.settings,
            "sandbox_external_root",
            sandbox_external_root,
        )
        return orchestrator

    def test_external_root_takes_priority(self) -> None:
        """When sandbox_external_root is set, it overrides sandbox_base_dir."""
        orchestrator = self._make_orchestrator(
            sandbox_external_root="C:/SomeExternal",
            sandbox_base_dir="data/sandboxes",
        )
        task = self._make_task("9889e83d-ff7a-4e70-b9c3-d8f12a9c8b31")

        result = orchestrator._develop_sandbox_dir(task)

        expected = Path("C:/SomeExternal") / task.id
        self.assertEqual(result, expected)
        self.assertNotIn("data/sandboxes", str(result))

    def test_falls_back_to_base_dir_when_external_root_is_none(self) -> None:
        """When sandbox_external_root is None, use sandbox_base_dir."""
        orchestrator = self._make_orchestrator(
            sandbox_external_root=None,
            sandbox_base_dir="data/sandboxes",
        )
        task = self._make_task("task-fallback")

        result = orchestrator._develop_sandbox_dir(task)

        expected = Path("data/sandboxes") / task.id
        self.assertEqual(result, expected)
