from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agents.schemas import (
    FinalOutputContract,
    GeneratedPlan,
    GeneratedSemanticTranslation,
    PlanCodeLocation,
    PlanStep,
    PlanTool,
)
from app.core.config import Settings
from app.core.enums import (
    ActorRole,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    ToolPermissionCategory,
    WorkflowStage,
)
from app.orchestrator.service import PrimaryOrchestrator
from app.schemas.evidence import EvidenceItem
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


def _settings(workspace_root: Path, sandbox_root: Path | None = None, source_root: Path | None = None) -> Settings:
    return Settings(
        agent_workspace_root=str(workspace_root),
        sandbox_base_dir=str(sandbox_root or workspace_root / "sandboxes"),
        knowledge_source_path=str(source_root) if source_root else None,
        develop_require_jira_approval=True,
        knowledge_synthesis_enabled=False,
        knowledge_rerank_enabled=False,
        knowledge_query_rewrite_enabled=False,
    )


def _knowledge_plan(task_id: str = "task-1") -> GeneratedPlan:
    return GeneratedPlan(
        task_id=task_id,
        objective="Locate Firebase configuration.",
        request_summary="Locate Firebase configuration.",
        scenario="process_question",
        change_summary="Search repository knowledge.",
        change_explanation="Answer with citations.",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        tools=[
            PlanTool(
                tool_name="knowledge.search",
                permission_category=ToolPermissionCategory.READ_ONLY,
                purpose="Search indexed repository knowledge.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Search",
                kind="knowledge",
                owner_role=RoleName.KNOWLEDGE,
                tool_name="knowledge.search",
                expected_output="Grounded answer.",
                success_criteria="Evidence is returned.",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="knowledge_answer",
            required_fields=["answer", "citations"],
        ),
    )


def _develop_plan(task_id: str = "task-dev") -> GeneratedPlan:
    return GeneratedPlan(
        task_id=task_id,
        objective='Remove "Minij" from src/a.py.',
        request_summary='Remove "Minij".',
        scenario="jira_issue_develop",
        change_summary="Remove obsolete anchor.",
        change_explanation="Update the existing source file.",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        affected_code_locations=[
            PlanCodeLocation(
                source_name="repo",
                relative_path="src/a.py",
                reason="Contains the anchor.",
            )
        ],
        must_touch_files=["src/a.py"],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate patch.",
            )
        ],
        steps=[
            PlanStep(
                step_id="step_1",
                title="Generate patch",
                kind="action",
                owner_role=RoleName.ACTION,
                tool_name="codegen.generate_patch",
                expected_output="Diff.",
                success_criteria="Anchor removed.",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _translation(task_id: str = "task-1") -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id=task_id,
        provider={"name": "mock"},
        normalized_request="Locate Firebase configuration.",
        intent="find_config",
        work_type="question",
        objective="Find Firebase configuration.",
        issue_key=None,
        issue_url=None,
        candidate_modules=[],
        search_queries=["firebase config"],
        constraints=[],
        requested_outputs=["answer"],
        grounding_terms=["firebase"],
        missing_information=[],
        confidence=0.9,
    )


def _task(task_id: str = "task-1", *, scenario: str = "process_question") -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        session_id=f"session-{task_id}",
        actor_name="tester",
        actor_role=ActorRole.EMPLOYEE,
        request_text="Locate Firebase configuration file(s) in the codebase.",
        scenario=scenario,
        status=TaskStatus.CREATED,
        workflow_stage=WorkflowStage.INTAKE,
        current_role=RoleName.PRIMARY,
        risk_level=RiskLevel.LOW,
        risk_category=RiskCategory.KNOWLEDGE_LOOKUP,
        translation_json=None,
        plan_json=None,
        review_json=None,
        latest_result_json=None,
        pending_approval=False,
        retry_count=0,
        trace_id=None,
    )


def _orchestrator(settings: Settings) -> PrimaryOrchestrator:
    orchestrator = PrimaryOrchestrator(db=Mock())
    orchestrator.tool_gateway.settings = settings
    orchestrator.tool_gateway.get_category = Mock(return_value=ToolPermissionCategory.READ_ONLY)
    orchestrator._sync_retry_count = Mock()
    orchestrator._anchor_precheck_fails = Mock(return_value=False)
    return orchestrator


def test_bootstrap_writes_intent_plan_checkpoint_and_audit() -> None:
    root = _writable_mkdtemp("workspace-hooks-")
    try:
        settings = _settings(root)
        orchestrator = _orchestrator(settings)
        task = _task()
        plan = _knowledge_plan(task.id)
        translation = _translation(task.id)
        orchestrator.semantic_translator.translate = Mock(
            return_value=SimpleNamespace(
                translation=translation,
                provider_name="mock",
                model_name="mock",
                used_fallback=False,
                fallback_reason=None,
            )
        )
        orchestrator.primary_agent.generate_plan = Mock(
            return_value=SimpleNamespace(
                plan=plan,
                provider_name="mock",
                model_name="mock",
                used_fallback=False,
                fallback_reason=None,
            )
        )
        orchestrator.reviewer_agent.review_plan = Mock(
            return_value=SimpleNamespace(
                review=SimpleNamespace(
                    verdict="rejected",
                    summary="stop after plan",
                    review_id="review-1",
                    model_dump=Mock(return_value={"verdict": "rejected", "summary": "stop after plan"}),
                )
            )
        )

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._bootstrap_task_impl(task=task, actor_name="tester")

        workspace = TaskWorkspace.for_task(task.id, settings)
        assert workspace.read_intent()["intent_text"] == task.request_text
        assert workspace.read_plan()["plan_id"] == plan.plan_id
        assert workspace.read_checkpoint()["stage_completed"] == "plan"
        audit_names = [
            json.loads(line)["event_name"]
            for line in (workspace.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert audit_names[:3] == ["intake", "semantic_translation", "plan"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_knowledge_execution_writes_evidence_manifest() -> None:
    root = _writable_mkdtemp("workspace-hooks-")
    try:
        settings = _settings(root)
        orchestrator = _orchestrator(settings)
        task = _task()
        plan = _knowledge_plan(task.id)
        task.plan_json = plan.model_dump(mode="json")
        task.translation_json = _translation(task.id).model_dump(mode="json")
        evidence = EvidenceItem(
            id="ev-1",
            source="rag_lexical",
            file_path="app/google-services.json",
            line_start=1,
            line_end=8,
            snippet='{"project_info": {}}',
            chunk_kind="line_window",
        )
        result = {
            "query": "firebase config",
            "answer": "Firebase config is in app/google-services.json.",
            "citations": [],
            "claims": [],
            "evidence_items": [evidence.model_dump(mode="json")],
            "answer_trace": {},
            "packaged_context": "",
        }
        orchestrator.action_agent.build_payload = Mock(return_value={"query": "firebase config", "top_k": 1})
        orchestrator.tool_gateway.execute = Mock(return_value=result)
        orchestrator.reviewer_agent.review_output = Mock(
            return_value=SimpleNamespace(
                review=SimpleNamespace(
                    verdict="approved",
                    model_dump=Mock(return_value={"verdict": "approved"}),
                )
            )
        )

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._execute_plan_impl(task=task, actor_name="tester", plan=plan)

        workspace = TaskWorkspace.for_task(task.id, settings)
        items = workspace.list_evidence()
        assert len(items) == 1
        assert items[0].file_path == "app/google-services.json"
        assert json.loads((workspace.root / "audit.jsonl").read_text(encoding="utf-8").splitlines()[0])[
            "event_name"
        ] == "knowledge.search"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_develop_pipeline_writes_attempt_artifacts_and_approval_checkpoint() -> None:
    root = _writable_mkdtemp("workspace-hooks-")
    source_root = root / "source"
    (source_root / "src").mkdir(parents=True)
    (source_root / "src" / "a.py").write_text('user = "Minij"\nother = "ok"\n', encoding="utf-8")
    try:
        settings = _settings(root / "workspace", root / "sandboxes", source_root)
        orchestrator = _orchestrator(settings)
        orchestrator.tool_gateway.get_category = Mock(return_value=ToolPermissionCategory.WRITE)
        orchestrator.db.add = Mock(side_effect=lambda obj: setattr(obj, "id", "approval-1"))
        orchestrator.db.flush = Mock()
        task = _task("task-dev", scenario="jira_issue_develop")
        task.request_text = 'Remove "Minij" from src/a.py.'
        task.risk_level = RiskLevel.MEDIUM
        task.risk_category = RiskCategory.CHANGE_MANAGEMENT
        task.translation_json = {
            "issue_key": "TEST-1",
            "normalized_request": 'Remove "Minij" from src/a.py.',
            "grounding_terms": ["Minij"],
        }
        plan = _develop_plan(task.id)
        task.plan_json = plan.model_dump(mode="json")
        diff = (
            "diff --git a/src/a.py b/src/a.py\n"
            "index aaa..bbb 100644\n"
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "@@ -1,2 +1,1 @@\n"
            '-user = "Minij"\n'
            ' other = "ok"\n'
        )
        orchestrator._gather_codegen_context = Mock(return_value={"src/a.py": 'user = "Minij"\nother = "ok"\n'})
        orchestrator._ensure_develop_sandbox = Mock(return_value={"status": "ready"})
        orchestrator.tool_gateway.execute = Mock(
            side_effect=[
                {
                    "diff": diff,
                    "summary": "Removed anchor.",
                    "files_changed": ["src/a.py"],
                    "provider_name": "mock",
                },
                {"status": "patched", "method": "git_apply"},
                {"status": "passed", "overall_passed": True, "failed_count": 0, "passed_count": 1},
                {"verdict": "pass", "violations": [], "rules_checked": 4, "duration_ms": 1},
            ]
        )

        with patch("app.orchestrator.service.record_event"), patch("app.orchestrator.service.set_task_status"):
            orchestrator._execute_develop_pipeline(task=task, actor_name="tester", plan=plan)

        workspace = TaskWorkspace.for_task(task.id, settings)
        assert (workspace.root / "attempts" / "001" / "diff.patch").read_text(
            encoding="utf-8"
        ).rstrip() == diff.rstrip()
        assert (workspace.root / "attempts" / "001" / "review.json").is_file()
        checkpoint = workspace.read_checkpoint()
        assert checkpoint["stage_completed"] == "attempt_001"
        assert checkpoint["next_stage"] == "approval"
        assert checkpoint["resume_args"]["approval_id"] == "approval-1"
    finally:
        shutil.rmtree(root, ignore_errors=True)
