"""Stage X.7.d: _resolve_knowledge_source_path must prefer
task.translation_json[source_path] over settings.knowledge_source_path.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import PrimaryOrchestrator  # noqa: E402


def _make_orch(settings_path: str) -> PrimaryOrchestrator:
    orch = PrimaryOrchestrator.__new__(PrimaryOrchestrator)
    settings = SimpleNamespace(knowledge_source_path=settings_path)
    orch.tool_gateway = SimpleNamespace(settings=settings)
    return orch


def test_task_translation_source_path_wins_when_set():
    with tempfile.TemporaryDirectory() as task_path:
        with tempfile.TemporaryDirectory() as settings_path:
            orch = _make_orch(settings_path)
            task = SimpleNamespace(translation_json={"source_path": task_path})
            assert orch._resolve_knowledge_source_path(task) == Path(task_path)


def test_falls_back_to_settings_when_task_none():
    with tempfile.TemporaryDirectory() as settings_path:
        orch = _make_orch(settings_path)
        assert orch._resolve_knowledge_source_path(None) == Path(settings_path)


def test_falls_back_to_settings_when_task_path_does_not_exist():
    with tempfile.TemporaryDirectory() as settings_path:
        orch = _make_orch(settings_path)
        task = SimpleNamespace(translation_json={"source_path": "C:/nonexistent/zz"})
        assert orch._resolve_knowledge_source_path(task) == Path(settings_path)


def test_falls_back_when_translation_json_not_dict():
    with tempfile.TemporaryDirectory() as settings_path:
        orch = _make_orch(settings_path)
        task = SimpleNamespace(translation_json=None)
        assert orch._resolve_knowledge_source_path(task) == Path(settings_path)


def test_returns_none_when_no_settings_no_task():
    orch = _make_orch("")
    assert orch._resolve_knowledge_source_path(None) is None
