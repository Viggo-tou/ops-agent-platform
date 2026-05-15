"""Tests for v15 Ticket 2B: must_touch / expected_new coverage gate.

Models the v14 P69-19 partial-success scenario plus the 4 rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.services.batch_coverage import (
    BatchOutcome,
    CoverageVerdict,
    check_coverage,
    classify_batch_outcome,
    role_for_path,
)


@dataclass
class _StubPlan:
    must_touch_files: list[str] = field(default_factory=list)
    must_inspect_files: list[str] = field(default_factory=list)
    likely_touch_files: list[str] = field(default_factory=list)
    expected_new_files: list[str] = field(default_factory=list)


# --- role_for_path ---------------------------------------------------------


def test_role_for_path_must_touch_exact():
    plan = _StubPlan(must_touch_files=["app/src/Foo.kt"])
    assert role_for_path("app/src/Foo.kt", plan) == "must_touch"


def test_role_for_path_must_touch_suffix_tolerant():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    assert role_for_path("app/src/Foo.kt", plan) == "must_touch"


def test_role_for_path_must_inspect():
    plan = _StubPlan(must_inspect_files=["app/build.gradle"])
    assert role_for_path("app/build.gradle", plan) == "must_inspect"


def test_role_for_path_likely_touch():
    plan = _StubPlan(likely_touch_files=["src/Bar.kt"])
    assert role_for_path("src/Bar.kt", plan) == "likely_touch"


def test_role_for_path_expected_new():
    plan = _StubPlan(expected_new_files=["src/NewFile.kt"])
    assert role_for_path("src/NewFile.kt", plan) == "expected_new"


def test_role_for_path_unknown_when_not_declared():
    plan = _StubPlan(must_touch_files=["src/Other.kt"])
    assert role_for_path("src/Foo.kt", plan) == "unknown"


# --- classify_batch_outcome ------------------------------------------------


def test_classify_patched_when_diff_modifies_target():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    batch_result = {
        "diff": (
            "diff --git a/src/Foo.kt b/src/Foo.kt\n"
            "--- a/src/Foo.kt\n"
            "+++ b/src/Foo.kt\n"
            "@@ -1,3 +1,4 @@\n"
            " fun foo() {\n"
            "+    val x = 1\n"
            " }\n"
        ),
        "files_changed": ["src/Foo.kt"],
        "provider_name": "deepseek",
    }
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result=batch_result,
        error=None,
        batch_id="batch-1/2",
    )
    assert outcome.status == "patched"
    assert outcome.role == "must_touch"
    assert outcome.changed is True
    assert outcome.diff_stats["added"] == 1


def test_classify_phantom_no_change_from_error():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    err = RuntimeError(
        "codegen_terminal: PHANTOM_NO_CHANGE: 1 of 1 evidence quote(s) "
        "could not be verified against the file content."
    )
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result=None,
        error=err,
    )
    assert outcome.status == "phantom_no_change"
    assert outcome.terminal_kind == "PHANTOM_NO_CHANGE"


def test_classify_verified_no_change_from_error():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    err = RuntimeError(
        "codegen_terminal: NO_CHANGE_NEEDED_VERIFIED: feature already implemented"
    )
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result=None,
        error=err,
    )
    assert outcome.status == "verified_no_change"
    assert outcome.terminal_kind == "NO_CHANGE_NEEDED_VERIFIED"


def test_classify_no_output_when_diff_empty():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result={"diff": "", "files_changed": []},
        error=None,
    )
    assert outcome.status == "no_output"


def test_classify_no_output_when_diff_doesnt_touch_target():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    batch_result = {
        "diff": (
            "diff --git a/src/Other.kt b/src/Other.kt\n"
            "--- a/src/Other.kt\n"
            "+++ b/src/Other.kt\n"
            "@@ -1,1 +1,2 @@\n"
            " line1\n"
            "+added\n"
        ),
        "files_changed": ["src/Other.kt"],
    }
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt", plan=plan, batch_result=batch_result, error=None
    )
    assert outcome.status == "no_output"


def test_classify_non_executable_when_patch_only_adds_comment():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    batch_result = {
        "diff": (
            "diff --git a/src/Foo.kt b/src/Foo.kt\n"
            "--- a/src/Foo.kt\n"
            "+++ b/src/Foo.kt\n"
            "@@ -1,3 +1,4 @@\n"
            " fun foo() {\n"
            "+    // Allow repeated OTP requests for the same phone number\n"
            " }\n"
        ),
        "files_changed": ["src/Foo.kt"],
    }
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result=batch_result,
        error=None,
    )
    assert outcome.status == "non_executable_patch"
    assert outcome.changed is False


def test_classify_other_failure_on_generic_error():
    plan = _StubPlan(must_touch_files=["src/Foo.kt"])
    outcome = classify_batch_outcome(
        file_path="src/Foo.kt",
        plan=plan,
        batch_result=None,
        error=RuntimeError("API rate limit exceeded"),
    )
    assert outcome.status == "other_failure"
    assert "rate limit" in outcome.reason.lower()


# --- check_coverage --------------------------------------------------------


def _patched(path: str, role: str = "must_touch") -> BatchOutcome:
    return BatchOutcome(
        file_path=path,
        role=role,  # type: ignore[arg-type]
        status="patched",
        changed=True,
        diff_stats={"added": 10, "removed": 1},
    )


def _phantom(path: str, role: str = "must_touch") -> BatchOutcome:
    return BatchOutcome(
        file_path=path,
        role=role,  # type: ignore[arg-type]
        status="phantom_no_change",
        terminal_kind="PHANTOM_NO_CHANGE",
        reason="quote not found",
    )


def _verified_no_change(path: str, role: str = "must_touch") -> BatchOutcome:
    return BatchOutcome(
        file_path=path,
        role=role,  # type: ignore[arg-type]
        status="verified_no_change",
        terminal_kind="NO_CHANGE_NEEDED_VERIFIED",
    )


def _no_output(path: str, role: str = "must_touch") -> BatchOutcome:
    return BatchOutcome(
        file_path=path,
        role=role,  # type: ignore[arg-type]
        status="no_output",
    )


def _non_executable(path: str, role: str = "must_touch") -> BatchOutcome:
    return BatchOutcome(
        file_path=path,
        role=role,  # type: ignore[arg-type]
        status="non_executable_patch",
        reason="patch only changed comments or whitespace",
    )


def test_coverage_ok_when_all_must_touch_patched():
    plan = _StubPlan(must_touch_files=["src/A.kt", "src/B.kt"])
    verdict = check_coverage([_patched("src/A.kt"), _patched("src/B.kt")], plan)
    assert verdict.ok
    assert verdict.kind == "ok"


def test_coverage_phantom_blocks_pipeline():
    """v14-style failure mode: must_touch + phantom_no_change → unrecoverable."""
    plan = _StubPlan(must_touch_files=["src/A.kt"])
    verdict = check_coverage([_phantom("src/A.kt")], plan)
    assert verdict.kind == "phantom_no_change_unrecoverable"
    assert "PHANTOM_NO_CHANGE" in verdict.summary


def test_coverage_partial_success_blocked():
    """v14 P69-19 scenario: 1 patched + 1 phantom → blocked, not partial-pass."""
    plan = _StubPlan(must_touch_files=[
        "src/CustomerKYCAddressForm.kt",
        "src/HandymanKYCAddressForm.kt",
    ])
    verdict = check_coverage(
        [
            _phantom("src/CustomerKYCAddressForm.kt"),
            _patched("src/HandymanKYCAddressForm.kt"),
        ],
        plan,
    )
    # phantom precedence over missing — we still flag the phantom.
    assert verdict.kind == "phantom_no_change_unrecoverable"
    assert verdict.diagnostic["must_touch_patched"] == 1
    assert verdict.diagnostic["must_touch_phantom"] == 1


def test_coverage_verified_no_change_goes_to_approval():
    """must_touch + verified_no_change → plan_codegen_conflict (human review)."""
    plan = _StubPlan(must_touch_files=["src/A.kt"])
    verdict = check_coverage([_verified_no_change("src/A.kt")], plan)
    assert verdict.kind == "plan_codegen_conflict"
    assert "Human review" in verdict.summary or "human" in verdict.summary.lower()


def test_coverage_missing_batch_blocks():
    """A must_touch file with NO batch dispatched at all."""
    plan = _StubPlan(must_touch_files=["src/A.kt", "src/B.kt"])
    # Only A reported an outcome; B is missing.
    verdict = check_coverage([_patched("src/A.kt")], plan)
    assert verdict.kind == "missing_must_touch"
    missing_paths = [o.file_path for o in verdict.failures]
    assert any("B.kt" in p for p in missing_paths)


def test_coverage_comment_only_patch_blocks_as_missing_must_touch():
    plan = _StubPlan(must_touch_files=["src/A.kt"])
    verdict = check_coverage([_non_executable("src/A.kt")], plan)
    assert verdict.kind == "missing_must_touch"
    assert verdict.failures[0].status == "non_executable_patch"


def test_coverage_expected_new_must_be_created():
    plan = _StubPlan(expected_new_files=["src/NewFile.kt"])
    # No patches, no outcomes for the new file.
    verdict = check_coverage([], plan)
    assert verdict.kind == "missing_expected_new"


def test_coverage_expected_new_patched_passes():
    plan = _StubPlan(expected_new_files=["src/NewFile.kt"])
    verdict = check_coverage(
        [_patched("src/NewFile.kt", role="expected_new")], plan
    )
    assert verdict.ok


def test_coverage_likely_touch_verified_no_change_ok():
    """Rule B-likely: likely_touch may verified-no-change without conflict."""
    plan = _StubPlan(
        must_touch_files=["src/A.kt"],
        likely_touch_files=["src/B.kt"],
    )
    verdict = check_coverage(
        [
            _patched("src/A.kt"),
            _verified_no_change("src/B.kt", role="likely_touch"),
        ],
        plan,
    )
    assert verdict.ok


def test_coverage_likely_touch_phantom_still_fails():
    """likely_touch + phantom → still fails (model failure)."""
    plan = _StubPlan(
        must_touch_files=["src/A.kt"],
        likely_touch_files=["src/B.kt"],
    )
    verdict = check_coverage(
        [
            _patched("src/A.kt"),
            _phantom("src/B.kt", role="likely_touch"),
        ],
        plan,
    )
    assert verdict.kind == "phantom_no_change_unrecoverable"


def test_coverage_precedence_phantom_over_conflict():
    """When both phantom AND verified_no_change present, phantom wins
    (it's a model failure, not a human-judgment item)."""
    plan = _StubPlan(must_touch_files=["src/A.kt", "src/B.kt"])
    verdict = check_coverage(
        [
            _phantom("src/A.kt"),
            _verified_no_change("src/B.kt"),
        ],
        plan,
    )
    assert verdict.kind == "phantom_no_change_unrecoverable"


def test_coverage_no_must_touch_plan_passes_trivially():
    """When the plan declares no must_touch files (e.g. legacy fallback
    path), the gate should not block."""
    plan = _StubPlan()
    verdict = check_coverage([], plan)
    assert verdict.ok


# --- v14 regression --------------------------------------------------------


def test_v14_p69_19_regression():
    """The exact v14 P69-19 failure mode.

    Batch 1 (CustomerKYCAddressForm) returned NO_CHANGE_NEEDED with a
    hallucinated MapView claim → phantom_no_change. Batch 2
    (HandymanKYCAddressForm) produced +129/-1 patch → patched. Under
    v14 behavior the orchestrator counted this as "1/2 partial
    success" and continued into compile/review. The coverage gate
    must now block: phantom_no_change_unrecoverable.
    """
    plan = _StubPlan(must_touch_files=[
        "app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
        "app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
    ])
    outcomes = [
        BatchOutcome(
            file_path="app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
            role="must_touch",
            status="phantom_no_change",
            terminal_kind="PHANTOM_NO_CHANGE",
            reason="claimed MapView exists but quote not found",
            failed_quotes=[
                {
                    "claim": "Already integrates a MapView",
                    "quote_preview": "AndroidView { MapView(it) }",
                    "reason": "quote_not_found",
                }
            ],
        ),
        BatchOutcome(
            file_path="app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
            role="must_touch",
            status="patched",
            changed=True,
            diff_stats={"added": 129, "removed": 1},
        ),
    ]
    verdict = check_coverage(outcomes, plan)
    assert verdict.kind == "phantom_no_change_unrecoverable"
    # Pipeline must not continue: 1/2 patched isn't enough.
    assert verdict.diagnostic["must_touch_patched"] == 1
    assert verdict.diagnostic["must_touch_phantom"] == 1
