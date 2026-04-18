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

    has_block = any(f.severity == "block" for f in findings)
    return ValidationReport(passed=not has_block, findings=findings)


def _check_case_sensitive_comparisons(
    diff: str,
    context_files: dict[str, str],
) -> list[ValidationFinding]:
    """Detect added strict comparisons where the original used different casing."""
    findings: list[ValidationFinding] = []

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
                orig_matches = pattern.findall(original)
                for om in orig_matches:
                    if om != match and om.lower() == match.lower():
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
    """Check that string replacements are complete across all context files."""
    findings: list[ValidationFinding] = []

    removed_strings: set[str] = set()
    added_strings: set[str] = set()

    for line in diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            for m in re.findall(r'["\']([^"\']{3,})["\']', line[1:]):
                removed_strings.add(m)
        elif line.startswith("+") and not line.startswith("+++"):
            for m in re.findall(r'["\']([^"\']{3,})["\']', line[1:]):
                added_strings.add(m)

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
