"""Focused tests for semantic_review hard-block gating."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import (  # noqa: E402
    _semantic_review_should_attempt_repair,
    _semantic_review_should_block_on_exhausted,
)


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
