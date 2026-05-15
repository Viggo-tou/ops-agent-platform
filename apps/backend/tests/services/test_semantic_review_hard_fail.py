"""Focused tests for semantic_review hard-block gating."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import (  # noqa: E402
    _build_semantic_review_spec_text,
    _semantic_review_actionable_quality_findings,
    _semantic_review_exhausted_block_reason,
    _semantic_review_filter_after_verified_gates,
    _semantic_review_high_count,
    _semantic_review_should_attempt_quality_refine,
    _semantic_review_should_attempt_repair,
    _semantic_review_should_block_on_exhausted,
)


def test_semantic_review_spec_includes_artifact_target_contract():
    task = SimpleNamespace(
        request_text="develop P69-8",
        translation_json={
            "normalized_request": "develop P69-8",
            "search_queries": ["develop P69-8"],
            "grounding_terms": ["develop", "P69-8"],
        },
    )
    plan = SimpleNamespace(
        objective="Implement the Jira issue.",
        request_summary="Privacy Update: Firebase rule",
        change_summary=(
            "The Firebase database rules need to be changed from public "
            "to private."
        ),
        change_explanation=(
            "Create or update database.rules.json to enforce private access."
        ),
        must_touch_files=[],
        expected_new_files=["database.rules.json"],
        acceptance_tests=[
            {
                "kind": "diff_contains_pattern_in_file",
                "file": "database.rules.json",
                "pattern": '".read"',
                "rationale": "Rules file carries the privacy change.",
            }
        ],
    )

    text = _build_semantic_review_spec_text(task=task, plan=plan)

    assert "public to private" in text
    assert "expected_new_files: database.rules.json" in text
    assert "artifact_only: true" in text
    assert "diff_contains_pattern_in_file" in text


def test_high_finding_blocks_when_enabled():
    settings = SimpleNamespace(
        semantic_review_high_blocks_on_exhausted=True,
        semantic_review_pass_threshold=80,
    )
    sr_report = SimpleNamespace(
        high_severity_count=2,
        completeness_pct=75,
    )

    assert _semantic_review_should_block_on_exhausted(sr_report, settings) is True
    assert (
        _semantic_review_exhausted_block_reason(sr_report, settings)
        == "semantic_review_unresolved_high"
    )


def test_high_finding_passes_when_disabled():
    settings = SimpleNamespace(
        semantic_review_high_blocks_on_exhausted=False,
        semantic_review_pass_threshold=80,
    )
    sr_report = SimpleNamespace(
        high_severity_count=2,
        completeness_pct=75,
    )

    assert _semantic_review_should_block_on_exhausted(sr_report, settings) is False


def test_low_completeness_blocks_even_without_grounded_high_findings():
    settings = SimpleNamespace(
        semantic_review_blocks_on_exhausted=True,
        semantic_review_pass_threshold=80,
    )
    sr_report = SimpleNamespace(
        passed=False,
        high_severity_count=lambda: 0,
        completeness_pct=45,
        findings=[],
    )

    assert _semantic_review_should_block_on_exhausted(sr_report, settings) is True
    assert (
        _semantic_review_exhausted_block_reason(sr_report, settings)
        == "semantic_review_low_completeness"
    )


def test_semantic_review_pass_does_not_block_on_exhausted():
    settings = SimpleNamespace(
        semantic_review_blocks_on_exhausted=True,
        semantic_review_pass_threshold=80,
    )
    sr_report = SimpleNamespace(
        passed=True,
        high_severity_count=lambda: 0,
        completeness_pct=90,
        findings=[],
    )

    assert _semantic_review_should_block_on_exhausted(sr_report, settings) is False
    assert _semantic_review_exhausted_block_reason(sr_report, settings) is None


def test_semantic_review_does_not_repair_without_grounded_findings():
    sr_report = SimpleNamespace(
        passed=False,
        completeness_pct=65,
        findings=[],
    )

    assert (
        _semantic_review_should_attempt_repair(
            sr_report,
            sr_round=1,
            max_repair_rounds=2,
        )
        is False
    )


def test_semantic_review_repairs_grounded_findings_before_budget_exhausted():
    sr_report = SimpleNamespace(
        passed=False,
        completeness_pct=68,
        findings=[{"severity": "high", "description": "grounded"}],
    )

    assert (
        _semantic_review_should_attempt_repair(
            sr_report,
            sr_round=0,
            max_repair_rounds=2,
        )
        is True
    )


def test_semantic_review_does_not_repair_after_verified_gates_pass():
    sr_report = SimpleNamespace(
        passed=False,
        completeness_pct=60,
        findings=[{"severity": "high", "description": "grounded"}],
    )

    assert (
        _semantic_review_should_attempt_repair(
            sr_report,
            sr_round=0,
            max_repair_rounds=2,
            verified_gates_passed=True,
        )
        is False
    )


def test_semantic_review_drops_compile_finding_contradicted_by_gates():
    pipeline_state = {
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True, "verdict_kind": "complete"},
        "acceptance_check_done": True,
        "symbol_graph_done": True,
        "symbol_graph": {"passed": True},
    }
    sr_report = SimpleNamespace(
        passed=False,
        pass_threshold=80,
        completeness_pct=60,
        summary="Reviewer found a compile issue.",
        findings=[
            {
                "severity": "high",
                "category": "compile_error",
                "description": (
                    "CustomerSignup.kt uses Locale.getDefault() without "
                    "importing java.util.Locale and will not compile."
                ),
                "evidence_quote": "Geocoder(ctx, Locale.getDefault())",
                "suggested_fix": "Add import java.util.Locale.",
            },
            {
                "severity": "low",
                "category": "style",
                "description": "Copy could be clearer.",
            },
        ],
    )

    filtered, dropped = _semantic_review_filter_after_verified_gates(
        sr_report,
        pipeline_state=pipeline_state,
    )

    assert len(dropped) == 1
    assert _semantic_review_high_count(filtered) == 0
    assert len(filtered.findings) == 1


def test_semantic_review_drops_unbound_state_finding_when_viewmodel_is_compose_state():
    pipeline_state = {
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True, "verdict_kind": "complete"},
        "acceptance_check_done": True,
        "symbol_graph_done": True,
        "symbol_graph": {"passed": True},
    }
    sr_report = SimpleNamespace(
        passed=False,
        pass_threshold=80,
        completeness_pct=30,
        summary="Reviewer says the UI cannot recompose.",
        findings=[
            {
                "severity": "high",
                "category": "unbound_field",
                "description": (
                    "ViewModel properties (locationAddress, latitude, longitude) "
                    "are assigned directly but are not backed by Compose state, "
                    "so the UI never recomposes."
                ),
                "evidence_quote": "viewModel.locationAddress = homeAddress",
                "suggested_fix": "Wrap the properties in mutableStateOf.",
            }
        ],
    )
    file_contents = {
        "app/src/main/java/com/example/handyman/JobPostingViewModel.kt": """
            import androidx.compose.runtime.mutableStateOf
            class JobPostingViewModel {
                var locationAddress by mutableStateOf("")
                var latitude by mutableStateOf(0.0)
                var longitude by mutableStateOf(0.0)
            }
        """,
    }

    filtered, dropped = _semantic_review_filter_after_verified_gates(
        sr_report,
        pipeline_state=pipeline_state,
        file_contents=file_contents,
    )

    assert len(dropped) == 1
    assert _semantic_review_high_count(filtered) == 0
    assert filtered.passed is False  # completeness is still below threshold


def test_semantic_review_keeps_unbound_state_finding_without_viewmodel_fact():
    pipeline_state = {
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True, "verdict_kind": "complete"},
        "acceptance_check_done": True,
        "symbol_graph_done": True,
        "symbol_graph": {"passed": True},
    }
    sr_report = SimpleNamespace(
        passed=False,
        pass_threshold=80,
        completeness_pct=30,
        summary="Reviewer says the UI cannot recompose.",
        findings=[
            {
                "severity": "high",
                "category": "unbound_field",
                "description": "viewModel.locationAddress is not backed by Compose state.",
                "evidence_quote": "viewModel.locationAddress = homeAddress",
                "suggested_fix": "Wrap the property in mutableStateOf.",
            }
        ],
    )

    filtered, dropped = _semantic_review_filter_after_verified_gates(
        sr_report,
        pipeline_state=pipeline_state,
        file_contents={},
    )

    assert dropped == []
    assert _semantic_review_high_count(filtered) == 1


def test_semantic_review_keeps_unbound_state_finding_when_only_some_fields_are_state():
    pipeline_state = {
        "compile_gate": {"passed": True},
        "contract_coverage_verdict": {"ok": True, "verdict_kind": "complete"},
        "acceptance_check_done": True,
        "symbol_graph_done": True,
        "symbol_graph": {"passed": True},
    }
    sr_report = SimpleNamespace(
        passed=False,
        pass_threshold=80,
        completeness_pct=30,
        summary="Reviewer says the UI cannot recompose.",
        findings=[
            {
                "severity": "high",
                "category": "unbound_field",
                "description": (
                    "ViewModel properties (locationAddress, latitude, longitude) "
                    "are not backed by Compose state."
                ),
                "evidence_quote": "viewModel.locationAddress = homeAddress",
                "suggested_fix": "Wrap the properties in mutableStateOf.",
            }
        ],
    )
    file_contents = {
        "app/src/main/java/com/example/handyman/JobPostingViewModel.kt": """
            import androidx.compose.runtime.mutableStateOf
            class JobPostingViewModel {
                var locationAddress by mutableStateOf("")
            }
        """,
    }

    filtered, dropped = _semantic_review_filter_after_verified_gates(
        sr_report,
        pipeline_state=pipeline_state,
        file_contents=file_contents,
    )

    assert dropped == []
    assert _semantic_review_high_count(filtered) == 1


def test_semantic_quality_refine_requires_grounded_medium_finding():
    sr_report = SimpleNamespace(
        passed=True,
        completeness_pct=90,
        findings=[],
    )

    assert _semantic_review_actionable_quality_findings(sr_report) == []
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=True,
        )
        is False
    )


def test_semantic_quality_refine_runs_for_passed_grounded_medium_finding():
    finding = SimpleNamespace(
        severity="medium",
        description="Persisted payload omits the validated address field.",
        evidence_quote='"address" to addressText',
    )
    sr_report = SimpleNamespace(
        passed=True,
        completeness_pct=90,
        high_severity_count=0,
        findings=[finding],
    )

    assert _semantic_review_actionable_quality_findings(sr_report) == [finding]
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=True,
        )
        is True
    )


def test_semantic_quality_refine_ignores_low_or_ungrounded_findings():
    sr_report = SimpleNamespace(
        passed=True,
        completeness_pct=90,
        findings=[
            {
                "severity": "low",
                "description": "Naming could be clearer.",
                "evidence_quote": "val name",
            },
            {
                "severity": "medium",
                "description": "Could improve edge handling.",
                "evidence_quote": "",
            },
        ],
    )

    assert _semantic_review_actionable_quality_findings(sr_report) == []
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=True,
        )
        is False
    )


def test_semantic_quality_refine_respects_disable_threshold_attempts_and_gates():
    sr_report = SimpleNamespace(
        passed=True,
        completeness_pct=95,
        high_severity_count=0,
        findings=[
            {
                "severity": "medium",
                "description": "Persisted payload omits the validated address field.",
                "evidence_quote": '"address" to addressText',
            }
        ],
    )

    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            enabled=False,
            verified_gates_passed=True,
        )
        is False
    )
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=True,
        )
        is False
    )
    sr_report.completeness_pct = 90
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=1,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=True,
        )
        is False
    )
    assert (
        _semantic_review_should_attempt_quality_refine(
            sr_report,
            refine_attempts=0,
            max_refine_attempts=1,
            quality_threshold=95,
            verified_gates_passed=False,
        )
        is False
    )
