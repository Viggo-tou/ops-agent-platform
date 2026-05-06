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


logger = logging.getLogger("semantic_review")


_SEVERITIES: tuple[str, ...] = ("high", "medium", "low")


class SemanticReviewError(Exception):
    """Raised when the LLM call or response parsing fails."""


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

    def high_severity_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "high")

    def to_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "completeness_pct": self.completeness_pct,
            "summary": self.summary,
            "pass_threshold": self.pass_threshold,
            "total_findings_raw": self.total_findings_raw,
            "findings_dropped_no_evidence": self.findings_dropped_no_evidence,
            "high_severity_count": self.high_severity_count(),
            "provider_name": self.provider_name,
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
    "7. Output ONLY the JSON. No prose, no markdown fences.\n\n"
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
    resp = httpx.post(
        f"{base.rstrip('/')}/v1/messages",
        json=body,
        headers={
            "x-api-key": settings.deepseek_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=timeout,
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


def _call_with_retry(
    provider_fn: Callable[..., str],
    prompt: str,
    *,
    settings: Settings,
    timeout: float,
    max_attempts: int = 2,
) -> str:
    """L2: wrap provider_fn with one auto-retry on empty / unparsable
    response. The retry adds an explicit "JSON ONLY" suffix to the
    prompt — empirically gets DeepSeek out of "thinking-only" responses
    after a single retry.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_prompt = prompt if attempt == 1 else (prompt + _RETRY_SUFFIX)
        try:
            text = provider_fn(attempt_prompt, settings=settings, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(
                "semantic_review provider call failed (attempt %d/%d): %s",
                attempt, max_attempts, exc,
            )
            continue
        # Empty text -> retry
        if not (text or "").strip():
            last_err = SemanticReviewError("LLM returned empty response")
            logger.warning(
                "semantic_review empty response (attempt %d/%d)",
                attempt, max_attempts,
            )
            continue
        # Try a quick parse-check; if it fails, retry once.
        candidate = _extract_json_object(text)
        try:
            json.loads(candidate)
            return text  # parsed cleanly; return original (caller re-extracts)
        except (json.JSONDecodeError, ValueError) as exc:
            last_err = SemanticReviewError(
                f"LLM response not parseable as JSON (attempt {attempt}): {exc}"
            )
            logger.warning(
                "semantic_review unparsable JSON (attempt %d/%d): %s",
                attempt, max_attempts, exc,
            )
            continue
    # Exhausted
    if last_err is None:
        last_err = SemanticReviewError("LLM call failed without specific error")
    raise last_err if isinstance(last_err, SemanticReviewError) else \
        SemanticReviewError(str(last_err))


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
    if llm_caller is not None:
        raw_text = llm_caller(prompt)
    else:
        provider_fn = _PROVIDERS.get(provider)
        if provider_fn is None:
            raise SemanticReviewError(
                f"Unknown semantic_review provider: {provider!r}"
            )
        # L2: wrap with auto-retry that injects an explicit "JSON only"
        # suffix on retry, to get DeepSeek out of thinking-only loops.
        raw_text = _call_with_retry(
            provider_fn,
            prompt,
            settings=settings,
            timeout=timeout_seconds,
            max_attempts=2,
        )

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
    )
