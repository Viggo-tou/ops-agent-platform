"""Per-model codegen context budgets (Tier 2 categorical budgeter).

Why this module. The default ``EvidencePackBudget`` (6 files, 18 KB
total, 6 KB per file) was tuned for DeepSeek-V4-Pro's reliable codegen
window (~ 30k tokens, falls apart past ~ 50k). Other providers have
very different sweet spots — Claude Sonnet handles 100 KB+ comfortably,
GPT-4o-mini sits between, Mistral has a tighter window than DeepSeek.
Using one budget for all providers either starves the big-window
models (we measured "harness contribution" lower than it could be) or
overflows the small-window ones (the SWE-bench 0/4 baseline).

This module owns the per-provider profile registry and exposes
``budget_for_codegen_provider(provider, settings)``. Settings overrides
always win — operators can pin a budget per environment via the same
``OPS_AGENT_EVIDENCE_PACK_*`` env vars they already use, and the
profile only fills in the ones they didn't set.

Profile values are deliberately conservative. The win is going from
"one-size-fits-all" to "model-aware"; over-tuning per model is a
separate exercise once we have measured numbers on each.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.evidence_pack import EvidencePackBudget


@dataclass(frozen=True)
class CodegenModelProfile:
    """Reliable codegen window for a given provider.

    Numbers are bytes of *source content* the model sees inside
    ``=== FILE CONTEXT ===``, not total prompt length. The prompt
    builder spends ~ 4-8 KB on system prompt, plan, retry suffixes,
    so the *raw* prompt is larger than these caps.
    """

    provider: str
    max_files: int
    max_total_bytes: int
    max_per_file_bytes: int
    rationale: str = ""


# Keys are the codegen provider names from settings.codegen_provider.
# "default" is the safety net for unknown providers — matches
# DeepSeek's profile because DeepSeek is our most-tested baseline.
_PROFILES: dict[str, CodegenModelProfile] = {
    "deepseek": CodegenModelProfile(
        provider="deepseek",
        max_files=6,
        max_total_bytes=18_000,
        max_per_file_bytes=6_000,
        rationale=(
            "Empirically observed: DeepSeek-V4-Pro produces reliable diffs "
            "below ~ 20 KB of source; 90-140 KB injections during the "
            "2026-05-09 SWE-bench baseline gave 0/4. Holding the line at "
            "18 KB is the regression cap."
        ),
    ),
    "openai": CodegenModelProfile(
        provider="openai",
        max_files=8,
        max_total_bytes=30_000,
        max_per_file_bytes=8_000,
        rationale=(
            "GPT-4o-mini handles ~ 50k tokens of input reliably; budget at "
            "30 KB of source after prompt overhead. Increase if a future "
            "operator uses GPT-4o or GPT-5 with higher reliable windows."
        ),
    ),
    "anthropic": CodegenModelProfile(
        provider="anthropic",
        max_files=12,
        max_total_bytes=80_000,
        max_per_file_bytes=20_000,
        rationale=(
            "Claude Sonnet/Opus reliably handle 100k+ tokens; we cap at "
            "80 KB of source so prompt + system + retry suffix still fit "
            "in 200k context. Per-file 20 KB lets larger Python modules "
            "be considered without truncation."
        ),
    ),
    "claude_code": CodegenModelProfile(
        provider="claude_code",
        max_files=12,
        max_total_bytes=80_000,
        max_per_file_bytes=20_000,
        rationale="Same engine as anthropic; CLI wrapper has identical window.",
    ),
    "codex": CodegenModelProfile(
        provider="codex",
        max_files=10,
        max_total_bytes=60_000,
        max_per_file_bytes=15_000,
        rationale=(
            "GPT-5.4 via Codex CLI sits between OpenAI API and Claude in "
            "practice; 60 KB is conservative."
        ),
    ),
    "minimax": CodegenModelProfile(
        provider="minimax",
        max_files=6,
        max_total_bytes=18_000,
        max_per_file_bytes=6_000,
        rationale="MiniMax-M2.7 is mid-tier; treat the same as DeepSeek.",
    ),
    "ollama": CodegenModelProfile(
        provider="ollama",
        max_files=4,
        max_total_bytes=10_000,
        max_per_file_bytes=4_000,
        rationale=(
            "Local Ollama models (qwen3.5 etc.) have small reliable "
            "windows; tighter cap to keep latency manageable too."
        ),
    ),
    "mock": CodegenModelProfile(
        provider="mock",
        max_files=6,
        max_total_bytes=18_000,
        max_per_file_bytes=6_000,
        rationale="Test provider; matches default.",
    ),
}

_DEFAULT_PROFILE = _PROFILES["deepseek"]


def get_profile(provider: str | None) -> CodegenModelProfile:
    """Look up a model profile. Unknown / None falls back to the
    default (DeepSeek-shaped) profile.
    """
    if not provider:
        return _DEFAULT_PROFILE
    return _PROFILES.get(provider, _DEFAULT_PROFILE)


def budget_for_codegen_provider(
    provider: str | None, settings: object | None = None
) -> EvidencePackBudget:
    """Return the codegen evidence_pack budget for ``provider``.

    Operator overrides via settings always win:
      - ``evidence_pack_max_files``
      - ``evidence_pack_max_total_bytes``
      - ``evidence_pack_max_per_file_bytes``

    When a setting is unset (or zero/None), fall back to the model
    profile's value. This keeps existing deployments that pinned
    explicit values working unchanged.
    """
    profile = get_profile(provider)

    def _from_settings(name: str, default: int) -> int:
        if settings is None:
            return default
        raw = getattr(settings, name, None)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    return EvidencePackBudget(
        max_files=_from_settings("evidence_pack_max_files", profile.max_files),
        max_total_bytes=_from_settings(
            "evidence_pack_max_total_bytes", profile.max_total_bytes
        ),
        max_per_file_bytes=_from_settings(
            "evidence_pack_max_per_file_bytes", profile.max_per_file_bytes
        ),
    )
