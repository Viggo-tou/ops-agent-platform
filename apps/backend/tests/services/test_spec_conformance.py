"""Unit tests for spec_conformance service (T-038 P0)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.spec_conformance import (  # noqa: E402
    build_goal_attestation,
    check_spec_conformance,
)


# ------------------------------- fixtures --------------------------------- #


@pytest.fixture()
def tree_with_anchor(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.kt").write_text(
        "val user = \"Minij\"\nval admin = \"master admin\"\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "b.kt").write_text(
        "val other = \"Minij\"\n", encoding="utf-8"
    )
    (tmp_path / "src" / "c.kt").write_text(
        "fun unrelated() = 42\n", encoding="utf-8"
    )
    return tmp_path


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


def _modify_diff(path: str, minus_lines: list[str], plus_lines: list[str]) -> str:
    total_context = max(len(minus_lines), len(plus_lines)) or 1
    header = (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{total_context} +1,{total_context} @@\n"
    )
    body = "".join(f"-{l}\n" for l in minus_lines) + "".join(f"+{l}\n" for l in plus_lines)
    return header + body


# ---------------------------- shadow rule --------------------------------- #


def test_shadow_implementation_blocks_all_new_file_patch_for_destructive_verb(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new_session.kt", "object NewThing")
    report = check_spec_conformance(
        request_text="Remove hardcoded user Minij across the codebase",
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert report.blocked
    rules = {f.rule for f in report.findings}
    assert "shadow_implementation" in rules


def test_shadow_rule_passes_when_patch_modifies_existing_file(
    tree_with_anchor: Path,
) -> None:
    diff = _modify_diff("src/a.kt", ['val user = "Minij"'], ['val user = ""'])
    diff += _modify_diff("src/b.kt", ['val other = "Minij"'], ['val other = ""'])
    report = check_spec_conformance(
        request_text="Remove hardcoded Minij",
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert not report.blocked


def test_shadow_rule_skipped_without_destructive_verb(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new_feature.kt", "object Shiny")
    report = check_spec_conformance(
        request_text="Add a new feature X",
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert not report.blocked


# ---------------------------- hit_delta rule ------------------------------ #


def test_hit_delta_blocks_when_anchor_count_unchanged(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new.kt", 'val s = "unrelated"')
    report = check_spec_conformance(
        request_text='Clean up hardcoded "Minij" values',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert report.blocked
    rules = {f.rule for f in report.findings}
    assert "hit_delta" in rules


def test_hit_delta_passes_when_anchor_removed(tree_with_anchor: Path) -> None:
    diff = _modify_diff("src/a.kt", ['val user = "Minij"'], ['val user = ""'])
    diff += _modify_diff("src/b.kt", ['val other = "Minij"'], ['val other = ""'])
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    hit_delta_findings = [f for f in report.findings if f.rule == "hit_delta"]
    assert hit_delta_findings == []


def test_hit_delta_skipped_when_anchor_absent_from_tree(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new.kt", "nothing")
    report = check_spec_conformance(
        request_text='Remove "NeverPresent"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    hit_delta_findings = [f for f in report.findings if f.rule == "hit_delta"]
    assert hit_delta_findings == []


# ---------------------------- must_touch rule ----------------------------- #


def test_must_touch_blocks_when_diff_avoids_anchor_files(
    tree_with_anchor: Path,
) -> None:
    diff = _modify_diff("src/c.kt", ["fun unrelated() = 42"], ["fun unrelated() = 43"])
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    rules = {f.rule for f in report.findings}
    assert "must_touch" in rules
    assert report.blocked


def test_must_touch_passes_when_at_least_one_anchor_file_edited(
    tree_with_anchor: Path,
) -> None:
    diff = _modify_diff("src/a.kt", ['val user = "Minij"'], ['val user = ""'])
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    must_touch_findings = [f for f in report.findings if f.rule == "must_touch"]
    assert must_touch_findings == []


# ---------------------------- misc / safety ------------------------------- #


def test_no_findings_when_request_is_plain_non_destructive(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new.kt", "val x = 1")
    report = check_spec_conformance(
        request_text="Please add a new utility file",
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert not report.blocked


def test_empty_diff_produces_empty_pass(tree_with_anchor: Path) -> None:
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff="",
        source_tree=tree_with_anchor,
    )
    # empty diff shouldn't create new files, so shadow won't trigger, but
    # hit_delta / must_touch will — the anchor is still in the tree.
    assert report.blocked
    rules = {f.rule for f in report.findings}
    assert "must_touch" in rules


def test_no_source_tree_skips_anchor_rules() -> None:
    diff = _new_file_diff("src/new.kt", "val x = 1")
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=None,
    )
    # shadow still fires (destructive verb + 100% new files)
    rules = {f.rule for f in report.findings}
    assert "shadow_implementation" in rules
    # but hit_delta / must_touch need a tree, so they don't
    assert "hit_delta" not in rules
    assert "must_touch" not in rules


def test_normalized_request_anchors_also_considered(
    tree_with_anchor: Path,
) -> None:
    diff = _new_file_diff("src/new.kt", "val x = 1")
    report = check_spec_conformance(
        request_text="do the cleanup",  # original text has no anchor
        normalized_request='Remove hardcoded "Minij" in the codebase',
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert report.blocked
    rules = {f.rule for f in report.findings}
    # destructive verb + new files only → shadow
    assert "shadow_implementation" in rules
    # anchor found via normalized_request
    assert "must_touch" in rules


def test_report_payload_serializes_cleanly(tree_with_anchor: Path) -> None:
    diff = _new_file_diff("src/new.kt", "nothing")
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    import json

    encoded = json.dumps(report.to_payload())
    assert '"verdict"' in encoded
    assert '"findings"' in encoded


# ----------------------- goal attestation (T-038) ------------------------ #


def _modify_diff_remove_anchor(path: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"index aaa..bbb 100644\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,1 @@\n"
        "-val user = \"Minij\"\n"
        " val admin = \"master admin\"\n"
    )


def test_attestation_reports_achieved_when_anchor_removed(tree_with_anchor: Path) -> None:
    diff = _modify_diff_remove_anchor("src/a.kt")
    result = build_goal_attestation(
        request_text='Remove "Minij" from the codebase.',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert "remove" in result["destructive_verbs_detected"]
    minij = next(a for a in result["anchors"] if a["anchor"] == "Minij")
    assert minij["status"] == "achieved"
    assert minij["count_after"] < minij["count_before"]
    assert "src/a.kt" in minij["files_modified"]


def test_attestation_reports_not_achieved_when_only_new_files(tree_with_anchor: Path) -> None:
    diff = _new_file_diff("src/new.kt", "clean\n")
    result = build_goal_attestation(
        request_text='Remove "Minij" from the codebase.',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    minij = next(a for a in result["anchors"] if a["anchor"] == "Minij")
    assert minij["status"] == "not_achieved"
    assert minij["files_modified"] == []
    assert result["all_goals_met"] is False


def test_attestation_true_when_no_destructive_intent(tree_with_anchor: Path) -> None:
    diff = _new_file_diff("src/feature.kt", "pass\n")
    result = build_goal_attestation(
        request_text="Add a new feature module.",
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    assert result["destructive_verbs_detected"] == []
    assert result["all_goals_met"] is True


# --------------- T-040 bug fix: unified diff parsing (Strategy 2) ----------- #


def _unified_diff(path: str, minus_lines: list[str], plus_lines: list[str]) -> str:
    """Build a standard unified diff WITHOUT 'diff --git' header."""
    total = max(len(minus_lines), len(plus_lines)) or 1
    header = (
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{total} +1,{total} @@\n"
    )
    body = "".join(f"-{l}\n" for l in minus_lines) + "".join(f"+{l}\n" for l in plus_lines)
    return header + body


def test_unified_diff_without_git_header_detected_as_modify(
    tree_with_anchor: Path,
) -> None:
    diff = _unified_diff("src/a.kt", ['val user = "Minij"'], ['val user = ""'])
    report = check_spec_conformance(
        request_text='Remove "Minij"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    must_touch = [f for f in report.findings if f.rule == "must_touch"]
    assert must_touch == [], "unified diff should count as touching src/a.kt"


def test_unified_diff_new_file_detected() -> None:
    from app.services.spec_conformance import _classify_files_in_diff

    diff = "--- /dev/null\n+++ b/src/new.kt\n@@ -0,0 +1 @@\n+hello\n"
    shapes = _classify_files_in_diff(diff)
    assert shapes.get("src/new.kt") == "create"


def test_unified_diff_delete_detected() -> None:
    from app.services.spec_conformance import _classify_files_in_diff

    diff = "--- a/src/old.kt\n+++ /dev/null\n@@ -1 +0,0 @@\n-goodbye\n"
    shapes = _classify_files_in_diff(diff)
    assert shapes.get("src/old.kt") == "delete"


# ------------ T-040 bug fix: hit_delta aggregate (warn not block) ----------- #


@pytest.fixture()
def tree_multi_anchor(tmp_path: Path) -> Path:
    """Tree with two anchors: master1 (primary target) and mockUsers (incidental)."""
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "mockUsers.js").write_text(
        'export const mockUsers = [\n  { id: "master1", name: "Master" },\n  { id: "staff1" },\n];\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "Dashboard.js").write_text(
        'const currentUser = JSON.parse(localStorage.getItem("currentUser"));\n',
        encoding="utf-8",
    )
    return tmp_path


def test_hit_delta_aggregate_warn_when_primary_decreased(
    tree_multi_anchor: Path,
) -> None:
    """If at least one anchor decreased, remaining non-decreased are warn (not block)."""
    diff = _modify_diff(
        "src/data/mockUsers.js",
        ['  { id: "master1", name: "Master" },'],
        [],
    )
    report = check_spec_conformance(
        request_text='delete "master1" from mockUsers.js',
        normalized_request=None,
        diff=diff,
        source_tree=tree_multi_anchor,
    )
    hit_delta = [f for f in report.findings if f.rule == "hit_delta"]
    block_deltas = [f for f in hit_delta if f.severity == "block"]
    assert block_deltas == [], "incidental anchors should be warn when primary decreased"


def test_hit_delta_all_unchanged_blocks(tree_multi_anchor: Path) -> None:
    """When NO anchor decreased, all hit_delta findings escalate to block."""
    diff = _new_file_diff("src/wrapper.js", "// empty wrapper")
    report = check_spec_conformance(
        request_text='delete "master1" from mockUsers.js',
        normalized_request=None,
        diff=diff,
        source_tree=tree_multi_anchor,
    )
    hit_delta = [f for f in report.findings if f.rule == "hit_delta"]
    assert len(hit_delta) > 0
    assert all(f.severity == "block" for f in hit_delta)


# ----------- T-040 bug fix: alphanum identifier extraction (master1) -------- #


def test_alphanum_anchor_extracted() -> None:
    from app.services.spec_conformance import _extract_quoted_anchors

    anchors = _extract_quoted_anchors("delete master1 from mockUsers.js")
    anchor_lower = [a.lower() for a in anchors]
    assert "master1" in anchor_lower, "master1 should be extracted as an alphanum identifier"


def test_alphanum_short_tokens_ignored() -> None:
    from app.services.spec_conformance import _extract_quoted_anchors

    anchors = _extract_quoted_anchors("fix a1 b2 c3 in code")
    short = [a for a in anchors if len(a) < 4]
    assert short == [], "tokens shorter than 4 chars should be excluded"


def test_alphanum_anchor_triggers_must_touch(tree_multi_anchor: Path) -> None:
    """Unquoted master1 anchor should make must_touch fire when its file is not in the diff."""
    diff = _new_file_diff("src/other.js", "// unrelated")
    report = check_spec_conformance(
        request_text="delete master1 from mockUsers.js",
        normalized_request=None,
        diff=diff,
        source_tree=tree_multi_anchor,
    )
    rules = {f.rule for f in report.findings}
    assert "must_touch" in rules


# ---------- T-040 defense line 2: anchor pre-check decision logic ---------- #


def test_find_files_containing_anchor_finds_hits(tree_with_anchor: Path) -> None:
    from app.services.spec_conformance import _find_files_containing_anchor

    hits = _find_files_containing_anchor(tree_with_anchor, "Minij")
    assert len(hits) >= 2
    assert all(v > 0 for v in hits.values())


def test_find_files_containing_anchor_returns_empty_for_missing(tree_with_anchor: Path) -> None:
    from app.services.spec_conformance import _find_files_containing_anchor

    hits = _find_files_containing_anchor(tree_with_anchor, "NeverPresent99")
    assert hits == {}


def test_precheck_logic_all_missing_would_fail(tree_with_anchor: Path) -> None:
    """Simulate the orchestrator pre-check decision: all anchors missing → fail."""
    from app.services.spec_conformance import _find_files_containing_anchor

    anchors = ["ghost1", "ghost2", "ghost3"]
    missing = [a for a in anchors if not _find_files_containing_anchor(tree_with_anchor, a)]
    assert len(missing) == len(anchors), "all anchors should be missing"


def test_precheck_logic_partial_hit_would_proceed(tree_with_anchor: Path) -> None:
    """When at least one anchor exists, pre-check should NOT fail."""
    from app.services.spec_conformance import _find_files_containing_anchor

    anchors = ["Minij", "ghost1"]
    missing = [a for a in anchors if not _find_files_containing_anchor(tree_with_anchor, a)]
    assert len(missing) < len(anchors), "partial hit means proceed"


# -------- T-040: anchors_missing_from_tree rule in check_spec_conformance --- #


def test_anchors_missing_from_tree_blocks(tree_with_anchor: Path) -> None:
    """When ALL anchors are absent from tree + destructive verb → block."""
    diff = _new_file_diff("src/new.kt", "nothing")
    report = check_spec_conformance(
        request_text='Remove "zzzNeverExist" and "yyyAlsoMissing"',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    rules = {f.rule for f in report.findings}
    assert "anchors_missing_from_tree" in rules
    assert report.blocked


def test_anchors_present_no_missing_rule(tree_with_anchor: Path) -> None:
    """When anchors DO exist in tree, anchors_missing_from_tree should not fire."""
    diff = _new_file_diff("src/new.kt", "nothing")
    report = check_spec_conformance(
        request_text='Remove "Minij" from code',
        normalized_request=None,
        diff=diff,
        source_tree=tree_with_anchor,
    )
    rules = {f.rule for f in report.findings}
    assert "anchors_missing_from_tree" not in rules
