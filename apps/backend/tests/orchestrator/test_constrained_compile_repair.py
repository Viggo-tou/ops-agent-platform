from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.service import (  # noqa: E402
    _compile_repair_intent_dropped,
    _count_intent_preservation,
)


REL_PATH = "app/src/main/java/example/JobPostingFragment.kt"


def _first_attempt(lines: list[str]) -> str:
    added = "\n".join(f"+    {line}" if line else "+" for line in lines)
    return (
        f"diff --git a/{REL_PATH} b/{REL_PATH}\n"
        "index 1111111..2222222 100644\n"
        f"--- a/{REL_PATH}\n"
        f"+++ b/{REL_PATH}\n"
        "@@ -1,2 +1,8 @@\n"
        " fun bind() {\n"
        f"{added}\n"
        " }\n"
    )


def _repair_removing(lines: list[str]) -> str:
    removed = "\n".join(f"-    {line}" if line else "-" for line in lines)
    return (
        f"diff --git a/{REL_PATH} b/{REL_PATH}\n"
        "index 2222222..3333333 100644\n"
        f"--- a/{REL_PATH}\n"
        f"+++ b/{REL_PATH}\n"
        "@@ -1,8 +1,3 @@\n"
        " fun bind() {\n"
        f"{removed}\n"
        "+}\n"
    )


INTENT_LINES = [
    "LaunchedEffect(Unit) {",
    "val homeAddress = SessionManager.getHomeAddress(context)",
    "if (homeAddress.isNotBlank()) {",
    "viewModel.address = homeAddress",
    "viewModel.addressSource = AddressSource.Session",
]


def test_intent_verifier_preserves_all_five_lines() -> None:
    ratio = _count_intent_preservation(
        _first_attempt(INTENT_LINES),
        REL_PATH,
        _repair_removing([]),
        "",
    )

    assert ratio == pytest.approx(1.0)


def test_intent_verifier_fails_when_all_five_lines_removed() -> None:
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(INTENT_LINES),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(INTENT_LINES),
        baseline_content="",
        threshold=0.4,
    )

    assert intent_count == 5
    assert ratio == pytest.approx(0.0)
    assert dropped is True


def test_intent_verifier_passes_when_three_of_five_preserved() -> None:
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(INTENT_LINES),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(INTENT_LINES[3:]),
        baseline_content="",
        threshold=0.4,
    )

    assert intent_count == 5
    assert ratio == pytest.approx(0.6)
    assert dropped is False


def test_intent_verifier_fails_when_one_of_five_preserved() -> None:
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(INTENT_LINES),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(INTENT_LINES[1:]),
        baseline_content="",
        threshold=0.4,
    )

    assert intent_count == 5
    assert ratio == pytest.approx(0.2)
    assert dropped is True


def test_whitespace_only_lines_are_not_counted_as_intent() -> None:
    lines = ["realIntent()", "", "   ", "secondIntent()"]
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(lines),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(["realIntent()", "secondIntent()"]),
        baseline_content="",
        threshold=0.4,
    )

    assert intent_count == 2
    assert ratio == pytest.approx(0.0)
    assert dropped is True


def test_comment_only_lines_are_not_counted_as_intent() -> None:
    lines = ["// explain the new flow", "# generated note", "realIntent()"]
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(lines),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(["realIntent()"]),
        baseline_content="",
        threshold=0.4,
    )

    assert intent_count == 1
    assert ratio == pytest.approx(0.0)
    assert dropped is True


def test_threshold_zero_disables_intent_verification() -> None:
    dropped, ratio, intent_count = _compile_repair_intent_dropped(
        first_attempt_diff=_first_attempt(INTENT_LINES),
        rel_path=REL_PATH,
        repair_diff=_repair_removing(INTENT_LINES),
        baseline_content="",
        threshold=0,
    )

    assert intent_count == 5
    assert ratio == pytest.approx(1.0)
    assert dropped is False
