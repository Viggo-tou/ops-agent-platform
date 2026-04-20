"""LLM-driven semantic completeness review for generated diffs.

Positioned right after compile_gate in the develop pipeline.
Asks an LLM to read the task description and the generated diff
side-by-side and produce a coverage verdict:

- **pass**: every requirement is addressed, edge cases handled.
- **iterate**: specific gaps identified — feed back to codegen.

Unlike spec_conformance (keyword counting) or goal_attestation
(anchor delta), this gate understands *intent*: "did you add
try/catch for JSON.parse?", "is the filter case-insensitive?",
"does handleEdit normalize the role before saving?".

Design constraints:
- Max 2 iteration rounds (avoid cost explosion).
- Two backends: Claude Code CLI (default, no API key needed) and
  Anthropic API (when api_key is supplied).
- Structured JSON output for machine-parseable gap list.
- Falls back gracefully: if the LLM call fails, the gate passes
  with a warning (don't block the pipeline on infra errors).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Maximum file content to include in the review prompt (chars per file)
_MAX_FILE_CHARS = 12_000
# Maximum diff chars to include
_MAX_DIFF_CHARS = 20_000


@dataclass(frozen=True)
class ReviewGap:
    """A single gap found during semantic review."""
    category: str          # "missing_feature" | "edge_case" | "defensive_coding" | "logic_error"
    description: str       # Human-readable description
    file_hint: str = ""    # Which file needs the fix (optional)
    severity: str = "required"  # "required" | "suggested"


@dataclass(frozen=True)
class SemanticReviewReport:
    verdict: str           # "pass" | "iterate"
    covered: list[str]     # Requirements confirmed covered
    gaps: list[ReviewGap]  # Gaps that need addressing
    summary: str           # One-paragraph summary
    raw_response: str = "" # Raw LLM response for debugging

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "covered": self.covered,
            "gaps": [
                {
                    "category": g.category,
                    "description": g.description,
                    "file_hint": g.file_hint,
                    "severity": g.severity,
                }
                for g in self.gaps
            ],
            "summary": self.summary,
        }

    def gap_feedback_lines(self) -> list[str]:
        """Format gaps as feedback lines for the codegen retry prompt."""
        return [
            f"[{g.severity.upper()}] {g.description}"
            + (f" (in {g.file_hint})" if g.file_hint else "")
            for g in self.gaps
            if g.severity == "required"
        ]


_REVIEW_SYSTEM_PROMPT = """\
You are a senior code reviewer. Your job is to verify that a generated \
code diff FULLY and CORRECTLY addresses every requirement in the task \
description.

You must check:
1. Every explicit requirement — is it implemented?
2. Edge cases — are inputs validated, nulls handled, case sensitivity addressed?
3. Defensive coding — try/catch for parsing, fallbacks for missing data?
4. Consistency — if a pattern is applied in one place, is it applied everywhere \
   it should be (e.g., role normalization on read AND on edit AND on filter)?
5. Security — no XSS (dangerouslySetInnerHTML), no injection, no exposed secrets?

Respond with ONLY a JSON object (no markdown fences, no prose before/after):
{
  "verdict": "pass" or "iterate",
  "covered": ["requirement 1 that IS addressed", ...],
  "gaps": [
    {
      "category": "missing_feature|edge_case|defensive_coding|logic_error|security",
      "description": "What is missing or wrong",
      "file_hint": "filename.js (if known)",
      "severity": "required|suggested"
    }
  ],
  "summary": "One paragraph overall assessment"
}

Rules:
- verdict="pass" ONLY if there are zero "required" severity gaps.
- verdict="iterate" if ANY required gap exists.
- "suggested" gaps are nice-to-haves; they don't force iterate.
- Be specific in descriptions — say "handleEdit() does not normalize \
  role before saving" not "role handling incomplete".
- Do not invent requirements not in the task description.
- If the task only asks to rename roles, don't demand a full auth rewrite.
"""


def _build_review_prompt(
    *,
    task_description: str,
    normalized_request: str | None,
    diff: str,
    source_files: dict[str, str] | None,
    previous_gaps: list[str] | None,
) -> str:
    """Build the user prompt for the semantic review LLM call."""
    parts: list[str] = []

    parts.append("## Task Description\n")
    parts.append(task_description.strip())
    if normalized_request:
        parts.append(f"\n\n## Normalized Request (semantic translation)\n")
        parts.append(normalized_request.strip())

    if previous_gaps:
        parts.append("\n\n## Previous Review Feedback (these gaps were identified earlier)\n")
        for g in previous_gaps:
            parts.append(f"- {g}")
        parts.append("\nVerify whether these gaps have been addressed in the current diff.")

    # Truncate diff if too long
    review_diff = diff
    if len(review_diff) > _MAX_DIFF_CHARS:
        review_diff = review_diff[:_MAX_DIFF_CHARS] + "\n... (diff truncated)"

    parts.append(f"\n\n## Generated Diff ({len(diff)} chars)\n```diff\n{review_diff}\n```")

    if source_files:
        parts.append("\n\n## Original Source Files (before patch)\n")
        for filepath, content in source_files.items():
            truncated = content[:_MAX_FILE_CHARS]
            if len(content) > _MAX_FILE_CHARS:
                truncated += "\n... (truncated)"
            parts.append(f"### {filepath}\n```\n{truncated}\n```\n")

    parts.append(
        "\n\nReview the diff against the task description. "
        "Output ONLY the JSON verdict object."
    )

    return "\n".join(parts)


def _parse_review_response(raw: str) -> dict:
    """Extract JSON from the LLM response, handling markdown fences."""
    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Remove opening fence (possibly with language tag)
        cleaned = re.sub(r"^```\w*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try to find JSON object in the response
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {}


def run_semantic_review(
    *,
    task_description: str,
    normalized_request: str | None,
    diff: str,
    source_files: dict[str, str] | None = None,
    previous_gaps: list[str] | None = None,
    api_key: str,
    base_url: str = "https://api.anthropic.com",
    model: str = "claude-sonnet-4-20250514",
    timeout_seconds: float = 60.0,
) -> SemanticReviewReport:
    """Call the LLM to perform semantic review of a generated diff.

    Returns a SemanticReviewReport with verdict, covered items, and gaps.
    Raises on network/API errors — caller should catch and handle gracefully.
    """
    user_prompt = _build_review_prompt(
        task_description=task_description,
        normalized_request=normalized_request,
        diff=diff,
        source_files=source_files,
        previous_gaps=previous_gaps,
    )

    headers = {
        "x-api-key": api_key,
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    payload = {
        "model": model,
        "max_tokens": 2048,
        "system": _REVIEW_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    url = f"{base_url.rstrip('/')}/v1/messages"

    with httpx.Client(timeout=timeout_seconds) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()

    data = resp.json()
    raw_text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            raw_text += block.get("text", "")

    parsed = _parse_review_response(raw_text)
    if not parsed:
        logger.warning("semantic_review: could not parse LLM response, defaulting to pass")
        return SemanticReviewReport(
            verdict="pass",
            covered=[],
            gaps=[],
            summary="Review response could not be parsed; defaulting to pass.",
            raw_response=raw_text[:500],
        )

    verdict = parsed.get("verdict", "pass")
    covered = parsed.get("covered", [])
    if not isinstance(covered, list):
        covered = []

    raw_gaps = parsed.get("gaps", [])
    if not isinstance(raw_gaps, list):
        raw_gaps = []

    gaps: list[ReviewGap] = []
    for g in raw_gaps:
        if isinstance(g, dict) and g.get("description"):
            gaps.append(ReviewGap(
                category=str(g.get("category", "missing_feature")),
                description=str(g["description"]),
                file_hint=str(g.get("file_hint", "")),
                severity=str(g.get("severity", "required")),
            ))

    # Enforce: if any required gap exists, verdict must be iterate
    has_required = any(g.severity == "required" for g in gaps)
    if has_required and verdict == "pass":
        verdict = "iterate"
    if not has_required and verdict == "iterate":
        verdict = "pass"

    summary = str(parsed.get("summary", ""))

    return SemanticReviewReport(
        verdict=verdict,
        covered=covered,
        gaps=gaps,
        summary=summary,
        raw_response=raw_text[:1000],
    )


# ---------------------------------------------------------------------------
# CLI backend — uses Claude Code CLI (`claude -p`) instead of Anthropic API
# ---------------------------------------------------------------------------

def run_semantic_review_cli(
    *,
    task_description: str,
    normalized_request: str | None,
    diff: str,
    source_files: dict[str, str] | None = None,
    previous_gaps: list[str] | None = None,
    claude_command: str = "npx",
    claude_args: str = "--yes @anthropic-ai/claude-code",
    timeout_seconds: float = 120.0,
) -> SemanticReviewReport:
    """Run semantic review via Claude Code CLI.

    Same contract as ``run_semantic_review`` but invokes ``claude -p``
    instead of the Anthropic Messages API.  Requires no API key — uses
    the CLI's own OAuth session.
    """
    user_prompt = _build_review_prompt(
        task_description=task_description,
        normalized_request=normalized_request,
        diff=diff,
        source_files=source_files,
        previous_gaps=previous_gaps,
    )

    full_prompt = (
        _REVIEW_SYSTEM_PROMPT
        + "\n\n---\n\n"
        + user_prompt
    )

    claude_cmd = shutil.which(claude_command)
    if not claude_cmd:
        raise RuntimeError(f"Claude Code CLI not found: {claude_command}")

    # Write prompt to temp file (Windows pipe buffer workaround)
    prompt_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    prompt_file.write(full_prompt)
    prompt_file.close()

    workdir = tempfile.mkdtemp(prefix="ops_semantic_review_")

    try:
        # Build CLI args
        cli_args = claude_args.split()
        if "-p" not in cli_args and "--print" not in cli_args:
            cli_args.append("--print")
        cmd = [claude_cmd, *cli_args, "-"]

        env = {**os.environ}
        env.pop("ANTHROPIC_API_KEY", None)

        # Windows: Claude Code CLI needs git-bash path
        if os.name == "nt" and "CLAUDE_CODE_GIT_BASH_PATH" not in env:
            for candidate in [
                "D:\\Git\\bin\\bash.exe",
                "C:\\Program Files\\Git\\bin\\bash.exe",
                "C:\\Program Files (x86)\\Git\\bin\\bash.exe",
            ]:
                if os.path.isfile(candidate):
                    env["CLAUDE_CODE_GIT_BASH_PATH"] = candidate
                    break

        logger.info("semantic_review CLI cmd: %s", cmd)

        with open(prompt_file.name, "r", encoding="utf-8") as stdin_f:
            proc = subprocess.run(
                cmd,
                stdin=stdin_f,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(timeout_seconds),
                cwd=workdir,
                env=env,
            )

        raw_text = proc.stdout or ""
        if proc.returncode != 0:
            logger.warning(
                "semantic_review CLI rc=%d stderr=%s",
                proc.returncode, (proc.stderr or "")[:500],
            )
            if not raw_text.strip():
                raise RuntimeError(
                    f"Claude Code CLI failed (rc={proc.returncode}): "
                    + (proc.stderr or "")[:300]
                )

    finally:
        try:
            os.unlink(prompt_file.name)
        except OSError:
            pass
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass

    return _parse_cli_output_to_report(raw_text)


def _parse_cli_output_to_report(raw_text: str) -> SemanticReviewReport:
    """Parse Claude Code CLI output into a SemanticReviewReport."""
    parsed = _parse_review_response(raw_text)
    if not parsed:
        logger.warning(
            "semantic_review CLI: could not parse response, defaulting to pass"
        )
        return SemanticReviewReport(
            verdict="pass",
            covered=[],
            gaps=[],
            summary="CLI review response could not be parsed; defaulting to pass.",
            raw_response=raw_text[:500],
        )

    verdict = parsed.get("verdict", "pass")
    covered = parsed.get("covered", [])
    if not isinstance(covered, list):
        covered = []

    raw_gaps = parsed.get("gaps", [])
    if not isinstance(raw_gaps, list):
        raw_gaps = []

    gaps: list[ReviewGap] = []
    for g in raw_gaps:
        if isinstance(g, dict) and g.get("description"):
            gaps.append(ReviewGap(
                category=str(g.get("category", "missing_feature")),
                description=str(g["description"]),
                file_hint=str(g.get("file_hint", "")),
                severity=str(g.get("severity", "required")),
            ))

    has_required = any(g.severity == "required" for g in gaps)
    if has_required and verdict == "pass":
        verdict = "iterate"
    if not has_required and verdict == "iterate":
        verdict = "pass"

    summary = str(parsed.get("summary", ""))

    return SemanticReviewReport(
        verdict=verdict,
        covered=covered,
        gaps=gaps,
        summary=summary,
        raw_response=raw_text[:1000],
    )
