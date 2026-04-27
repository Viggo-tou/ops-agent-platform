from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from app.agents.schemas import (
    FinalOutputContract,
    GeneratedPlan,
    PlanStep,
    PlanTool,
)
from app.core.config import Settings
from app.core.enums import RiskLevel, RoleName, ToolPermissionCategory
from app.schemas.evidence import EvidenceItem
from app.schemas.knowledge import KnowledgeClaim
from app.services.evidence_chain import check_evidence_chain, _path_in, _paths_match
from app.services.task_workspace import TaskWorkspace


BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def workspace_root() -> Path:
    if os.name != "nt":
        root = Path(tempfile.mkdtemp(prefix="evidence-chain-", dir=str(BACKEND_ROOT)))
    else:
        original_mkdir = tempfile._os.mkdir

        def mkdir_with_write_access(path: str, mode: int = 0o777) -> None:
            original_mkdir(path, 0o777)

        tempfile._os.mkdir = mkdir_with_write_access
        try:
            root = Path(tempfile.mkdtemp(prefix="evidence-chain-", dir=str(BACKEND_ROOT)))
        finally:
            tempfile._os.mkdir = original_mkdir
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


MODIFY_A = (
    "diff --git a/src/a.py b/src/a.py\n"
    "index aaa..bbb 100644\n"
    "--- a/src/a.py\n"
    "+++ b/src/a.py\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
)


def _settings(root: Path, *, min_confident: int = 3) -> Settings:
    return Settings(
        agent_workspace_root=str(root),
        evidence_chain_min_confident_claims=min_confident,
    )


def _workspace(root: Path, *, with_intent: bool = True) -> TaskWorkspace:
    workspace = TaskWorkspace.for_task("task-1", _settings(root))
    if with_intent:
        workspace.write_intent(
            intent_text="Implement TEST-1.",
            request_text="Implement TEST-1.",
            jira_issue_body="Summary: update src/a.py\n\nDescription: edit src/a.py",
            jira_issue_key="TEST-1",
            language="en",
            must_touch_files=[],
            scenario="jira_issue_develop",
        )
    return workspace


def _plan(*, expected_new_files: list[str] | None = None) -> GeneratedPlan:
    return GeneratedPlan(
        task_id="task-1",
        objective="Update implementation.",
        request_summary="Update implementation.",
        scenario="jira_issue_develop",
        change_summary="Modify files.",
        change_explanation="Modify files according to the issue.",
        assumptions=[],
        missing_information=[],
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        approval_reasons=[],
        affected_code_locations=[],
        must_touch_files=[],
        expected_new_files=expected_new_files or [],
        tools=[
            PlanTool(
                tool_name="codegen.generate_patch",
                permission_category=ToolPermissionCategory.WRITE,
                purpose="Generate the patch.",
            )
        ],
        steps=[
            PlanStep(
                step_id="s1",
                title="Patch",
                kind="action",
                owner_role=RoleName.ACTION,
                expected_output="Diff",
                success_criteria="Diff generated",
            )
        ],
        final_output_contract=FinalOutputContract(
            type="jira_issue_develop",
            required_fields=["status"],
        ),
    )


def _evidence(
    file_path: str = "src/a.py",
    *,
    source: str = "cc_read",
    item_id: str = "ev-1",
) -> EvidenceItem:
    return EvidenceItem(
        id=item_id,
        source=source,  # type: ignore[arg-type]
        file_path=file_path,
        line_start=1,
        line_end=4,
        snippet="source snippet",
        chunk_kind="line_window",
    )


def _claims(count: int = 3) -> list[KnowledgeClaim]:
    return [
        KnowledgeClaim(text=f"Claim {index}", citation_indices=[0], confidence="high")
        for index in range(count)
    ]


def _attestation(*, files: list[str] | None = None) -> dict:
    return {
        "all_goals_met": True,
        "anchors": [
            {
                "anchor": "src/a.py",
                "before_count": 1,
                "after_count": 0,
                "modified_files": files or ["src/a.py"],
            }
        ],
    }


def test_happy_path_closes_chain(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence()])

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=_claims(),
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is True
    assert report.findings == []
    assert report.diagnostic["evidence_count"] == 1
    assert report.diagnostic["modified_files_with_evidence"] == ["src/a.py"]


def test_empty_intent_blocks(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root, with_intent=False)
    workspace.add_evidence([_evidence()])

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=_claims(),
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    assert any(f.rule == "intent_missing" and f.severity == "block" for f in report.findings)


def test_empty_evidence_blocks_as_weak(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=[],
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    assert any(f.rule == "evidence_weak" for f in report.findings)


def test_untracked_modified_file_blocks(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence("src/a.py")])
    diff = MODIFY_A.replace("src/a.py", "src/b.py")

    report = check_evidence_chain(
        workspace=workspace,
        diff=diff,
        plan=_plan(),
        claims=[],
        citations=[],
        attestation=None,
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    finding = next(f for f in report.findings if f.rule == "untracked_file")
    assert finding.file_path == "src/b.py"
    assert finding.severity == "block"


def test_planned_new_file_does_not_need_file_evidence(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence("docs/spec.md")])
    diff = (
        "diff --git a/src/new.py b/src/new.py\n"
        "new file mode 100644\n"
        "index 0000000..bbb\n"
        "--- /dev/null\n"
        "+++ b/src/new.py\n"
        "@@ -0,0 +1 @@\n"
        "+value = 1\n"
    )

    report = check_evidence_chain(
        workspace=workspace,
        diff=diff,
        plan=_plan(expected_new_files=["src/new.py"]),
        claims=[],
        citations=[],
        attestation=None,
        settings=_settings(workspace_root),
    )

    assert report.closed is True
    assert not any(f.rule == "untracked_file" and f.severity == "block" for f in report.findings)


def test_all_low_or_uncited_claims_block(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence()])
    claims = [
        KnowledgeClaim(text="Unsupported", citation_indices=[], confidence="low"),
        KnowledgeClaim(text="Also unsupported", citation_indices=[], confidence="low"),
    ]

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=claims,
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    assert any(f.rule == "ungrounded_claims" and f.severity == "block" for f in report.findings)


def test_some_ungrounded_claims_warn_when_min_confident_met(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence()])
    claims = _claims(3) + [
        KnowledgeClaim(text="Unsupported aside", citation_indices=[], confidence="low")
    ]

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=claims,
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is True
    assert any(f.rule == "ungrounded_claims" and f.severity == "warn" for f in report.findings)


def test_attestation_missing_warns_only(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence()])

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=_claims(),
        citations=[],
        attestation=None,
        settings=_settings(workspace_root),
    )

    assert report.closed is True
    assert any(f.rule == "attestation_missing" and f.severity == "warn" for f in report.findings)


def test_attestation_modified_files_must_be_subset_of_diff(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence()])

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=_claims(),
        citations=[],
        attestation=_attestation(files=["src/a.py", "src/ghost.py"]),
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    assert any(f.rule == "attestation_mismatch" and f.severity == "block" for f in report.findings)


def test_user_provided_evidence_alone_is_weak(workspace_root: Path) -> None:
    workspace = _workspace(workspace_root)
    workspace.add_evidence([_evidence(source="user_provided")])

    report = check_evidence_chain(
        workspace=workspace,
        diff=MODIFY_A,
        plan=_plan(),
        claims=[],
        citations=[],
        attestation=_attestation(),
        settings=_settings(workspace_root),
    )

    assert report.closed is False
    assert any(f.rule == "evidence_weak" and f.severity == "block" for f in report.findings)


# --- Path normalization (suffix-tolerant matching) -----------------------------------

def test_paths_match_handles_source_prefix_mismatch() -> None:
    # Diff has source-name prefix (codegen.repair re-emitted with it),
    # evidence has repo-relative path. Must match.
    assert _paths_match(
        "handyman-admin-dashboard/src/pages/JobManagement.js",
        "src/pages/JobManagement.js",
    )
    # Reverse direction also matches.
    assert _paths_match(
        "src/pages/JobManagement.js",
        "handyman-admin-dashboard/src/pages/JobManagement.js",
    )
    # Equal strings match.
    assert _paths_match("src/foo.js", "src/foo.js")


def test_paths_match_rejects_substring_collision() -> None:
    # Suffix matching must respect path-segment boundaries: "rc/foo.js"
    # is a substring of "src/foo.js" but NOT a path suffix.
    assert not _paths_match("src/foo.js", "rc/foo.js")
    # Empty inputs never match.
    assert not _paths_match("", "src/foo.js")
    assert not _paths_match("src/foo.js", "")
    # Different files don't match even with shared prefix.
    assert not _paths_match("src/a.js", "src/b.js")


def test_path_in_iterable_with_prefix_mismatch() -> None:
    evidence_paths = {"src/pages/Login.js", "src/firebase.js"}
    # Modified path with source-name prefix should still resolve.
    assert _path_in("handyman-admin-dashboard/src/pages/Login.js", evidence_paths)
    # Modified path without prefix matches identical evidence.
    assert _path_in("src/firebase.js", evidence_paths)
    # No match for unrelated file.
    assert not _path_in("src/components/Sidebar.js", evidence_paths)


def test_evidence_chain_accepts_diff_with_source_prefix_when_evidence_unprefixed(
    workspace_root: Path,
) -> None:
    """Regression for the P69-7 follow-up: codegen.repair patched files using
    ``handyman-admin-dashboard/src/...`` paths while evidence_items recorded
    ``src/...``. Without suffix-tolerant matching, evidence_chain blocked
    every repaired file as untracked.
    """
    workspace = _workspace(workspace_root)
    workspace.add_evidence([
        EvidenceItem(
            id="ev-1",
            source="rag_lexical",
            file_path="src/pages/JobManagement.js",
            line_start=1,
            line_end=10,
            snippet="...",
        )
    ])
    diff_with_prefix = (
        "diff --git a/handyman-admin-dashboard/src/pages/JobManagement.js "
        "b/handyman-admin-dashboard/src/pages/JobManagement.js\n"
        "--- a/handyman-admin-dashboard/src/pages/JobManagement.js\n"
        "+++ b/handyman-admin-dashboard/src/pages/JobManagement.js\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    attestation = {
        "goals": [],
        "modified_files": ["src/pages/JobManagement.js"],
    }
    report = check_evidence_chain(
        workspace=workspace,
        diff=diff_with_prefix,
        plan=_plan(),
        claims=[],
        citations=[],
        attestation=attestation,
        settings=_settings(workspace_root),
    )

    untracked_blocks = [
        f for f in report.findings
        if f.rule == "untracked_file" and f.severity == "block"
    ]
    assert untracked_blocks == [], (
        "modified file with evidence (path-prefixed) should not be flagged untracked: "
        f"{untracked_blocks}"
    )
