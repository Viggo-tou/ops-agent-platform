from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient

from app.api import tasks as tasks_api
from app.core.config import Settings
from app.main import app
from app.services.task_workspace import TaskWorkspace


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _writable_mkdtemp(prefix: str) -> Path:
    if os.name != "nt":
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(BACKEND_ROOT)))

    original_mkdir = tempfile._os.mkdir

    def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
        original_mkdir(path, 0o777)

    tempfile._os.mkdir = mkdir_with_write_access
    try:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(BACKEND_ROOT)))
    finally:
        tempfile._os.mkdir = original_mkdir


def test_workspace_checkpoint_endpoint_returns_checkpoint(monkeypatch) -> None:
    root = _writable_mkdtemp("workspace-api-")
    try:
        settings = Settings(agent_workspace_root=str(root))
        workspace = TaskWorkspace.for_task("task-1", settings)
        workspace.write_checkpoint(
            stage_completed="plan",
            next_stage="codegen",
            resume_args={"plan_id": "plan-1"},
        )
        service = Mock()
        service.task_exists.return_value = True
        monkeypatch.setattr(tasks_api, "TaskService", Mock(return_value=service))
        monkeypatch.setattr(tasks_api.TaskWorkspace, "for_task", Mock(return_value=workspace))

        response = TestClient(app).get("/api/tasks/task-1/workspace/checkpoint")

        assert response.status_code == 200
        assert response.json()["stage_completed"] == "plan"
        assert response.json()["resume_args"] == {"plan_id": "plan-1"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_workspace_checkpoint_endpoint_404s_for_missing_task(monkeypatch) -> None:
    service = Mock()
    service.task_exists.return_value = False
    monkeypatch.setattr(tasks_api, "TaskService", Mock(return_value=service))

    response = TestClient(app).get("/api/tasks/missing/workspace/checkpoint")

    assert response.status_code == 404
    assert response.json()["detail"] == "Task not found."
