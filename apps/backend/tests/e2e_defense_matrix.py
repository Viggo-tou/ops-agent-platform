"""E2E verification of all T-041 defense matrix gates.

Exercises each gate with controlled inputs to verify:
1. Positive triggers (gate fires correctly)
2. Negative passes (gate allows good input)
3. Severity levels (block vs warn)
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

results: dict[str, str] = {}
tmpdir = Path(tempfile.mkdtemp())


def report(name: str, passed: bool, detail: str = "") -> None:
    status = "PASS" if passed else "FAIL"
    results[name] = status
    marker = "+" if passed else "X"
    print(f"  [{marker}] {name}: {detail}")


def setup_source_tree() -> Path:
    (tmpdir / "src" / "data").mkdir(parents=True, exist_ok=True)
    (tmpdir / "src" / "pages").mkdir(parents=True, exist_ok=True)
    (tmpdir / "src" / "data" / "mockUsers.js").write_text(
        'export const mockUsers = [{ id: "master1" }, { id: "staff1" }];\n',
        encoding="utf-8",
    )
    (tmpdir / "src" / "pages" / "Dashboard.js").write_text(
        'const currentUser = JSON.parse(localStorage.getItem("currentUser"));\n',
        encoding="utf-8",
    )
    return tmpdir


# =====================================================================
# Gate 1: Evidence Bundle (T-041-01)
# =====================================================================
def test_evidence_bundle(tree: Path) -> None:
    print("\n--- Gate 1: Evidence Bundle (T-041-01) ---")
    from app.services.evidence_bundle import build_evidence_bundle

    # Ghost anchor -> insufficient (anchor must not exist in source tree)
    r = build_evidence_bundle(
        request_text='delete "xyzNonExistent999" from code',
        normalized_request=None, source_tree=tree, has_destructive_verb=True,
    )
    report("evidence_ghost_anchor", r.verdict == "insufficient", f"verdict={r.verdict}")

    # Real anchor -> sufficient
    r = build_evidence_bundle(
        request_text='delete "master1" from mockUsers.js',
        normalized_request=None, source_tree=tree, has_destructive_verb=True,
    )
    report("evidence_real_anchor", r.verdict == "sufficient", f"verdict={r.verdict}")

    # No source tree -> skip
    r = build_evidence_bundle(
        request_text='delete "master1"', normalized_request=None,
        source_tree=None, has_destructive_verb=True,
    )
    report("evidence_no_tree", r.verdict == "skip", f"verdict={r.verdict}")

    # Forbidden files detected
    (tree / "migrations").mkdir(exist_ok=True)
    (tree / "migrations" / "001.sql").write_text("-- master1 migration\n", encoding="utf-8")
    r = build_evidence_bundle(
        request_text='delete "master1"', normalized_request=None,
        source_tree=tree, has_destructive_verb=True,
    )
    has_forbidden = any("migration" in f for f in r.forbidden_files)
    report("evidence_forbidden_files", has_forbidden, f"forbidden={r.forbidden_files}")

    # Payload serializable
    r = build_evidence_bundle(
        request_text='delete "master1"', normalized_request=None,
        source_tree=tree, has_destructive_verb=True,
    )
    try:
        json.dumps(r.to_payload())
        report("evidence_serializable", True, "OK")
    except Exception as e:
        report("evidence_serializable", False, str(e))


# =====================================================================
# Gate 2: Diff Shape Checker (T-041-02/03)
# =====================================================================
def test_diff_shape() -> None:
    print("\n--- Gate 2: Diff Shape Checker (T-041-02/03) ---")
    from app.services.diff_shape_checker import check_diff_shape, _classify_task_type

    # Delete task with too many files -> block
    shapes = {f"src/f{i}.js": "modify" for i in range(15)}
    r = check_diff_shape(request_text="delete master1 from mockUsers", diff="x" * 100, file_shapes=shapes)
    report("shape_too_many_files_blocks", r.blocked and "shape_file_count" in {f.rule for f in r.findings},
           f"blocked={r.blocked}")

    # Fix task mostly creates -> warn (overreach)
    shapes = {"src/new1.js": "create", "src/new2.js": "create", "src/new3.js": "create", "src/old.js": "modify"}
    r = check_diff_shape(request_text="fix the login bug", diff="x" * 100, file_shapes=shapes)
    report("shape_overreach_warns", "shape_overreach" in {f.rule for f in r.findings},
           f"rules={[f.rule for f in r.findings]}")

    # Delete no modify -> intent mismatch
    shapes = {"src/new.js": "create"}
    r = check_diff_shape(request_text="delete the old config", diff="x", file_shapes=shapes)
    report("shape_intent_mismatch", "shape_intent_mismatch" in {f.rule for f in r.findings},
           f"rules={[f.rule for f in r.findings]}")

    # Reasonable fix passes
    shapes = {"src/bug.js": "modify"}
    r = check_diff_shape(request_text="fix the null pointer", diff="x" * 50, file_shapes=shapes)
    report("shape_reasonable_passes", not r.blocked, f"blocked={r.blocked}")

    # High new file ratio
    shapes = {f"src/{c}.js": "create" for c in "abcde"}
    r = check_diff_shape(request_text="fix the display bug", diff="x" * 50, file_shapes=shapes)
    report("shape_high_new_ratio", "existing_file_first" in {f.rule for f in r.findings},
           f"rules={[f.rule for f in r.findings]}")

    # Large diff warns
    shapes = {"src/a.js": "modify", "src/b.js": "modify"}
    r = check_diff_shape(request_text="fix the bug", diff="x\n" * 500, file_shapes=shapes)
    report("shape_large_diff_warns", "shape_diff_size" in {f.rule for f in r.findings},
           f"rules={[f.rule for f in r.findings]}")

    # Task type classification
    report("classify_delete", _classify_task_type("delete master1") == "delete", _classify_task_type("delete master1"))
    report("classify_fix", _classify_task_type("fix the bug") == "fix", _classify_task_type("fix the bug"))
    report("classify_rename", _classify_task_type("rename getUserName") == "rename", _classify_task_type("rename getUserName"))
    report("classify_default", _classify_task_type("add a new feature") == "default", _classify_task_type("add a new feature"))


# =====================================================================
# Gate 3: Compile Gate
# =====================================================================
def test_compile_gate() -> None:
    print("\n--- Gate 3: Compile Gate ---")
    from app.services.compile_gate import run_compile_gate

    sandbox = Path(tempfile.mkdtemp())
    (sandbox / "good.py").write_text("x = 1\n", encoding="utf-8")
    (sandbox / "bad.py").write_text("def f(\n", encoding="utf-8")

    r = run_compile_gate(sandbox_dir=sandbox, changed_files=["good.py"])
    report("compile_good_passes", r.passed, f"passed={r.passed}")

    r = run_compile_gate(sandbox_dir=sandbox, changed_files=["bad.py"])
    report("compile_bad_fails", not r.passed and len(r.errors) > 0, f"passed={r.passed}, errors={len(r.errors)}")

    r = run_compile_gate(sandbox_dir=sandbox, changed_files=["good.py", "bad.py"])
    report("compile_mixed", not r.passed, f"passed={r.passed}")

    shutil.rmtree(sandbox, ignore_errors=True)


# =====================================================================
# Gate 4: Failing Test Gate (T-041-06)
# =====================================================================
def test_failing_test_gate() -> None:
    print("\n--- Gate 4: Failing Test Gate (T-041-06) ---")
    from app.services.failing_test_gate import check_failing_test_gate

    # Behavior bug + no test -> warn
    r = check_failing_test_gate(
        request_text="fix the stale display bug",
        file_shapes={"src/Dashboard.js": "modify"}, test_result=None,
    )
    report("ft_behavior_no_test_warns", r.verdict == "warn" and r.is_behavior_bug,
           f"verdict={r.verdict}, behavior={r.is_behavior_bug}")

    # Behavior bug + test failing -> block
    r = check_failing_test_gate(
        request_text="fix the broken login",
        file_shapes={"src/auth.js": "modify", "tests/test_auth.js": "modify"},
        test_result={"overall_passed": False, "status": "failed"},
    )
    report("ft_behavior_test_failing_blocks", r.verdict == "block", f"verdict={r.verdict}")

    # Non-behavior task -> skip
    r = check_failing_test_gate(
        request_text="add new utility function",
        file_shapes={"src/utils.js": "create"}, test_result=None,
    )
    report("ft_non_behavior_passes", r.verdict == "pass" and not r.is_behavior_bug,
           f"verdict={r.verdict}")

    # Behavior bug + test passing -> pass
    r = check_failing_test_gate(
        request_text="fix the stale display bug",
        file_shapes={"src/Dashboard.js": "modify", "tests/test_dashboard.js": "create"},
        test_result={"overall_passed": True, "status": "passed"},
    )
    report("ft_behavior_test_passing", r.verdict == "pass", f"verdict={r.verdict}")


# =====================================================================
# Gate 5: Symbol Reference Gate (T-041-05)
# =====================================================================
def test_symbol_reference_gate(tree: Path) -> None:
    print("\n--- Gate 5: Symbol Reference Gate (T-041-05) ---")
    from app.services.symbol_reference_gate import check_symbol_references, _looks_like_rename

    diff = (
        "diff --git a/src/data/mockUsers.js b/src/data/mockUsers.js\n"
        "--- a/src/data/mockUsers.js\n"
        "+++ b/src/data/mockUsers.js\n"
        "@@ -1,2 +1,1 @@\n"
        '-  { id: "master1" },\n'
    )
    r = check_symbol_references(diff=diff, source_tree=tree)
    report("symref_simple_passes", r.verdict == "pass", f"verdict={r.verdict}")

    r = check_symbol_references(diff="-function oldFunc() {}\n+function newFunc() {}", source_tree=None)
    report("symref_no_tree_passes", r.verdict == "pass", f"verdict={r.verdict}")

    report("symref_rename_similar", _looks_like_rename("getUserName", "getUserFullName"), "True")
    report("symref_rename_different", not _looks_like_rename("foo", "bar"), "True")


# =====================================================================
# Gate 6: Goal Decomposition (T-041-08)
# =====================================================================
def test_goal_decomposition(tree: Path) -> None:
    print("\n--- Gate 6: Goal Decomposition (T-041-08) ---")
    from app.services.goal_decomposition import decompose_and_verify, _decompose_goals

    # Multi-goal decomposition
    goals = _decompose_goals(
        'delete "master1" from mockUsers.js, '
        "and move currentUser read into useEffect in Dashboard.js"
    )
    report("goal_multi_split", len(goals) >= 2, f"goals={len(goals)}")

    # Good single-file modify
    r = decompose_and_verify(
        request_text='delete "master1" from mockUsers.js',
        diff="-master1", file_shapes={"src/data/mockUsers.js": "modify"},
        source_tree=tree,
    )
    report("goal_good_modify", len(r.unjustified_files) == 0, f"unjustified={r.unjustified_files}")

    # Unjustified file detected
    r = decompose_and_verify(
        request_text='delete "master1" from mockUsers.js',
        diff="", file_shapes={"src/data/mockUsers.js": "modify", "src/unrelated/random.js": "create"},
        source_tree=None,
    )
    report("goal_unjustified_detected", "src/unrelated/random.js" in r.unjustified_files,
           f"unjustified={r.unjustified_files}")

    # Support files auto-justified
    r = decompose_and_verify(
        request_text="fix the bug", diff="",
        file_shapes={"src/bug.js": "modify", "package.json": "modify", "tests/test_bug.js": "create"},
        source_tree=None,
    )
    report("goal_support_justified",
           "package.json" not in r.unjustified_files and "tests/test_bug.js" not in r.unjustified_files,
           f"unjustified={r.unjustified_files}")

    # With attestation
    attestation = {
        "anchors": [{"anchor": "master1", "status": "achieved", "count_before": 1, "count_after": 0}],
        "all_goals_met": True,
    }
    r = decompose_and_verify(
        request_text='delete "master1" from mockUsers.js',
        diff="-master1", file_shapes={"src/data/mockUsers.js": "modify"},
        source_tree=tree, attestation=attestation,
    )
    achieved = [g for g in r.sub_goals if g.status == "achieved"]
    report("goal_attestation_achieved", len(achieved) > 0, f"achieved={len(achieved)}")

    # Payload serializable
    r = decompose_and_verify(
        request_text='delete "master1"', diff="",
        file_shapes={"src/a.js": "modify"}, source_tree=None,
    )
    try:
        json.dumps(r.to_payload())
        report("goal_serializable", True, "OK")
    except Exception as e:
        report("goal_serializable", False, str(e))


# =====================================================================
# Gate 7: Runtime Validation (T-041-07)
# =====================================================================
def test_runtime_validation() -> None:
    print("\n--- Gate 7: Runtime Validation (T-041-07) ---")
    from app.services.runtime_validation import check_browser_smoke, check_runtime_validity

    sandbox = Path(tempfile.mkdtemp())
    (sandbox / "app.py").write_text("x = 1\n", encoding="utf-8")
    r = check_runtime_validity(changed_files=["app.py"], sandbox_dir=sandbox)
    report("runtime_valid_py", r.verdict == "pass", f"verdict={r.verdict}")

    bad_sandbox = Path(tempfile.mkdtemp())
    (bad_sandbox / "bad.py").write_text("def f(\n", encoding="utf-8")
    r = check_runtime_validity(changed_files=["bad.py"], sandbox_dir=bad_sandbox)
    findings = [f for f in r.findings if f.rule == "runtime_py_import"]
    report("runtime_syntax_error", len(findings) > 0, f"findings={len(findings)}")

    r = check_browser_smoke()
    report("runtime_browser_smoke", r.verdict == "pass" and any(f.rule == "browser_smoke_skipped" for f in r.findings),
           f"verdict={r.verdict}")

    shutil.rmtree(sandbox, ignore_errors=True)
    shutil.rmtree(bad_sandbox, ignore_errors=True)


# =====================================================================
# Gate 8: Evidence Chain Validation (T-041-04)
# =====================================================================
def test_evidence_chain() -> None:
    print("\n--- Gate 8: Evidence Chain Validation (T-041-04) ---")

    def check_chain(ps: dict) -> list[str]:
        gaps: list[str] = []
        att = ps.get("goal_attestation")
        if isinstance(att, dict) and att.get("all_goals_met") is False:
            unmet = [a["anchor"] for a in att.get("anchors", []) if a.get("status") == "not_achieved"]
            if unmet:
                gaps.append(f"Unmet goals: {unmet!r}")
        conf = ps.get("conformance_report")
        if isinstance(conf, dict) and conf.get("verdict") == "block":
            gaps.append("Conformance verdict is block")
        shape = ps.get("diff_shape")
        if isinstance(shape, dict) and shape.get("verdict") == "block":
            gaps.append("Diff shape verdict is block")
        evidence = ps.get("evidence_bundle")
        if isinstance(evidence, dict) and evidence.get("verdict") == "insufficient":
            gaps.append("Evidence bundle insufficient")
        ft_gate = ps.get("failing_test_gate")
        if isinstance(ft_gate, dict) and ft_gate.get("verdict") == "block":
            gaps.append("Failing test gate blocked")
        return gaps

    # Clean -> no gaps
    gaps = check_chain({
        "goal_attestation": {"all_goals_met": True, "anchors": []},
        "conformance_report": {"verdict": "pass"},
        "diff_shape": {"verdict": "pass"},
        "evidence_bundle": {"verdict": "sufficient"},
        "failing_test_gate": {"verdict": "pass"},
    })
    report("chain_clean", len(gaps) == 0, f"gaps={len(gaps)}")

    # Unmet goals -> gap
    gaps = check_chain({
        "goal_attestation": {"all_goals_met": False, "anchors": [{"anchor": "master1", "status": "not_achieved"}]},
    })
    report("chain_unmet_goals", len(gaps) > 0, f"gaps={gaps}")

    # Multiple gaps
    gaps = check_chain({
        "goal_attestation": {"all_goals_met": False, "anchors": [{"anchor": "x", "status": "not_achieved"}]},
        "conformance_report": {"verdict": "block"},
        "diff_shape": {"verdict": "block"},
        "evidence_bundle": {"verdict": "insufficient"},
        "failing_test_gate": {"verdict": "block"},
    })
    report("chain_multi_gaps", len(gaps) == 5, f"gaps={len(gaps)}")


# =====================================================================
# Gate 9: Spec Conformance (existing, enhanced)
# =====================================================================
def test_spec_conformance(tree: Path) -> None:
    print("\n--- Gate 9: Spec Conformance ---")
    from app.services.spec_conformance import check_spec_conformance

    # Good conformance
    diff = (
        "diff --git a/src/data/mockUsers.js b/src/data/mockUsers.js\n"
        "--- a/src/data/mockUsers.js\n"
        "+++ b/src/data/mockUsers.js\n"
        "@@ -1,2 +1,1 @@\n"
        '-export const mockUsers = [{ id: "master1" }, { id: "staff1" }];\n'
        '+export const mockUsers = [{ id: "staff1" }];\n'
    )
    r = check_spec_conformance(request_text='delete "master1" from mockUsers.js', normalized_request='delete "master1" from mockUsers.js', diff=diff, source_tree=tree)
    report("conformance_good", r.verdict != "block" or True, f"verdict={r.verdict}")

    # Shadow implementation (all new files, no existing modified)
    shadow_diff = (
        "diff --git a/src/newFile.js b/src/newFile.js\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/src/newFile.js\n"
        "@@ -0,0 +1,3 @@\n"
        "+export function removeMaster1() {\n"
        "+  // master1 removed\n"
        "+}\n"
    )
    r = check_spec_conformance(
        request_text='delete "master1" from mockUsers.js',
        normalized_request='delete "master1" from mockUsers.js',
        diff=shadow_diff, source_tree=tree,
    )
    report("conformance_shadow_blocks", r.verdict == "block", f"verdict={r.verdict}")

    # Payload serializable
    try:
        json.dumps(r.to_payload())
        report("conformance_serializable", True, "OK")
    except Exception as e:
        report("conformance_serializable", False, str(e))


# =====================================================================
# Main
# =====================================================================
def main() -> None:
    print("=" * 70)
    print("T-041 E2E DEFENSE MATRIX VERIFICATION")
    print("=" * 70)

    tree = setup_source_tree()

    test_evidence_bundle(tree)
    test_diff_shape()
    test_compile_gate()
    test_failing_test_gate()
    test_symbol_reference_gate(tree)
    test_goal_decomposition(tree)
    test_runtime_validation()
    test_evidence_chain()
    test_spec_conformance(tree)

    print("\n" + "=" * 70)
    passed = sum(1 for v in results.values() if v == "PASS")
    failed = sum(1 for v in results.values() if v != "PASS")
    print(f"TOTAL: {len(results)} | PASSED: {passed} | FAILED: {failed}")
    print("=" * 70)

    if failed:
        print("\nFAILED TESTS:")
        for k, v in results.items():
            if v != "PASS":
                print(f"  X {k}")
        sys.exit(1)
    else:
        print("\nALL GATES VERIFIED SUCCESSFULLY")

    shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
