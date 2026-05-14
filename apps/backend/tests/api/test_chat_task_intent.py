from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import chat  # noqa: E402
from app.core.enums import ActorRole  # noqa: E402


def test_persist_task_intent_forces_scenario_and_preserves_summary_context(monkeypatch):
    captured = {}

    class DummyDb:
        def close(self) -> None:
            captured["closed"] = True

    class DummyTaskService:
        def __init__(self, db):
            captured["db"] = db

        def create_task(self, payload):
            captured["payload"] = payload
            return SimpleNamespace(id="task-123")

    monkeypatch.setattr(chat, "SessionLocal", lambda: DummyDb())
    monkeypatch.setattr(chat, "TaskService", DummyTaskService)

    task_id = chat._persist_task_intent_task_once(
        message="develop",
        summary="Develop implementation for P69-19",
        scenario="jira_issue_develop",
        session_id="session-1",
        actor_name="Operator",
        actor_role=ActorRole.ADMIN,
        previous_task_id=None,
        source_name="handymanapp",
    )

    payload = captured["payload"]
    assert task_id == "task-123"
    assert payload.scenario_override == "jira_issue_develop"
    assert payload.source_name == "handymanapp"
    assert "P69-19" in payload.request
    assert "User follow-up: develop" in payload.request
    assert captured["closed"] is True
