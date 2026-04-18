"""Adversarial tests: verify each defense line blocks evasion attempts.

T-040 defense lines:
  1. Translation anchors (extract_quoted_anchors)
  2. Orchestrator pre-check (anchors_missing_from_tree rule)
  3. Codegen prompt refusal (tested via prompt inspection, not here)
  4. Spec conformance validator (shadow, hit_delta, must_touch, planner_must_touch)
  5. Compile gate (syntax check)

This file tests adversarial evasion scenarios that the validator MUST block.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.spec_conformance import (
    ConformanceReport,
    _classify_files_in_diff,
    _extract_quoted_anchors,
    check_spec_conformance,
)
from app.services.compile_gate import run_compile_gate


# ---- helpers ---- #


def _new_file_diff(path: str, body: str) -> str:
    lines = body.splitlines()
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
    )
    return header + "".join(f"+{line}\n" for line in lines)


def _modify_diff(path: str, minus: list[str], plus: list[str]) -> str:
    total = max(len(minus), len(plus)) or 1
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{total} +1,{total} @@\n"
    )
    body = "".join(f"-{l}\n" for l in minus) + "".join(f"+{l}\n" for l in plus)
    return header + body


@pytest.fixture()
def real_tree(tmp_path: Path) -> Path:
    """Simulates HostedDashboard structure with master1 and currentUser."""
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "pages").mkdir(parents=True)

    (tmp_path / "src" / "data" / "mockUsers.js").write_text(
        'export const mockUsers = [\n'
        '  { id: "staff1", name: "Staff" },\n'
        '  { id: "master1", name: "Master" },\n'
        '];\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "pages" / "Dashboard.js").write_text(
        'import React from "react";\n'
        'const currentUser = JSON.parse(localStorage.getItem("currentUser"));\n'
        'export default function Dashboard() {\n'
        '  return <div>{currentUser?.name}</div>;\n'
        '}\n',
        encoding="utf-8",
    )
    return tmp_path


REQ = (
    'delete the array element with id "master1" from src/data/mockUsers.js, '
    'and move the top-level currentUser read into a useEffect in '
    'src/pages/Dashboard.js. Touch only those two files.'
)


# ======================== DEFENSE LINE 1: ANCHOR EXTRACTION ======================== #


class TestDefenseLine1Anchors:
    """Verify anchor extraction catches all critical identifiers."""

    def test_quoted_anchor_extracted(self) -> None:
        anchors = _extract_quoted_anchors(REQ)
        anchor_lower = {a.lower() for a in anchors}
        assert "master1" in anchor_lower

    def test_camelcase_anchor_extracted(self) -> None:
        anchors = _extract_quoted_anchors("fix currentUser in Dashboard.js")
        anchor_lower = {a.lower() for a in anchors}
        assert "currentuser" in anchor_lower

    def test_snakecase_anchor_extracted(self) -> None:
        anchors = _extract_quoted_anchors("remove mock_users from config")
        anchor_lower = {a.lower() for a in anchors}
        assert "mock_users" in anchor_lower

    def test_no_false_positive_on_common_english(self) -> None:
        anchors = _extract_quoted_anchors("Please remove the old code and fix bugs")
        assert len(anchors) == 0, f"Should not extract common English words, got {anchors}"

    def test_multiple_anchors_all_extracted(self) -> None:
        anchors = _extract_quoted_anchors(
            'delete "master1" and "staff1" from mockUsers'
        )
        anchor_lower = {a.lower() for a in anchors}
        assert "master1" in anchor_lower
        assert "staff1" in anchor_lower


# ======================== DEFENSE LINE 2: ANCHORS MISSING FROM TREE =============== #


class TestDefenseLine2PreCheck:
    """When ALL anchors are absent from tree, block the task."""

    def test_all_anchors_missing_blocks(self, real_tree: Path) -> None:
        diff = _new_file_diff("src/wrapper.js", "// empty")
        report = check_spec_conformance(
            request_text='Remove "ghostUser" and "phantomAdmin" from code',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        rules = {f.rule for f in report.findings}
        assert "anchors_missing_from_tree" in rules
        assert report.blocked

    def test_partial_anchors_present_does_not_trigger(self, real_tree: Path) -> None:
        diff = _new_file_diff("src/wrapper.js", "// empty")
        report = check_spec_conformance(
            request_text='Remove "master1" and "ghostUser" from code',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        rules = {f.rule for f in report.findings}
        assert "anchors_missing_from_tree" not in rules

    def test_non_destructive_request_skips_check(self, real_tree: Path) -> None:
        diff = _new_file_diff("src/wrapper.js", "// empty")
        report = check_spec_conformance(
            request_text='explain what "ghostUser" does',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        rules = {f.rule for f in report.findings}
        assert "anchors_missing_from_tree" not in rules


# ======================== DEFENSE LINE 4a: SHADOW IMPLEMENTATION =================== #


class TestDefenseLine4aShadow:
    """Block patches that only create new files for destructive requests."""

    def test_wrapper_file_evasion_blocked(self, real_tree: Path) -> None:
        """Model creates a 'clean' wrapper instead of editing dirty source."""
        diff = _new_file_diff(
            "src/data/cleanMockUsers.js",
            'export const mockUsers = [\n  { id: "staff1" },\n];\n',
        )
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        assert report.blocked
        rules = {f.rule for f in report.findings}
        assert "shadow_implementation" in rules

    def test_multiple_new_files_evasion_blocked(self, real_tree: Path) -> None:
        """Model creates multiple new 'replacement' files."""
        diff = _new_file_diff("src/data/mockUsersV2.js", "// clean")
        diff += _new_file_diff("src/pages/DashboardV2.js", "// clean")
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        assert report.blocked
        rules = {f.rule for f in report.findings}
        assert "shadow_implementation" in rules

    def test_mix_modify_and_create_passes_shadow(self, real_tree: Path) -> None:
        """If at least one existing file is modified, shadow doesn't fire."""
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        diff += _new_file_diff("src/utils/helper.js", "// utility")
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        shadow = [f for f in report.findings if f.rule == "shadow_implementation"]
        assert shadow == []


# ======================== DEFENSE LINE 4b: HIT DELTA ============================== #


class TestDefenseLine4bHitDelta:
    """Anchor occurrence count must decrease for destructive requests."""

    def test_no_decrease_blocks(self, real_tree: Path) -> None:
        """Patch touches the file but doesn't actually remove the anchor."""
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "staff1", name: "Staff" },'],
            ['  { id: "staff1", name: "Staff Member" },'],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        hit_delta = [f for f in report.findings if f.rule == "hit_delta"]
        assert len(hit_delta) > 0
        assert any(f.severity == "block" for f in hit_delta)

    def test_anchor_moved_not_removed_blocks(self, real_tree: Path) -> None:
        """Anchor removed from one line but re-added on another — net zero."""
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            ['  // moved: { id: "master1", name: "Master" }'],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        hit_delta = [f for f in report.findings if f.rule == "hit_delta"]
        block_deltas = [f for f in hit_delta if f.severity == "block"]
        assert len(block_deltas) > 0, "net-zero anchor count should block"

    def test_actual_removal_passes(self, real_tree: Path) -> None:
        """Properly removing the anchor line should pass hit_delta."""
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        hit_delta_blocks = [
            f for f in report.findings
            if f.rule == "hit_delta" and f.severity == "block"
        ]
        assert hit_delta_blocks == []

    def test_aggregate_logic_incidental_anchor_warns_not_blocks(self, real_tree: Path) -> None:
        """Primary anchor decreased → incidental anchors that didn't decrease are warn only."""
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js, keep mockUsers intact',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        hit_delta = [f for f in report.findings if f.rule == "hit_delta"]
        block_deltas = [f for f in hit_delta if f.severity == "block"]
        assert block_deltas == [], "incidental anchors should be warn when primary decreased"


# ======================== DEFENSE LINE 4c: MUST TOUCH ============================= #


class TestDefenseLine4cMustTouch:
    """Diff must touch files that actually contain the anchor."""

    def test_unrelated_file_only_blocked(self, real_tree: Path) -> None:
        """Editing an unrelated file when anchor lives in another."""
        diff = _modify_diff(
            "src/pages/Dashboard.js",
            ['  return <div>{currentUser?.name}</div>;'],
            ['  return <div>Hello</div>;'],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        must_touch = [f for f in report.findings if f.rule == "must_touch"]
        assert len(must_touch) > 0

    def test_correct_file_touched_passes(self, real_tree: Path) -> None:
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        report = check_spec_conformance(
            request_text='delete "master1" from mockUsers.js',
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
        )
        must_touch = [f for f in report.findings if f.rule == "must_touch"]
        assert must_touch == []


# ======================== DEFENSE LINE 4d: PLANNER MUST TOUCH ===================== #


class TestDefenseLine4dPlannerMustTouch:
    """Planner committed to touching specific files — diff must honor that."""

    def test_planner_file_missing_from_diff_blocked(self, real_tree: Path) -> None:
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
            must_touch_files=["src/data/mockUsers.js", "src/pages/Dashboard.js"],
        )
        planner = [f for f in report.findings if f.rule == "planner_must_touch"]
        assert len(planner) > 0, "Dashboard.js committed but not in diff"
        assert report.blocked

    def test_all_planner_files_touched_passes(self, real_tree: Path) -> None:
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        diff += _modify_diff(
            "src/pages/Dashboard.js",
            ['const currentUser = JSON.parse(localStorage.getItem("currentUser"));'],
            [],
        )
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
            must_touch_files=["src/data/mockUsers.js", "src/pages/Dashboard.js"],
        )
        planner = [f for f in report.findings if f.rule == "planner_must_touch"]
        assert planner == []


# ======================== DEFENSE LINE 5: COMPILE GATE ============================ #


class TestDefenseLine5CompileGate:
    """Syntax errors in patched files must be caught."""

    def test_broken_js_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("function f() {\n", encoding="utf-8")
        result = run_compile_gate(sandbox_dir=tmp_path, changed_files=["app.js"])
        assert not result.passed

    def test_broken_py_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("class Foo\n  pass\n", encoding="utf-8")
        result = run_compile_gate(sandbox_dir=tmp_path, changed_files=["app.py"])
        assert not result.passed

    def test_valid_code_passes(self, tmp_path: Path) -> None:
        (tmp_path / "app.js").write_text("const x = 1;\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        result = run_compile_gate(
            sandbox_dir=tmp_path, changed_files=["app.js", "app.py"]
        )
        assert result.passed

    def test_jsx_syntax_error_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "comp.jsx").write_text(
            "export default function() { return <div>\n", encoding="utf-8"
        )
        result = run_compile_gate(sandbox_dir=tmp_path, changed_files=["comp.jsx"])
        assert not result.passed


# ======================== COMBINED: CORRECT PATCH PASSES ALL ====================== #


class TestCorrectPatchPassesAll:
    """A properly constructed correct patch must pass every gate."""

    def test_correct_two_file_patch_passes(self, real_tree: Path) -> None:
        diff = _modify_diff(
            "src/data/mockUsers.js",
            ['  { id: "master1", name: "Master" },'],
            [],
        )
        diff += _modify_diff(
            "src/pages/Dashboard.js",
            ['const currentUser = JSON.parse(localStorage.getItem("currentUser"));'],
            [],
        )
        report = check_spec_conformance(
            request_text=REQ,
            normalized_request=None,
            diff=diff,
            source_tree=real_tree,
            must_touch_files=["src/data/mockUsers.js", "src/pages/Dashboard.js"],
        )
        assert not report.blocked, (
            f"Correct patch should pass all gates, got: "
            f"{[(f.rule, f.severity, f.message) for f in report.findings if f.severity == 'block']}"
        )


# ======================== DIFF PARSER EDGE CASES ================================== #


class TestDiffParserEdgeCases:
    """Ensure diff parsing handles all formats the pipeline can produce."""

    def test_git_style_diff(self) -> None:
        diff = (
            "diff --git a/foo.js b/foo.js\n"
            "--- a/foo.js\n+++ b/foo.js\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        )
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("foo.js") == "modify"

    def test_standard_unified_diff(self) -> None:
        diff = "--- a/bar.py\n+++ b/bar.py\n@@ -1 +1 @@\n-old\n+new\n"
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("bar.py") == "modify"

    def test_new_file_git_style(self) -> None:
        diff = (
            "diff --git a/new.js b/new.js\n"
            "new file mode 100644\n"
            "--- /dev/null\n+++ b/new.js\n"
            "@@ -0,0 +1 @@\n+hello\n"
        )
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("new.js") == "create"

    def test_new_file_unified_style(self) -> None:
        diff = "--- /dev/null\n+++ b/new.js\n@@ -0,0 +1 @@\n+hello\n"
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("new.js") == "create"

    def test_delete_file_unified_style(self) -> None:
        diff = "--- a/old.js\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("old.js") == "delete"

    def test_empty_diff(self) -> None:
        assert _classify_files_in_diff("") == {}

    def test_multi_file_unified_diff(self) -> None:
        diff = (
            "--- a/a.js\n+++ b/a.js\n@@ -1 +1 @@\n-x\n+y\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
        )
        shapes = _classify_files_in_diff(diff)
        assert shapes.get("a.js") == "modify"
        assert shapes.get("b.py") == "modify"
