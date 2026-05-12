from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.agents.schemas import FinalOutputContract, GeneratedPlan, PlanStep, PlanTool
from app.core.config import Settings
from app.core.enums import RiskLevel, RoleName, ToolPermissionCategory
from app.schemas.evidence import EvidenceItem
from app.services.task_workspace import TaskWorkspace, sweep_task_workspaces


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


@pytest.fixture()
def workspace_root():
    root = _writable_mkdtemp("workspace-tests-")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _settings(root: Path, *, threshold: int = 4000, archive: bool = False) -> Settings:
    return Settings(
        agent_workspace_root=str(root),
        agent_workspace_snippet_inline_threshold=threshold,
        agent_workspace_archive_on_complete=archive,
        agent_workspace_retention_hours=168,
    )


def _evidence(
    item_id: str = "ev-1",
    *,
    source: str = "rag_lexical",
    file_path: str = "src/auth.py",
    snippet: str = "login failure",
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        source=source,
        file_path=file_path,
        line_start=1,
        line_end=3,
        snippet=snippet,
        enclosing_symbol="login",
        chunk_kind="function",
        confidence=0.8,
        metadata={"score": 40.0},
    )


def _plan(task_id: str = "task-1") -> GeneratedPlan:
    return GeneratedPlan(
        task_id=task_id,
        objective="Answer the repository question.",
        request_summary="Find auth config.",
        scenario="process_question",
        change_summary="Search knowledge.",
        change_explanation="Use repository evidence.",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        tools=[
            PlanTool(
                tool_name="knowledge.search",
                permission_category=ToolPermissionCategory.READ_ONLY,
                purpose="Search repository knowledge.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Search",
                kind="knowledge",
                owner_role=RoleName.KNOWLEDGE,
                expected_output="Grounded answer.",
                success_criteria="Citations are returned.",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="knowledge_answer",
            required_fields=["answer"],
        ),
    )


def test_for_task_is_lazy_until_first_write(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    assert not workspace.root.exists()
    assert workspace.read_checkpoint() is None
    assert workspace.list_evidence() == []


def test_write_intent_creates_layout_and_roundtrips(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    workspace.write_intent(
        intent_text="Locate Firebase config.",
        language="en",
        must_touch_files=["app/google-services.json"],
        scenario="process_question",
    )

    assert (workspace.root / "intent.md").is_file()
    assert (workspace.root / "evidence" / "snippets").is_dir()
    assert (workspace_root / "_global" / "memory" / "codebase_facts").is_dir()
    assert workspace.read_intent()["must_touch_files"] == ["app/google-services.json"]


def test_rejects_unsafe_task_id(workspace_root: Path) -> None:
    with pytest.raises(ValueError):
        TaskWorkspace.for_task("../escape", _settings(workspace_root))


def test_add_and_list_evidence_roundtrip(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))
    workspace.add_evidence([_evidence()])

    items = workspace.list_evidence()

    assert len(items) == 1
    assert items[0].source == "rag_lexical"
    assert items[0].file_path == "src/auth.py"
    assert items[0].snippet == "login failure"


def test_large_snippet_is_stored_out_of_band_and_hydrated(workspace_root: Path) -> None:
    snippet = "x" * 32
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root, threshold=10))

    workspace.add_evidence([_evidence(snippet=snippet)])

    manifest = json.loads((workspace.root / "evidence" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["snippet"] == "x" * 10
    assert manifest[0]["metadata"]["snippet_file"] == "evidence/snippets/ev-1.txt"
    assert (workspace.root / "evidence" / "snippets" / "ev-1.txt").read_text(encoding="utf-8") == snippet
    assert workspace.list_evidence()[0].snippet == snippet

    workspace.add_evidence([_evidence("ev-2", file_path="src/other.py", snippet="short")])
    manifest = json.loads((workspace.root / "evidence" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest[0]["snippet"] == "x" * 10
    assert manifest[1]["snippet"] == "short"


def test_list_evidence_filters_by_source(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))
    workspace.add_evidence(
        [
            _evidence("ev-1", source="rag_lexical"),
            _evidence("ev-2", source="cc_read", file_path="src/view.py"),
        ]
    )

    items = workspace.list_evidence(source_filter=["cc_read"])

    assert [item.id for item in items] == ["ev-2"]


def test_unsafe_evidence_path_is_rejected_by_workspace_api(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))
    unsafe = EvidenceItem.model_construct(
        id="ev-1",
        source="cc_read",
        file_path="../../../etc/passwd",
        line_start=1,
        line_end=1,
        snippet="secret",
        confidence=1.0,
        metadata={},
    )

    with pytest.raises(ValueError):
        workspace.add_evidence([unsafe])


def test_checkpoint_roundtrip(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    workspace.write_checkpoint(stage_completed="plan", next_stage="codegen", resume_args={"plan_id": "p1"})

    assert workspace.has_checkpoint()
    assert workspace.read_checkpoint()["stage_completed"] == "plan"
    assert workspace.read_checkpoint()["resume_args"] == {"plan_id": "p1"}


def test_plan_roundtrip_and_history(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    workspace.write_plan(plan_payload=_plan(), reason="initial")

    assert workspace.read_plan()["objective"] == "Answer the repository question."
    assert (workspace.root / "plan" / "current.md").read_text(encoding="utf-8").startswith("# Current Plan")
    history = (workspace.root / "plan" / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(history) == 1
    assert json.loads(history[0])["reason"] == "initial"


def test_attempt_writers_and_next_index(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    assert workspace.next_attempt_index() == 1
    workspace.write_attempt_diff(1, "diff --git a/a b/a\n")
    workspace.write_attempt_review(1, report_dict={"blocked": False}, narrative="passed")
    workspace.write_attempt_compile(1, {"passed": True, "errors": []})

    attempt = workspace.root / "attempts" / "001"
    assert (attempt / "diff.patch").is_file()
    assert json.loads((attempt / "review.json").read_text(encoding="utf-8")) == {"blocked": False}
    assert json.loads((attempt / "compile.json").read_text(encoding="utf-8"))["passed"] is True
    assert workspace.next_attempt_index() == 2


def test_audit_jsonl_append(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))

    workspace.append_audit("intake", {"ok": True})
    workspace.append_audit("plan", {"ok": True})

    lines = (workspace.root / "audit.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert [json.loads(line)["event_name"] for line in lines] == ["intake", "plan"]


def test_archive_creates_tarball(workspace_root: Path) -> None:
    workspace = TaskWorkspace.for_task("task-1", _settings(workspace_root))
    workspace.write_intent(intent_text="hello", language="en", must_touch_files=[], scenario="process_question")

    workspace.archive()

    assert (workspace_root / "_archive" / "task-1.tar.gz").is_file()


def test_sweep_deletes_old_terminal_and_preserves_active_or_recent(workspace_root: Path) -> None:
    settings = _settings(workspace_root)
    old_terminal = TaskWorkspace.for_task("task-old", settings)
    active = TaskWorkspace.for_task("task-active", settings)
    recent = TaskWorkspace.for_task("task-recent", settings)
    for workspace in (old_terminal, active, recent):
        workspace.write_intent(intent_text="x", language="en", must_touch_files=[], scenario="process_question")

    old_time = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
    os.utime(old_terminal.root, (old_time, old_time))
    os.utime(active.root, (old_time, old_time))

    counts = sweep_task_workspaces(
        settings=settings,
        task_statuses={
            "task-old": ("completed", None),
            "task-active": ("executing", None),
            "task-recent": ("completed", None),
        },
    )

    assert counts["deleted"] == 1
    assert not old_terminal.root.exists()
    assert active.root.exists()
    assert recent.root.exists()
