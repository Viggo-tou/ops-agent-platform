"""Runtime validation gate - semantic checks on generated diffs.

Catches bugs that pass syntax/compile gates but would break at runtime:
- Case-sensitive string comparisons against data sources with different casing
- Hardcoded string literals that should use case-insensitive comparison
- Role/permission checks that don't account for data format variations
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class ValidationFinding:
    file: str
    line: int | None
    severity: str  # "warn" or "block"
    rule: str
    message: str


@dataclass
class ValidationReport:
    passed: bool
    findings: list[ValidationFinding] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return f"Runtime validation passed ({len(self.findings)} warning(s))"
        blocks = [f for f in self.findings if f.severity == "block"]
        return f"Runtime validation failed: {len(blocks)} blocking issue(s)"

    def to_payload(self) -> dict:
        return {
            "passed": self.passed,
            "findings": [
                {
                    "file": f.file,
                    "line": f.line,
                    "severity": f.severity,
                    "rule": f.rule,
                    "message": f.message,
                }
                for f in self.findings
            ],
        }


_WARN_ESCALATION_THRESHOLD = 3


def validate_diff_semantics(
    diff: str,
    context_files: dict[str, str],
    request_text: str = "",
) -> ValidationReport:
    """Run semantic validation rules on a diff.

    Args:
        diff: The unified diff text.
        context_files: Original file contents before the patch.
        request_text: The user's task description for intent matching.

    Returns:
        ValidationReport with findings.
    """
    _ = request_text
    findings: list[ValidationFinding] = []

    findings.extend(_check_case_sensitive_comparisons(diff, context_files))
    findings.extend(_check_replacement_completeness(diff, context_files))

    # Escalate to block when too many warnings of the same rule accumulate
    rule_counts: dict[str, int] = {}
    for f in findings:
        rule_counts[f.rule] = rule_counts.get(f.rule, 0) + 1
    for f in findings:
        if f.severity == "warn" and rule_counts.get(f.rule, 0) >= _WARN_ESCALATION_THRESHOLD:
            f.severity = "block"

    has_block = any(f.severity == "block" for f in findings)
    return ValidationReport(passed=not has_block, findings=findings)


def _check_case_sensitive_comparisons(
    diff: str,
    context_files: dict[str, str],
) -> list[ValidationFinding]:
    """Detect added strict comparisons where the original used different casing."""
    findings: list[ValidationFinding] = []
    seen: set[tuple[str, str, str]] = set()

    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            added = line[1:]
            matches = re.findall(r'===\s*["\']([^"\']+)["\']', added)
            for match in matches:
                original = context_files.get(current_file, "")
                if not original:
                    continue
                pattern = re.compile(re.escape(match), re.IGNORECASE)
                orig_variants = set(pattern.findall(original))
                for om in orig_variants:
                    if om != match and om.lower() == match.lower():
                        key = (current_file, match, om)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append(
                            ValidationFinding(
                                file=current_file,
                                line=None,
                                severity="warn",
                                rule="case_sensitive_comparison",
                                message=(
                                    f'Strict comparison === "{match}" but original '
                                    f'file uses "{om}". Consider using '
                                    ".toLowerCase() or .toUpperCase() for "
                                    "case-insensitive comparison."
                                ),
                            )
                        )
    return findings


def _check_replacement_completeness(
    diff: str,
    context_files: dict[str, str],
) -> list[ValidationFinding]:
    """Check that string replacements are complete across all context files.

    Heuristic: strings that a CLI agent wholesale-deletes when rewriting a
    single file are NOT refactor targets — the agent just regenerated the
    file and dropped some lines. We detect wholesale-rewrite hunks
    (``@@ -1,N +0,0 @@`` or very large ``-`` blocks with minimal ``+``
    counterpart) and exclude their removed strings from the "replaced" set,
    otherwise every common import like "react" triggers 90+ false positives.
    """
    findings: list[ValidationFinding] = []

    # Track which strings appear on - lines ONLY in targeted replacement
    # hunks, not wholesale-rewrite hunks. A targeted hunk has both - and +
    # lines with roughly balanced substring counts; wholesale rewrites tend
    # to be one gigantic - block + one gigantic + block (or all-delete).
    removed_strings: set[str] = set()
    added_strings: set[str] = set()

    current_hunk_minus: list[str] = []
    current_hunk_plus: list[str] = []

    def _flush_hunk() -> None:
        # Only count removals from "small" hunks; treat hunks with >40 minus
        # lines as a wholesale rewrite and skip their removed-string contribution.
        is_wholesale = len(current_hunk_minus) > 40
        if not is_wholesale:
            for line in current_hunk_minus:
                for m in re.findall(r'["\']([^"\']{3,})["\']', line):
                    removed_strings.add(m)
        for line in current_hunk_plus:
            for m in re.findall(r'["\']([^"\']{3,})["\']', line):
                added_strings.add(m)

    for line in diff.splitlines():
        if line.startswith("@@"):
            _flush_hunk()
            current_hunk_minus = []
            current_hunk_plus = []
        elif line.startswith("-") and not line.startswith("---"):
            current_hunk_minus.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            current_hunk_plus.append(line[1:])
    _flush_hunk()

    replaced = removed_strings - added_strings

    diff_files = set()
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            diff_files.add(line[6:])

    for old_str in replaced:
        for path, content in context_files.items():
            if path in diff_files:
                continue
            if old_str in content:
                findings.append(
                    ValidationFinding(
                        file=path,
                        line=None,
                        severity="warn",
                        rule="incomplete_replacement",
                        message=(
                            f'String "{old_str}" was replaced in other files '
                            f"but still appears in {path} (not modified by this diff)."
                        ),
                    )
                )
    return findings


@dataclass(frozen=True)
class RuntimeFinding:
    rule: str
    severity: str
    message: str
    evidence: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeReport:
    verdict: str
    findings: tuple[RuntimeFinding, ...]

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "findings": [
                {"rule": f.rule, "severity": f.severity, "message": f.message, "evidence": f.evidence}
                for f in self.findings
            ],
        }


def check_runtime_validity(
    *,
    changed_files: Iterable[str],
    sandbox_dir: Path | None = None,
) -> RuntimeReport:
    """Run lightweight runtime checks on changed files."""
    findings: list[RuntimeFinding] = []

    py_files = [f for f in changed_files if f.endswith(".py")]

    for py_file in py_files:
        if sandbox_dir is not None:
            full_path = sandbox_dir / py_file
            if full_path.exists():
                result = _try_python_compile(full_path)
                if result:
                    findings.append(RuntimeFinding(
                        rule="runtime_py_import",
                        severity="warn",
                        message=f"Python file {py_file} has import/compile issue: {result}",
                        evidence={"file": py_file, "error": result},
                    ))

    health = _check_health_endpoint()
    if health is not None:
        findings.append(health)

    verdict = "block" if any(f.severity == "block" for f in findings) else "pass"
    return RuntimeReport(verdict=verdict, findings=tuple(findings))


def _try_python_compile(path: Path) -> str | None:
    """Try to compile (not execute) a Python file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
        compile(source, str(path), "exec")
        return None
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"
    except Exception as e:
        return str(e)[:200]


def _check_health_endpoint() -> RuntimeFinding | None:
    """Try hitting the local backend health endpoint.

    Non-blocking: returns None if backend is not running.
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request("http://127.0.0.1:8000/health", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return RuntimeFinding(
                    rule="runtime_health",
                    severity="warn",
                    message=f"Backend /health returned status {resp.status}",
                    evidence={"status": resp.status},
                )
        return None
    except (urllib.error.URLError, OSError):
        return None


def build_repair_prompt(findings: list[ValidationFinding]) -> str:
    """Build a codegen repair prompt from runtime validation findings."""
    blocks = [f for f in findings if f.severity == "block"]
    if not blocks:
        return ""

    files = sorted({f.file for f in blocks})
    issues_by_file: dict[str, list[str]] = {}
    for f in blocks:
        issues_by_file.setdefault(f.file, []).append(f"  - [{f.rule}] {f.message}")

    lines = [
        "RUNTIME VALIDATION REPAIR — fix semantic issues detected by automated analysis.\n",
        "The following files have issues that would break at runtime:\n",
    ]
    for path in files:
        lines.append(f"\n### {path}")
        for issue in issues_by_file[path]:
            lines.append(issue)

    lines.append(
        "\n\nRULES:\n"
        "- Fix ONLY the issues listed above.\n"
        "- Use case-insensitive comparisons (.toLowerCase() / .lower()) "
        "when the data source may use different casing.\n"
        "- Ensure string replacements are complete across all affected files.\n"
        "- Do NOT add new features or change unrelated logic.\n"
        "- Output ONLY valid unified diff hunks.\n"
    )
    return "\n".join(lines)


def check_browser_smoke() -> RuntimeReport:
    """Placeholder for browser-based smoke tests.

    Will be implemented when playwright MCP server is available in the
    pipeline (not just in the IDE). For now, returns pass with a note.
    """
    return RuntimeReport(
        verdict="pass",
        findings=(
            RuntimeFinding(
                rule="browser_smoke_skipped",
                severity="warn",
                message="Browser smoke test not available in pipeline; skipped.",
                evidence={"reason": "playwright_not_in_pipeline"},
            ),
        ),
    )
