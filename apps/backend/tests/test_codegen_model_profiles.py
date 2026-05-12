"""Tests for the per-model codegen budget profile registry (Tier 2)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.codegen_model_profiles import (
    budget_for_codegen_provider,
    get_profile,
)


def test_known_provider_returns_named_profile():
    p = get_profile("deepseek")
    assert p.provider == "deepseek"
    assert p.max_total_bytes == 18_000


def test_unknown_provider_falls_back_to_default():
    p = get_profile("doesnt_exist")
    assert p.provider == "deepseek"  # default profile


def test_none_provider_falls_back_to_default():
    p = get_profile(None)
    assert p.provider == "deepseek"


def test_budget_for_provider_uses_profile_when_settings_empty():
    settings = SimpleNamespace()
    budget = budget_for_codegen_provider("anthropic", settings)
    assert budget.max_files == 12
    assert budget.max_total_bytes == 80_000
    assert budget.max_per_file_bytes == 20_000


def test_budget_for_provider_settings_override_wins():
    settings = SimpleNamespace(
        evidence_pack_max_files=3,
        evidence_pack_max_total_bytes=5_000,
        evidence_pack_max_per_file_bytes=1_000,
    )
    budget = budget_for_codegen_provider("anthropic", settings)
    # Operator pinned smaller values; profile defaults are ignored.
    assert budget.max_files == 3
    assert budget.max_total_bytes == 5_000
    assert budget.max_per_file_bytes == 1_000


def test_budget_for_provider_zero_setting_falls_back_to_profile():
    """Defensive: an env var set to ``0`` shouldn't zero-out the budget;
    fall back to the profile value."""
    settings = SimpleNamespace(
        evidence_pack_max_files=0,
        evidence_pack_max_total_bytes=None,
        evidence_pack_max_per_file_bytes="",
    )
    budget = budget_for_codegen_provider("openai", settings)
    assert budget.max_files == 8           # OpenAI profile
    assert budget.max_total_bytes == 30_000
    assert budget.max_per_file_bytes == 8_000


def test_budget_for_provider_no_settings_uses_profile():
    budget = budget_for_codegen_provider("ollama", None)
    assert budget.max_files == 4
    assert budget.max_total_bytes == 10_000


def test_budget_for_unknown_provider_uses_default_profile():
    budget = budget_for_codegen_provider("totally_unknown", SimpleNamespace())
    # Default = deepseek profile.
    assert budget.max_files == 6
    assert budget.max_total_bytes == 18_000


def test_budget_partial_setting_override_mixes_with_profile():
    settings = SimpleNamespace(
        evidence_pack_max_total_bytes=50_000,  # override only the total
    )
    budget = budget_for_codegen_provider("deepseek", settings)
    assert budget.max_files == 6  # from profile
    assert budget.max_total_bytes == 50_000  # from settings
    assert budget.max_per_file_bytes == 6_000  # from profile


@pytest.mark.parametrize(
    "provider,expected_total",
    [
        ("deepseek", 18_000),
        ("openai", 30_000),
        ("anthropic", 80_000),
        ("claude_code", 80_000),
        ("codex", 60_000),
        ("minimax", 18_000),
        ("ollama", 10_000),
        ("mock", 18_000),
    ],
)
def test_each_known_provider_has_distinct_or_aligned_budget(provider, expected_total):
    p = get_profile(provider)
    assert p.max_total_bytes == expected_total
