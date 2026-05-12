"""Tests for T-041 anti-hallucination defense matrix (8 new mechanisms)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


# ---- helpers ---- #


def _new_file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        + "".join(f"+{l}\n" for l in lines)
    )


def _modify_diff(path: str, minus: list[str], plus: list[str]) -> str:
    total = max(len(minus), len(plus)) or 1
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1,{total} +1,{total} @@\n"
        + "".join(f"-{l}\n" for l in minus)
        + "".join(f"+{l}\n" for l in plus)
    )


@pytest.fixture()
def source_tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "data" / "mockUsers.js").write_text(
        'export const mockUsers = [{ id: "master1" }, { id: "staff1" }];\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "pages" / "Dashboard.js").write_text(
        'const currentUser = JSON.parse(localStorage.getItem("currentUser"));\n',
        encoding="utf-8",
    )
    return tmp_path


# ======================== T-041-01: Evidence Bundle ========================= #


class TestEvidenceBundle:
    def test_sufficient_when_anchors_found(self, source_tree: Path) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            source_tree=source_tree,
            has_destructive_verb=True,
        )
        assert result.verdict == "sufficient"
        assert result.coverage_score > 0
        assert len(result.must_touch_files) > 0

    def test_insufficient_when_no_anchors_found(self, source_tree: Path) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text='delete "ghostUser" from code',
            normalized_request=None,
            source_tree=source_tree,
            has_destructive_verb=True,
        )
        assert result.verdict == "insufficient"

    def test_skip_when_no_source_tree(self) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text='delete "master1"',
            normalized_request=None,
            source_tree=None,
            has_destructive_verb=True,
        )
        assert result.verdict == "skip"

    def test_skip_when_no_anchors(self, source_tree: Path) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text="add a new feature",
            normalized_request=None,
            source_tree=source_tree,
            has_destructive_verb=False,
        )
        assert result.verdict == "skip"

    def test_forbidden_files_detected(self, source_tree: Path) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        (source_tree / "migrations").mkdir(exist_ok=True)
        (source_tree / "migrations" / "001.sql").write_text("-- master1 migration\n", encoding="utf-8")
        result = build_evidence_bundle(
            request_text='delete "master1"',
            normalized_request=None,
            source_tree=source_tree,
            has_destructive_verb=True,
        )
        assert any("migration" in f for f in result.forbidden_files)

    def test_planner_must_touch_merged(self, source_tree: Path) -> None:
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text='delete "master1"',
            normalized_request=None,
            source_tree=source_tree,
            planner_must_touch=["src/extra.js"],
            has_destructive_verb=True,
        )
        assert "src/extra.js" in result.must_touch_files

    def test_payload_serializable(self, source_tree: Path) -> None:
        import json
        from app.services.evidence_bundle import build_evidence_bundle

        result = build_evidence_bundle(
            request_text='delete "master1"',
            normalized_request=None,
            source_tree=source_tree,
            has_destructive_verb=True,
        )
        json.dumps(result.to_payload())


# ==================== T-041-02/03: Diff Shape Checker ======================= #


class TestDiffShapeChecker:
    def test_delete_task_too_many_files_blocks(self) -> None:
        from app.services.diff_shape_checker import check_diff_shape

        shapes = {f"src/f{i}.js": "modify" for i in range(15)}
        report = check_diff_shape(
            request_text="delete master1 from mockUsers",
            diff="x" * 100,
            file_shapes=shapes,
        )
        assert report.blocked
        rules = {f.rule for f in report.findings}
        assert "shape_file_count" in rules

    def test_fix_task_mostly_creates_warns(self) -> None:
        from app.services.diff_shape_checker import check_diff_shape

        shapes = {
            "src/new1.js": "create",
            "src/new2.js": "create",
            "src/new3.js": "create",
            "src/old.js": "modify",
        }
        report = check_diff_shape(
            request_text="fix the login bug",
            diff="x" * 100,
            file_shapes=shapes,
        )
        rules = {f.rule for f in report.findings}
        assert "shape_overreach" in rules

    def test_delete_no_modify_warns(self) -> None:
        from app.services.diff_shape_checker import check_diff_shape

        shapes = {"src/new.js": "create"}
        report = check_diff_shape(
            request_text="delete the old config",
            diff="x",
            file_shapes=shapes,
        )
        rules = {f.rule for f in report.findings}
        assert "shape_intent_mismatch" in rules

    def test_reasonable_fix_passes(self) -> None:
        from app.services.diff_shape_checker import check_diff_shape

        shapes = {"src/bug.js": "modify"}
        report = check_diff_shape(
            request_text="fix the null pointer",
            diff="x" * 50,
            file_shapes=shapes,
        )
        assert not report.blocked

    def test_existing_file_first_high_ratio_blocks(self) -> None:
        from app.services.diff_shape_checker import check_diff_shape

        shapes = {
            "src/a.js": "create",
            "src/b.js": "create",
            "src/c.js": "create",
            "src/d.js": "create",
            "src/e.js": "create",
        }
        report = check_diff_shape(
            request_text="fix the display bug",
            diff="x" * 50,
            file_shapes=shapes,
        )
        rules = {f.rule for f in report.findings}
        assert "existing_file_first" in rules

    def test_task_type_classification(self) -> None:
        from app.services.diff_shape_checker import _classify_task_type

        assert _classify_task_type("delete master1") == "delete"
        assert _classify_task_type("fix the bug") == "fix"
        assert _classify_task_type("rename getUserName") == "rename"
        assert _classify_task_type("add a new feature") == "default"


# ==================== T-041-05: Symbol + Reference Gate ===================== #


class TestSymbolReferenceGate:
    def test_no_findings_on_simple_modify(self, source_tree: Path) -> None:
        from app.services.symbol_reference_gate import check_symbol_references

        diff = _modify_diff("src/data/mockUsers.js", ['  { id: "master1" },'], [])
        report = check_symbol_references(diff=diff, source_tree=source_tree)
        assert report.verdict == "pass"

    def test_rename_detection(self) -> None:
        from app.services.symbol_reference_gate import _looks_like_rename

        assert _looks_like_rename("getUserName", "getUserFullName")
        assert _looks_like_rename("mockUsers", "mockUsersList")
        assert not _looks_like_rename("foo", "bar")

    def test_skip_when_no_source_tree(self) -> None:
        from app.services.symbol_reference_gate import check_symbol_references

        diff = "-function oldFunc() {}\n+function newFunc() {}"
        report = check_symbol_references(diff=diff, source_tree=None)
        assert report.verdict == "pass"


# =================== T-041-06: Failing Test Gate ============================ #


class TestFailingTestGate:
    def test_behavior_bug_detected(self) -> None:
        from app.services.failing_test_gate import _is_behavior_bug

        assert _is_behavior_bug("fix the stale display on dashboard")
        assert _is_behavior_bug("login session bug: wrong user shown")
        assert _is_behavior_bug("permission error on admin page")
        assert not _is_behavior_bug("add a new feature module")

    def test_behavior_bug_no_test_warns(self) -> None:
        from app.services.failing_test_gate import check_failing_test_gate

        report = check_failing_test_gate(
            request_text="fix the stale display bug",
            file_shapes={"src/Dashboard.js": "modify"},
            test_result=None,
        )
        assert report.is_behavior_bug
        assert report.verdict == "warn"
        rules = {f.rule for f in report.findings}
        assert "behavior_no_test_evidence" in rules

    def test_behavior_bug_with_test_passes(self) -> None:
        from app.services.failing_test_gate import check_failing_test_gate

        report = check_failing_test_gate(
            request_text="fix the stale display bug",
            file_shapes={
                "src/Dashboard.js": "modify",
                "tests/test_dashboard.js": "create",
            },
            test_result={"overall_passed": True, "status": "passed"},
        )
        assert report.verdict == "pass"

    def test_non_behavior_task_skipped(self) -> None:
        from app.services.failing_test_gate import check_failing_test_gate

        report = check_failing_test_gate(
            request_text="add new utility function",
            file_shapes={"src/utils.js": "create"},
            test_result=None,
        )
        assert not report.is_behavior_bug
        assert report.verdict == "pass"

    def test_behavior_bug_test_present_but_failing_blocks(self) -> None:
        from app.services.failing_test_gate import check_failing_test_gate

        report = check_failing_test_gate(
            request_text="fix the broken login",
            file_shapes={
                "src/auth.js": "modify",
                "tests/test_auth.js": "modify",
            },
            test_result={"overall_passed": False, "status": "failed"},
        )
        assert report.verdict == "block"


# =================== T-041-07: Runtime Validation =========================== #


class TestRuntimeValidation:
    def test_valid_py_passes(self, tmp_path: Path) -> None:
        from app.services.runtime_validation import check_runtime_validity

        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        report = check_runtime_validity(
            changed_files=["app.py"],
            sandbox_dir=tmp_path,
        )
        assert report.verdict == "pass"

    def test_syntax_error_warns(self, tmp_path: Path) -> None:
        from app.services.runtime_validation import check_runtime_validity

        (tmp_path / "bad.py").write_text("def f(\n", encoding="utf-8")
        report = check_runtime_validity(
            changed_files=["bad.py"],
            sandbox_dir=tmp_path,
        )
        findings = [f for f in report.findings if f.rule == "runtime_py_import"]
        assert len(findings) > 0

    def test_browser_smoke_placeholder(self) -> None:
        from app.services.runtime_validation import check_browser_smoke

        report = check_browser_smoke()
        assert report.verdict == "pass"
        assert any(f.rule == "browser_smoke_skipped" for f in report.findings)


# ================ T-041-08: Goal Decomposition ============================== #


class TestGoalDecomposition:
    def test_decompose_multi_goal(self) -> None:
        from app.services.goal_decomposition import _decompose_goals

        goals = _decompose_goals(
            'delete "master1" from mockUsers.js, '
            'and move currentUser read into useEffect in Dashboard.js'
        )
        assert len(goals) >= 2

    def test_per_file_justification(self) -> None:
        from app.services.goal_decomposition import decompose_and_verify

        report = decompose_and_verify(
            request_text='delete "master1" from mockUsers.js',
            diff=_modify_diff("src/data/mockUsers.js", ["master1"], []),
            file_shapes={"src/data/mockUsers.js": "modify"},
            source_tree=None,
        )
        assert all(fj.justified for fj in report.file_justifications)
        assert report.unjustified_files == []

    def test_unjustified_file_detected(self) -> None:
        from app.services.goal_decomposition import decompose_and_verify

        report = decompose_and_verify(
            request_text='delete "master1" from mockUsers.js',
            diff="",
            file_shapes={
                "src/data/mockUsers.js": "modify",
                "src/unrelated/random.js": "create",
            },
            source_tree=None,
        )
        assert "src/unrelated/random.js" in report.unjustified_files

    def test_support_files_auto_justified(self) -> None:
        from app.services.goal_decomposition import decompose_and_verify

        report = decompose_and_verify(
            request_text='fix the bug',
            diff="",
            file_shapes={
                "src/bug.js": "modify",
                "package.json": "modify",
                "tests/test_bug.js": "create",
            },
            source_tree=None,
        )
        unjustified = [f for f in report.unjustified_files if f != "src/bug.js"]
        assert "package.json" not in unjustified
        assert "tests/test_bug.js" not in unjustified

    def test_goal_with_attestation(self, source_tree: Path) -> None:
        from app.services.goal_decomposition import decompose_and_verify

        attestation = {
            "anchors": [
                {"anchor": "master1", "status": "achieved", "count_before": 1, "count_after": 0},
            ],
            "all_goals_met": True,
        }
        report = decompose_and_verify(
            request_text='delete "master1" from mockUsers.js',
            diff=_modify_diff("src/data/mockUsers.js", ["master1"], []),
            file_shapes={"src/data/mockUsers.js": "modify"},
            source_tree=source_tree,
            attestation=attestation,
        )
        achieved = [g for g in report.sub_goals if g.status == "achieved"]
        assert len(achieved) > 0

    def test_payload_serializable(self) -> None:
        import json
        from app.services.goal_decomposition import decompose_and_verify

        report = decompose_and_verify(
            request_text='delete "master1"',
            diff="",
            file_shapes={"src/a.js": "modify"},
            source_tree=None,
        )
        json.dumps(report.to_payload())
