from __future__ import annotations

import logging
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tool_execution import ToolExecution

logger = logging.getLogger(__name__)


@dataclass
class RollbackStepResult:
    execution_id: str
    tool_name: str
    inverse_type: str
    success: bool
    message: str


@dataclass
class RollbackResult:
    steps: list[RollbackStepResult]
    all_succeeded: bool
    total_steps: int
    succeeded_count: int
    failed_count: int
    skipped_count: int


class RollbackExecutor:
    def __init__(self, db: Session):
        self.db = db

    def execute_rollback(self, task_id: str) -> RollbackResult:
        """Load task tool executions and replay inverse actions newest first."""
        stmt = (
            select(ToolExecution)
            .where(ToolExecution.task_id == task_id)
            .order_by(
                ToolExecution.started_at.desc(),
                ToolExecution.finished_at.desc(),
                ToolExecution.id.desc(),
            )
        )
        executions = list(self.db.scalars(stmt))

        steps: list[RollbackStepResult] = []
        skipped_count = 0
        for execution in executions:
            inverse = execution.inverse_action_json
            if not isinstance(inverse, dict):
                skipped_count += 1
                continue

            inverse_with_context = {
                **inverse,
                "_execution_id": execution.id,
                "_tool_name": execution.tool_name,
            }
            steps.append(self._execute_inverse(inverse_with_context))

        succeeded_count = sum(1 for step in steps if step.success)
        failed_count = sum(1 for step in steps if not step.success)
        return RollbackResult(
            steps=steps,
            all_succeeded=failed_count == 0,
            total_steps=len(steps),
            succeeded_count=succeeded_count,
            failed_count=failed_count,
            skipped_count=skipped_count,
        )

    def _execute_inverse(
        self,
        inverse: Mapping[str, object],
    ) -> RollbackStepResult:
        inverse_type = str(inverse.get("type") or "").strip()
        if inverse_type == "git_revert":
            return self._revert_git(inverse)
        if inverse_type == "jira_transition":
            return self._revert_jira_transition(inverse)
        if inverse_type == "jira_delete_comment":
            return self._revert_jira_comment(inverse)

        return self._step_result(
            inverse=inverse,
            inverse_type=inverse_type or "unknown",
            success=False,
            message=f"Unsupported inverse action type: {inverse_type or 'unknown'}.",
        )

    def _revert_git(
        self,
        inverse: Mapping[str, object],
    ) -> RollbackStepResult:
        """Reset the sandbox git worktree to the recorded pre-mutation SHA."""
        sandbox_dir_value = str(inverse.get("sandbox_dir") or "").strip()
        before_sha = str(inverse.get("before_sha") or "").strip()
        if not sandbox_dir_value or not before_sha:
            return self._step_result(
                inverse=inverse,
                inverse_type="git_revert",
                success=False,
                message="git_revert inverse is missing sandbox_dir or before_sha.",
            )

        sandbox_dir = Path(sandbox_dir_value)
        if not sandbox_dir.exists():
            return self._step_result(
                inverse=inverse,
                inverse_type="git_revert",
                success=False,
                message=f"Sandbox directory does not exist: {sandbox_dir}.",
            )

        try:
            repo_check = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                cwd=str(sandbox_dir),
                timeout=10,
            )
            if repo_check.returncode != 0:
                return self._step_result(
                    inverse=inverse,
                    inverse_type="git_revert",
                    success=False,
                    message=f"Sandbox directory is not a git worktree: {repo_check.stderr[:300]}",
                )

            result = subprocess.run(
                ["git", "reset", "--hard", before_sha],
                capture_output=True,
                text=True,
                cwd=str(sandbox_dir),
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return self._step_result(
                inverse=inverse,
                inverse_type="git_revert",
                success=False,
                message=f"git reset --hard {before_sha} timed out in {sandbox_dir}.",
            )

        if result.returncode != 0:
            return self._step_result(
                inverse=inverse,
                inverse_type="git_revert",
                success=False,
                message=f"git reset --hard {before_sha} failed: {result.stderr[:500]}",
            )

        return self._step_result(
            inverse=inverse,
            inverse_type="git_revert",
            success=True,
            message=f"Reset sandbox {sandbox_dir} to {before_sha}.",
        )

    def _revert_jira_transition(
        self,
        inverse: Mapping[str, object],
    ) -> RollbackStepResult:
        """Placeholder for Jira status restoration; real API calls are deferred."""
        issue_key = str(inverse.get("issue_key") or "").strip()
        from_status = str(inverse.get("from_status") or "").strip()
        to_status = str(inverse.get("to_status") or "").strip()
        message = (
            "Placeholder rollback: would transition Jira issue "
            f"{issue_key or '<unknown>'} from {from_status or '<unknown>'} "
            f"to {to_status or '<unknown>'}; Jira API rollback is deferred."
        )
        logger.info(message)
        return self._step_result(
            inverse=inverse,
            inverse_type="jira_transition",
            success=True,
            message=message,
        )

    def _revert_jira_comment(
        self,
        inverse: Mapping[str, object],
    ) -> RollbackStepResult:
        """Placeholder for Jira comment deletion; real API calls are deferred."""
        issue_key = str(inverse.get("issue_key") or "").strip()
        comment_id = str(inverse.get("comment_id") or "").strip()
        message = (
            "Placeholder rollback: would delete Jira comment "
            f"{comment_id or '<unknown>'} on issue {issue_key or '<unknown>'}; "
            "Jira API rollback is deferred."
        )
        logger.info(message)
        return self._step_result(
            inverse=inverse,
            inverse_type="jira_delete_comment",
            success=True,
            message=message,
        )

    @staticmethod
    def _step_result(
        *,
        inverse: Mapping[str, object],
        inverse_type: str,
        success: bool,
        message: str,
    ) -> RollbackStepResult:
        return RollbackStepResult(
            execution_id=str(inverse.get("_execution_id") or inverse.get("execution_id") or ""),
            tool_name=str(inverse.get("_tool_name") or inverse.get("tool_name") or ""),
            inverse_type=inverse_type,
            success=success,
            message=message,
        )
