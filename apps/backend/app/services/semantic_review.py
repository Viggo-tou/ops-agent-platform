"""Semantic review gate (R1).

LLM-based completeness + risk reviewer that runs after compile_gate +
SymbolGraph pass. Produces structured findings:

    {
      "completeness_pct": 70,
      "summary": "...",
      "findings": [
        {
          "file": "JobPostingFragment.kt",
          "line_start": 42,
          "line_end": 45,
          "severity": "high",
          "category": "orphan_ui",
          "description": "EditText etAddress added to layout but no Kotlin binding",
          "evidence_quote": "+            <EditText\\n+                android:id=\"@+id/etAddress\"",
          "suggested_fix": "Add findViewById(R.id.etAddress) and bind to viewModel.locationAddress"
        },
        ...
      ]
    }

Anti-hallucination guards:
    1. JSON schema validation via Pydantic.
    2. evidence_quote must appear (substring) in the supplied diff —
       otherwise the finding is dropped as fabricated.
    3. file referenced by each finding must appear in the supplied diff.
    4. severity must be one of {high, medium, low}.

Threshold: gate passes when ``completeness_pct >= pass_threshold`` AND
zero findings with ``severity == "high"``. Caller (orchestrator) feeds
non-passing reports into the existing repair loop pattern (same as
A from feature_presence repair, see service.py).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.core.config import Settings
from app.services.llm_cache import cached_http_post


logger = logging.getLogger("semantic_review")


_SEVERITIES: tuple[str, ...] = ("high", "medium", "low")


class SemanticReviewError(Exception):
    """Raised when the LLM call or response parsing fails."""


class SemanticReviewUnavailable(SemanticReviewError):
    """v15 Ticket 5 (2026-05-11): raised when both the initial review and
    the focused JSON repair pass fail to produce parseable output.

    Carries the structured diagnostic so the orchestrator can record the
    gate as ``status=unavailable`` (visible in latest_result_json and
    event log) instead of silently skipping. This is distinct from
    SemanticReviewError, which still represents infrastructure errors
    (provider auth, timeout, etc.) that should propagate as such.
    """

    def __init__(
        self,
        *,
        reason: str,
        provider: str,
        review_attempts: int,
        repair_attempted: bool,
        raw_preview: str,
        underlying: str,
    ) -> None:
        super().__init__(
            f"semantic_review_unavailable: {reason} "
            f"(provider={provider}, review_attempts={review_attempts}, "
            f"repair_attempted={repair_attempted})"
        )
        self.reason = reason
        self.provider = provider
        self.review_attempts = review_attempts
        self.repair_attempted = repair_attempted
        self.raw_preview = raw_preview
        self.underlying = underlying

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": self.reason,
            "provider": self.provider,
            "review_attempts": self.review_attempts,
            "repair_attempted": self.repair_attempted,
            "raw_preview": self.raw_preview,
            "underlying_error": self.underlying[:500],
        }


# ---- Pydantic schema for structured LLM output -----------------------------

class _RawFinding(BaseModel):
    file: str
    line_start: int = Field(default=0, ge=0)
    line_end: int = Field(default=0, ge=0)
    severity: Literal["high", "medium", "low"]
    category: str = Field(default="general", max_length=64)
    description: str = Field(min_length=1, max_length=600)
    # Cap raised from 400 to 1500 — DeepSeek + claude-haiku occasionally
    # cite a multi-line function header as evidence; rejecting the whole
    # review just because one quote is long is too brittle. The grounding
    # check still requires the quote to be a real diff substring.
    evidence_quote: str = Field(default="", max_length=1500)
    suggested_fix: str = Field(default="", max_length=600)


class _RawReviewOutput(BaseModel):
    completeness_pct: int = Field(ge=0, le=100)
    summary: str = Field(default="", max_length=600)
    findings: list[_RawFinding] = Field(default_factory=list)


# ---- Public dataclasses ----------------------------------------------------

@dataclass(frozen=True)
class SemanticReviewFinding:
    file: str
    line_start: int
    line_end: int
    severity: str
    category: str
    description: str
    evidence_quote: str
    suggested_fix: str


@dataclass(frozen=True)
class SemanticReviewReport:
    passed: bool
    completeness_pct: int
    summary: str
    findings: tuple[SemanticReviewFinding, ...]
    pass_threshold: int
    total_findings_raw: int  # how many came back before validation
    findings_dropped_no_evidence: int
    provider_name: str
    # v15 Ticket 5 (2026-05-11): tri-state status. ``"passed"`` /
    # ``"failed"`` mirror the legacy boolean; ``"unavailable"`` means
    # the reviewer could not produce parseable output even after the
    # JSON repair pass — the gate is neither pass nor fail, the
    # information is just missing. Callers must treat ``unavailable``
    # as "human review required" rather than silently continuing.
    status: str = "passed"
    unavailable_reason: str = ""
    raw_preview: str = ""
    review_attempts: int = 1
    repair_attempted: bool = False

    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    @property
    def is_unavailable(self) -> bool:
        return self.status == "unavailable"

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "passed": self.passed,
            "completeness_pct": self.completeness_pct,
            "summary": self.summary,
            "pass_threshold": self.pass_threshold,
            "total_findings_raw": self.total_findings_raw,
            "findings_dropped_no_evidence": self.findings_dropped_no_evidence,
            "high_severity_count": self.high_severity_count(),
            "provider_name": self.provider_name,
            "review_attempts": self.review_attempts,
            "repair_attempted": self.repair_attempted,
            "findings": [
                {
                    "file": f.file,
                    "line_start": f.line_start,
                    "line_end": f.line_end,
                    "severity": f.severity,
                    "category": f.category,
                    "description": f.description,
                    "evidence_quote": f.evidence_quote,
                    "suggested_fix": f.suggested_fix,
                }
                for f in self.findings
            ],
        }
        if self.status == "unavailable":
            payload["unavailable_reason"] = self.unavailable_reason
            payload["raw_preview"] = self.raw_preview
        return payload

    @classmethod
    def unavailable(
        cls,
        *,
        reason: str,
        provider: str,
        review_attempts: int,
        repair_attempted: bool,
        raw_preview: str,
        pass_threshold: int = 80,
    ) -> "SemanticReviewReport":
        """Factory for the unavailable status — produces a report with
        no findings and clear flags so downstream code never mistakes
        this for a passing review."""
        return cls(
            passed=False,
            completeness_pct=0,
            summary="Reviewer output was not parseable as JSON; gate unavailable.",
            findings=(),
            pass_threshold=pass_threshold,
            total_findings_raw=0,
            findings_dropped_no_evidence=0,
            provider_name=provider,
            status="unavailable",
            unavailable_reason=reason,
            raw_preview=raw_preview[:1000],
            review_attempts=review_attempts,
            repair_attempted=repair_attempted,
        )

    def repair_prompt_lines(self) -> list[str]:
        """Render findings as actionable bullet points the codegen can
        feed through its repair pipeline."""
        out: list[str] = []
        for f in self.findings:
            line_part = (
                f"{f.file}:{f.line_start}-{f.line_end}"
                if f.line_start and f.line_end
                else f.file
            )
            out.append(
                f"  - [{f.severity.upper()}|{f.category}] {line_part}: "
                f"{f.description}"
                + (f"  -- Suggested: {f.suggested_fix}" if f.suggested_fix else "")
            )
        return out


# ---- JSON extraction (markdown-tolerant) -----------------------------------

def _extract_json_object(text: str) -> str:
    """LLMs often wrap JSON in markdown fences. Strip them and return the
    longest balanced ``{...}`` slice. Tolerates LLM quirks:
      - markdown ```json ``` fences
      - leading/trailing prose
      - DeepSeek's thinking block embedded before/around JSON
      - trailing commas (best-effort)
    """
    if not text:
        return ""
    s = text.strip()
    # Strip markdown fence
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()
    # Drop common prose preludes that LLMs prepend even when asked not to
    for prelude in (
        "Here is the JSON", "Here's the JSON", "Sure, here's", "The review:",
        "JSON:", "Output:", "Analysis:",
    ):
        if s.lower().startswith(prelude.lower()):
            s = s.split(":", 1)[-1].lstrip(" \n:")
    if s.startswith("{"):
        return s
    # Find the FIRST `{` and return from there to the matching `}`
    # using brace-depth counting (handles JSON containing strings with `}`).
    start = s.find("{")
    if start < 0:
        return s
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    # Unbalanced — return what we have from `{` onward; json.loads will
    # complain with a clearer message
    return s[start:]


def _extract_text_with_thinking_fallback(content_blocks: list) -> str:
    """Pull text from Anthropic-compat /v1/messages content blocks.
    Prefers `type=="text"` blocks; if none have non-empty text (a real
    DeepSeek quirk where it returns only a `thinking` block), falls back
    to the `thinking` content as a last resort. Empirical: DeepSeek's
    `thinking` field sometimes contains the actual JSON output when the
    final `text` block is empty."""
    text_parts = [
        block.get("text", "") for block in content_blocks
        if block.get("type") == "text"
    ]
    joined = "".join(text_parts).strip()
    if joined:
        return joined
    thinking_parts = [
        block.get("thinking", "") for block in content_blocks
        if block.get("type") == "thinking"
    ]
    return "".join(thinking_parts).strip()


def parse_review_output(content: str) -> _RawReviewOutput:
    text = _extract_json_object(content)
    if not text:
        raise SemanticReviewError("LLM returned empty response")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SemanticReviewError(
            f"LLM response was not valid JSON: {exc}"
        ) from exc
    try:
        return _RawReviewOutput.model_validate(data)
    except ValidationError as exc:
        raise SemanticReviewError(
            f"LLM response did not match schema: {exc}"
        ) from exc


# ---- Anti-hallucination grounding ------------------------------------------

def _normalize_for_match(s: str) -> str:
    """Loose normalization: collapse all whitespace runs to single space.
    Lets us tolerate the LLM re-formatting evidence quotes slightly."""
    return re.sub(r"\s+", " ", s).strip()


def _is_finding_grounded(
    finding: _RawFinding,
    *,
    diff_normalized: str,
    diff_files: set[str],
) -> bool:
    """A finding is grounded if:
      - the file it references appears in the diff, AND
      - either:
          (a) it has an evidence_quote that is a normalized substring of
              the diff (LLM cited real text), OR
          (b) it has no evidence_quote AND severity is "low" (advisory
              findings are allowed without quotes)
    """
    file_basename = finding.file.replace("\\", "/").split("/")[-1]
    file_match = any(
        finding.file == f or file_basename == f.split("/")[-1]
        for f in diff_files
    )
    if not file_match:
        return False
    if finding.evidence_quote:
        ev = _normalize_for_match(finding.evidence_quote)
        # Drop quote if too short to be meaningful (1-2 chars are noise)
        if len(ev) < 5:
            return False
        return ev in diff_normalized
    return finding.severity == "low"


def _files_in_diff(diff: str) -> set[str]:
    out: set[str] = set()
    for line in diff.split("\n"):
        if line.startswith("+++ b/"):
            out.add(line[6:].strip())
        elif line.startswith("--- a/"):
            out.add(line[6:].strip())
    return out


# ---- Prompt construction ---------------------------------------------------

_SYSTEM_PROMPT = (
    "You are a strict senior code reviewer. Read the SPEC, the unified "
    "DIFF the agent produced, and the post-edit FILES. Score completeness "
    "0-100 and list concrete gaps as findings.\n\n"
    "RULES:\n"
    "1. completeness_pct = % of spec requirements actually implemented in "
    "the diff (not declared, not commented — actually wired and reachable).\n"
    "2. For every gap, emit ONE finding with: file, line range, severity "
    "(high/medium/low), category (orphan_ui / missing_route / hardcoded_stub "
    "/ unbound_field / null_handling / race_condition / api_mismatch / "
    "general), description (one sentence), evidence_quote (5-100 chars "
    "VERBATIM from the diff that proves the gap), suggested_fix.\n"
    "3. severity=high  → blocks merge (orphan UI, missing route target, "
    "hardcoded fake values, unhandled null on critical path).\n"
    "4. severity=medium → reviewer should request changes (missing edge "
    "case, weak validation).\n"
    "5. severity=low   → advisory (style, naming).\n"
    "6. evidence_quote MUST be a VERBATIM substring of the diff. If you "
    "cannot quote, omit the quote AND mark severity=low.\n"
    "7. Treat the SPEC and plan target contract as authoritative. If the "
    "SPEC declares an artifact-only change through expected_new_files and "
    "does not request application source code, do not require unrelated "
    "feature code or tests. Review whether the artifact content implements "
    "the declared requirement.\n"
    "8. Output ONLY the JSON. No prose, no markdown fences.\n\n"
    "JSON schema:\n"
    '{"completeness_pct": int, "summary": str, "findings": ['
    '{"file": str, "line_start": int, "line_end": int, '
    '"severity": "high"|"medium"|"low", "category": str, '
    '"description": str, "evidence_quote": str, "suggested_fix": str}'
    "]}"
)


def build_user_prompt(
    *,
    spec_text: str,
    diff: str,
    file_contents: dict[str, str] | None = None,
    file_content_cap: int = 12000,
) -> str:
    """Compose the user-facing review prompt from the task spec, the
    agent's diff, and (optionally) the post-edit file bodies.

    Total file content capped at ``file_content_cap`` bytes to keep
    the prompt within reasonable context budgets.
    """
    parts: list[str] = []
    parts.append("=== SPEC (what the user asked the agent to implement) ===")
    parts.append((spec_text or "").strip()[:4000])
    parts.append("")
    parts.append("=== DIFF (agent's complete output) ===")
    parts.append("```diff")
    parts.append((diff or "").strip()[:18000])
    parts.append("```")
    parts.append("")
    if file_contents:
        parts.append("=== POST-EDIT FILES (truncated) ===")
        used = 0
        for path, body in file_contents.items():
            if used >= file_content_cap:
                parts.append(f"[remaining files omitted to fit context]")
                break
            chunk = (body or "")[: max(file_content_cap - used, 1000)]
            used += len(chunk)
            parts.append(f"--- FILE: {path} ---")
            parts.append(chunk)
            parts.append("")
    parts.append("Now produce the JSON review.")
    return "\n".join(parts)


# ---- LLM provider dispatch -------------------------------------------------

# L2 (Awais lesson #2): retry suffix added to the user prompt on a second
# attempt when the first returned empty / unparsable. Reminds DeepSeek that
# we want JSON ONLY — no prose, no thinking-only response.
_RETRY_SUFFIX = (
    "\n\nIMPORTANT: Output ONLY the raw JSON object. NO prose. NO markdown "
    "code fences. NO 'thinking' preamble. Start with `{` and end with `}`."
)


# v15 Ticket 5 (2026-05-11): focused JSON repair prompt. Use AFTER an
# initial review attempt that returned unparseable output. The prompt is
# narrowly scoped to fix the JSON shape ONLY — repair must NOT add new
# findings, remove findings, or modify severities/files/quotes beyond
# what is strictly required to produce valid JSON. This is the entire
# point of separating repair from "retry the whole review": the latter
# drifts the verdict, the former just rescues the encoding.
_JSON_REPAIR_SYSTEM_PROMPT = (
    "You are repairing invalid JSON. You are NOT a reviewer; do not "
    "re-evaluate, add, or remove findings.\n"
    "Given a previous response that was meant to match a strict schema "
    "but failed to parse as JSON, output the SAME content as valid JSON.\n"
    "Rules:\n"
    "1. Preserve every finding from the previous output. Do not invent "
    "new findings or drop existing ones.\n"
    "2. Preserve severity, category, file, line numbers, description, "
    "evidence_quote, and suggested_fix exactly. Only fix encoding-level "
    "JSON errors (missing quotes, trailing commas, unescaped characters, "
    "unbalanced braces).\n"
    "3. Output ONLY the raw JSON object. No prose. No markdown fences. "
    "No leading newlines. Start with `{` and end with `}`."
)


def _build_json_repair_user_prompt(prior_raw: str) -> str:
    """The repair pass needs only the schema and the broken output. We do
    NOT re-send the diff / spec / files — that would tempt the model to
    re-review. The schema lives in the system prompt; the user message
    just hands over the broken text."""
    return (
        "Target schema:\n"
        '{"completeness_pct": int, "summary": str, "findings": ['
        '{"file": str, "line_start": int, "line_end": int, '
        '"severity": "high"|"medium"|"low", "category": str, '
        '"description": str, "evidence_quote": str, "suggested_fix": str}'
        "]}\n\n"
        "Previous output (invalid JSON):\n"
        "<<<\n" + prior_raw[:8000] + "\n>>>\n\n"
        "Repair it. JSON only."
    )


def _call_provider_with_system(
    provider_fn: Callable[..., str],
    *,
    user_prompt: str,
    system_prompt: str,
    settings: Settings,
    timeout: float,
) -> str:
    """Call a provider with a non-default system prompt.

    The existing _call_deepseek / _call_anthropic functions hard-code
    ``_SYSTEM_PROMPT``; the repair pass needs a different system prompt.
    Rather than expand every provider signature we monkey-patch the
    module-level _SYSTEM_PROMPT for the duration of the call. This is
    safe because the module is single-threaded per-task (no shared state
    across asyncio / threading) and the provider calls are synchronous.
    """
    import app.services.semantic_review as _sr_module

    saved = _sr_module._SYSTEM_PROMPT
    try:
        _sr_module._SYSTEM_PROMPT = system_prompt
        return provider_fn(user_prompt, settings=settings, timeout=timeout)
    finally:
        _sr_module._SYSTEM_PROMPT = saved


def _attempt_json_repair(
    provider_fn: Callable[..., str],
    prior_raw: str,
    *,
    settings: Settings,
    timeout: float,
) -> str:
    """Run a single focused JSON repair pass. Raises SemanticReviewError
    if the repaired text still doesn't parse as JSON. The repaired text
    is returned for downstream parsing — the caller is responsible for
    running it through ``parse_review_output`` and the grounding checks."""
    user_prompt = _build_json_repair_user_prompt(prior_raw)
    text = _call_provider_with_system(
        provider_fn,
        user_prompt=user_prompt,
        system_prompt=_JSON_REPAIR_SYSTEM_PROMPT,
        settings=settings,
        timeout=timeout,
    )
    if not (text or "").strip():
        raise SemanticReviewError(
            "JSON repair pass returned empty response"
        )
    candidate = _extract_json_object(text)
    try:
        json.loads(candidate)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SemanticReviewError(
            f"JSON repair output still unparseable: {exc}"
        ) from exc
    return text


def _call_anthropic(prompt: str, *, settings: Settings, timeout: float) -> str:
    if not settings.anthropic_api_key:
        raise SemanticReviewError("OPS_AGENT_ANTHROPIC_API_KEY is not configured.")
    body = {
        "model": settings.anthropic_model,
        "max_tokens": 4096,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = httpx.post(
        f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
        json=body,
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return _extract_text_with_thinking_fallback(data.get("content", []))


def _call_deepseek(prompt: str, *, settings: Settings, timeout: float) -> str:
    if not getattr(settings, "deepseek_api_key", None):
        raise SemanticReviewError("OPS_AGENT_DEEPSEEK_API_KEY is not configured.")
    base = getattr(settings, "deepseek_base_url",
                   "https://api.deepseek.com/anthropic")
    body = {
        "model": getattr(settings, "deepseek_model", "deepseek-v4-pro"),
        "max_tokens": 4096,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = cached_http_post(
        url=f"{base.rstrip('/')}/v1/messages",
        json=body,
        headers={
            "x-api-key": settings.deepseek_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=timeout,
        provider_hint="semantic_review.deepseek",
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        from app.services.llm_telemetry import log_llm_cache_hit
        log_llm_cache_hit(
            provider="deepseek",
            model=str(body.get("model", "")),
            purpose="semantic_review",
            usage=data.get("usage", {}) or {},
        )
    except Exception:  # noqa: BLE001 — instrumentation must never break flow
        pass
    # L2: thinking-block fallback for DeepSeek. When DeepSeek returns
    # ONLY a thinking block (no text), the helper returns the thinking
    # content; _extract_json_object can still pull the JSON out.
    return _extract_text_with_thinking_fallback(data.get("content", []))


@dataclass(frozen=True)
class _ReviewCallOutcome:
    """Internal carrier for the two-stage review+repair flow.

    Either ``text`` is set (raw provider output parseable as JSON) or
    ``unavailable`` is populated with the structured diagnostic that
    becomes a SemanticReviewReport.unavailable factory call.
    """

    text: str = ""
    review_attempts: int = 1
    repair_attempted: bool = False
    raw_preview: str = ""
    unavailable_reason: str = ""
    underlying: str = ""

    @property
    def succeeded(self) -> bool:
        return bool(self.text)


def _call_with_repair(
    provider_fn: Callable[..., str],
    prompt: str,
    *,
    settings: Settings,
    timeout: float,
    max_review_attempts: int = 1,
    max_repair_attempts: int = 1,
    raw_preview_chars: int = 500,
) -> _ReviewCallOutcome:
    """v15 Ticket 5 (2026-05-11): replaces the old retry-the-whole-review
    loop with an explicit two-stage flow:

      1. Up to ``max_review_attempts`` REVIEW attempts (default 1). The
         provider sees the full review prompt. Subsequent attempts add
         the "JSON ONLY" suffix the legacy code used. Parse success →
         done.
      2. If parse still fails, up to ``max_repair_attempts`` JSON
         REPAIR attempts (default 1). The provider receives only the
         broken prior output and the schema; it is explicitly told NOT
         to add/remove/re-evaluate findings. This rescues the encoding
         without drifting the verdict.
      3. If repair also fails, returns an ``_ReviewCallOutcome`` with
         ``unavailable_reason`` set — callers convert this into
         ``SemanticReviewReport.unavailable`` so the gate's status is
         honest and visible downstream.

    Provider errors (network / auth / timeout) still raise
    SemanticReviewError; only JSON-parse failures degrade to the
    unavailable outcome.
    """
    last_raw = ""
    last_err = ""
    attempts_made = 0

    for attempt in range(1, max(1, int(max_review_attempts)) + 1):
        attempts_made = attempt
        attempt_prompt = prompt if attempt == 1 else (prompt + _RETRY_SUFFIX)
        try:
            text = provider_fn(attempt_prompt, settings=settings, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — provider error escalates
            logger.warning(
                "semantic_review provider call failed (attempt %d/%d): %s",
                attempt, max_review_attempts, exc,
            )
            # Infrastructure error: re-raise so the orchestrator can
            # distinguish "provider down" from "reviewer output bad".
            raise SemanticReviewError(str(exc)) from exc
        if not (text or "").strip():
            last_raw = ""
            last_err = "empty response"
            logger.warning(
                "semantic_review empty response (attempt %d/%d)",
                attempt, max_review_attempts,
            )
            continue
        last_raw = text
        candidate = _extract_json_object(text)
        try:
            json.loads(candidate)
            return _ReviewCallOutcome(
                text=text,
                review_attempts=attempt,
                repair_attempted=False,
                raw_preview=text[:raw_preview_chars],
            )
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = f"unparseable JSON: {exc}"
            logger.warning(
                "semantic_review unparseable JSON (attempt %d/%d): %s",
                attempt, max_review_attempts, exc,
            )
            continue

    # Stage 2: focused JSON repair pass.
    repair_attempted = False
    for repair_attempt in range(1, max(0, int(max_repair_attempts)) + 1):
        repair_attempted = True
        try:
            repaired = _attempt_json_repair(
                provider_fn, last_raw,
                settings=settings, timeout=timeout,
            )
        except SemanticReviewError as exc:
            last_err = f"repair attempt {repair_attempt} failed: {exc}"
            logger.warning(
                "semantic_review JSON repair failed (attempt %d/%d): %s",
                repair_attempt, max_repair_attempts, exc,
            )
            continue
        except Exception as exc:  # noqa: BLE001 — provider error escalates
            raise SemanticReviewError(str(exc)) from exc
        return _ReviewCallOutcome(
            text=repaired,
            review_attempts=attempts_made,
            repair_attempted=True,
            raw_preview=repaired[:raw_preview_chars],
        )

    # Stage 3: gate becomes unavailable.
    return _ReviewCallOutcome(
        text="",
        review_attempts=attempts_made,
        repair_attempted=repair_attempted,
        raw_preview=(last_raw or "")[:raw_preview_chars],
        unavailable_reason="invalid_json",
        underlying=last_err,
    )


_PROVIDERS: dict[str, Callable[..., str]] = {
    "anthropic": _call_anthropic,
    "deepseek": _call_deepseek,
}


# ---- Top-level evaluator ---------------------------------------------------

def evaluate_semantic_review(
    *,
    spec_text: str,
    diff: str,
    file_contents: dict[str, str] | None,
    settings: Settings,
    pass_threshold: int = 80,
    provider: str = "anthropic",
    timeout_seconds: float = 60.0,
    llm_caller: Callable[[str], str] | None = None,
) -> SemanticReviewReport:
    """Run the LLM reviewer and return a validated, grounded report.

    ``llm_caller`` lets tests inject a stub. In production the caller
    is selected by ``provider`` and dispatched via ``_PROVIDERS``.
    """
    if not (diff or "").strip():
        # No diff to review — passing trivially.
        return SemanticReviewReport(
            passed=True,
            completeness_pct=100,
            summary="No diff to review (empty patch).",
            findings=(),
            pass_threshold=pass_threshold,
            total_findings_raw=0,
            findings_dropped_no_evidence=0,
            provider_name=provider,
        )

    prompt = build_user_prompt(
        spec_text=spec_text,
        diff=diff,
        file_contents=file_contents,
    )
    raw_preview_chars = int(
        getattr(settings, "semantic_review_raw_preview_chars", 500) or 500
    )
    review_attempts_used = 1
    repair_attempted = False
    if llm_caller is not None:
        # Test path: stub caller produces raw text directly. Honour the
        # same parse-then-repair contract for parity with production.
        raw_text = llm_caller(prompt)
        try:
            json.loads(_extract_json_object(raw_text))
        except (json.JSONDecodeError, ValueError) as exc:
            return SemanticReviewReport.unavailable(
                reason="invalid_json",
                provider=provider,
                review_attempts=1,
                repair_attempted=False,
                raw_preview=(raw_text or "")[:raw_preview_chars],
                pass_threshold=pass_threshold,
            )
    else:
        provider_fn = _PROVIDERS.get(provider)
        if provider_fn is None:
            raise SemanticReviewError(
                f"Unknown semantic_review provider: {provider!r}"
            )
        outcome = _call_with_repair(
            provider_fn,
            prompt,
            settings=settings,
            timeout=timeout_seconds,
            max_review_attempts=int(
                getattr(
                    settings, "semantic_review_max_review_attempts", 1,
                ) or 1
            ),
            max_repair_attempts=int(
                getattr(
                    settings, "semantic_review_max_repair_attempts", 1,
                ) or 1
            ),
            raw_preview_chars=raw_preview_chars,
        )
        review_attempts_used = outcome.review_attempts
        repair_attempted = outcome.repair_attempted
        if not outcome.succeeded:
            return SemanticReviewReport.unavailable(
                reason=outcome.unavailable_reason or "invalid_json",
                provider=provider,
                review_attempts=outcome.review_attempts,
                repair_attempted=outcome.repair_attempted,
                raw_preview=outcome.raw_preview,
                pass_threshold=pass_threshold,
            )
        raw_text = outcome.text

    raw = parse_review_output(raw_text)
    diff_files = _files_in_diff(diff)
    diff_norm = _normalize_for_match(diff)
    grounded: list[SemanticReviewFinding] = []
    dropped = 0
    for f in raw.findings:
        if not _is_finding_grounded(
            f, diff_normalized=diff_norm, diff_files=diff_files
        ):
            dropped += 1
            logger.debug("dropped ungrounded finding: %s", f.description)
            continue
        grounded.append(SemanticReviewFinding(
            file=f.file,
            line_start=f.line_start,
            line_end=f.line_end,
            severity=f.severity,
            category=f.category,
            description=f.description,
            evidence_quote=f.evidence_quote,
            suggested_fix=f.suggested_fix,
        ))

    high_count = sum(1 for f in grounded if f.severity == "high")
    completeness = max(0, min(100, int(raw.completeness_pct)))
    passed = completeness >= pass_threshold and high_count == 0
    return SemanticReviewReport(
        passed=passed,
        completeness_pct=completeness,
        summary=(raw.summary or "").strip()[:600],
        findings=tuple(grounded),
        pass_threshold=pass_threshold,
        total_findings_raw=len(raw.findings),
        findings_dropped_no_evidence=dropped,
        provider_name=provider,
        status="passed" if passed else "failed",
        review_attempts=review_attempts_used,
        repair_attempted=repair_attempted,
    )
