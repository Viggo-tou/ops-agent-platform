"""Post-codegen symbol verifier.

Catches the "Receiver.member doesn't exist in this repo" class of
hallucinations BEFORE they hit compile_gate. The verifier reads the
final unified diff, extracts ``Receiver.member`` references in added
lines, and grep-validates each one against the actual repository
source. Hallucinated references produce structured findings that the
repair loop can feed back to the LLM with concrete actionable signal
("`viewModel.jobAddress` doesn't exist; JobPostingViewModel has:
locationAddress, latitude, longitude — pick one or add the field").

Provider-agnostic: operates on the diff text only.

Conservative by design: a finding is only emitted when (a) the
receiver clearly resolves to an existing class/object/interface in the
repo AND (b) no member with that name appears anywhere near it. This
keeps false positives low — ambiguous cases defer to compile_gate.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# File extensions the verifier scans for member declarations. Limited
# to the source files we expect class/method declarations to live in.
_SOURCE_EXTENSIONS = {".kt", ".kts", ".java"}
# Skip generated / vendor / build directories anywhere in the path.
_SKIP_DIRS = {
    ".git", "node_modules", "build", ".gradle", ".idea", "dist",
    "__pycache__", ".venv", "venv", "target", "out", "generated",
}
# Hard cap on files scanned so the verifier stays cheap on large repos.
_MAX_SCAN_FILES = 1500
# How many characters of file content to load when verifying a single
# member reference. Receiver classes are usually well under 5KB.
_MAX_FILE_BYTES = 200_000

# Receivers that are noisy and never useful to verify (Kotlin builtins,
# Compose primitives, common stdlib types). Skip references to them.
_RECEIVER_BLOCKLIST = {
    "this", "super", "it",
    # Kotlin / Java stdlib
    "Math", "String", "Int", "Long", "Double", "Float", "Boolean",
    "List", "Map", "Set", "Array", "Pair", "Triple",
    "Result", "TODO",
    # Compose
    "Modifier", "Color", "FontWeight", "TextAlign", "Alignment",
    "Arrangement", "PaddingValues", "Brush", "Shape",
    # Coroutines
    "Dispatchers",
    # Common Android
    "Log", "R", "Bundle", "Intent",
}

# Members whose absence is meaningless (e.g. property delegate result
# shape, getter/setter). Skip these even if grep doesn't find them.
_MEMBER_BLOCKLIST = {
    "value", "values", "size", "length", "isEmpty", "isNotEmpty",
    "first", "last", "get", "set", "toString", "hashCode", "equals",
}

# Patterns to extract `Receiver.identifier` from a single source line.
# Matches: `Foo.bar`, `viewModel.locationAddress`, `obj.method(`
# Skips:   `0.5` (number), `"a.b"` (string literal)
# Receiver must start with a capital letter (class) OR match a known
# common variable receiver name (`viewModel`, `context`, `it`).
_REF_PATTERN = re.compile(
    r"\b(?P<receiver>[A-Za-z_][A-Za-z0-9_]*)"
    r"\.(?P<member>[a-zA-Z_][a-zA-Z0-9_]*)\b"
)
# A receiver "looks like a class" if PascalCase. We will try to verify
# class-receiver references; lowercase receivers (variables) are only
# verified when their declared type is a class we can find.
_PASCAL_CASE = re.compile(r"^[A-Z][A-Za-z0-9]*$")


@dataclass(frozen=True)
class HallucinatedReference:
    receiver: str
    member: str
    file: str
    line: str
    receiver_resolved_to: str | None  # Path of file declaring the receiver
    available_members_sample: list[str]  # First few real members of the receiver


@dataclass(frozen=True)
class VerificationReport:
    findings: list[HallucinatedReference] = field(default_factory=list)
    scanned_refs: int = 0
    skipped_refs_blocklist: int = 0
    skipped_refs_unverifiable: int = 0

    @property
    def has_hallucinations(self) -> bool:
        return bool(self.findings)

    def to_payload(self) -> dict[str, object]:
        return {
            "scanned_refs": self.scanned_refs,
            "hallucinations": [
                {
                    "receiver": f.receiver,
                    "member": f.member,
                    "file": f.file,
                    "line": f.line[:200],
                    "receiver_in": f.receiver_resolved_to,
                    "available_members_sample": f.available_members_sample[:8],
                }
                for f in self.findings
            ],
        }

    def repair_feedback(self) -> str:
        """Render findings as a feedback block the repair prompt can include."""
        if not self.findings:
            return ""
        lines = [
            "SYMBOL VERIFIER REJECTIONS — these references in your "
            "patch do NOT exist in the repository. Do not invent symbols. "
            "Either (a) use one of the existing members listed below, or "
            "(b) declare the new symbol in the receiver's source file "
            "yourself.",
        ]
        for f in self.findings:
            sample = ", ".join(f.available_members_sample[:8]) if f.available_members_sample else "(none discovered)"
            target = f.receiver_resolved_to or "(receiver class not located)"
            lines.append(
                f"  - `{f.receiver}.{f.member}` referenced in {f.file}: "
                f"`{f.receiver}` is declared in {target}, which has these "
                f"members: [{sample}]. `{f.member}` is not among them."
            )
        return "\n".join(lines)


def _added_lines_per_file(diff: str) -> dict[str, list[str]]:
    """Parse a unified diff and return {file_path: [added_line_text...]}."""
    added: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/", line)
            current = m.group(1).strip() if m else None
            continue
        if line.startswith("+++"):
            continue
        if line.startswith("---"):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+") and current:
            # Strip the leading +
            added.setdefault(current, []).append(line[1:])
    return added


def _walk_source_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for path in repo_root.rglob("*"):
        if len(out) >= _MAX_SCAN_FILES:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SOURCE_EXTENSIONS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return out


def _read_text(path: Path) -> str:
    try:
        data = path.read_bytes()[:_MAX_FILE_BYTES]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _find_receiver_declaration(
    receiver: str,
    source_files: list[Path],
    repo_root: Path,
) -> tuple[str | None, str]:
    """Locate the file that declares ``class/object/interface receiver``.

    Returns (relative_path, file_content) or (None, "").
    """
    decl_re = re.compile(
        r"\b(?:class|object|interface|data class|sealed class|abstract class|enum class)\s+"
        + re.escape(receiver)
        + r"\b"
    )
    for path in source_files:
        text = _read_text(path)
        if not text:
            continue
        if decl_re.search(text):
            try:
                rel = path.relative_to(repo_root).as_posix()
            except ValueError:
                rel = str(path)
            return rel, text
    return None, ""


def _extract_members_from_class_body(text: str) -> list[str]:
    """Extract declared member names (functions, properties) from a class body.

    Best-effort; doesn't aim to be a full Kotlin parser. Catches the
    common ``fun foo`` / ``val foo`` / ``var foo`` / ``private val foo``
    patterns plus ``foo by mutableStateOf`` Compose state delegates.
    """
    members: list[str] = []
    seen: set[str] = set()
    patterns = [
        re.compile(r"\bfun\s+(?:`([^`]+)`|([A-Za-z_][A-Za-z0-9_]*))\s*[<(]"),
        re.compile(r"\b(?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
        re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s+by\s+(?:mutableStateOf|remember|lazy|delegate)"),
    ]
    for pat in patterns:
        for m in pat.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if not name or name in seen:
                continue
            seen.add(name)
            members.append(name)
    return members


def verify_diff_symbols(
    *,
    diff: str,
    repo_root: Path,
    skip_files_in_diff: bool = True,
) -> VerificationReport:
    """Run symbol verification on a unified diff.

    ``skip_files_in_diff`` (default True): when True, the verifier
    treats files modified by the diff itself as "in flux" and does
    NOT use them as the source of truth for receiver declarations.
    A receiver declared INSIDE the diff target file is fine — but a
    receiver from a non-target file should grep against the live tree.
    """
    if not diff or not repo_root.exists():
        return VerificationReport()

    added = _added_lines_per_file(diff)
    if not added:
        return VerificationReport()

    diff_target_paths = set(added.keys())

    source_files = _walk_source_files(repo_root)
    if skip_files_in_diff:
        # Filter out files that the diff itself modifies, since their
        # current on-disk state is pre-patch and stale.
        source_files = [
            p for p in source_files
            if not any(
                p.as_posix().endswith("/" + tgt) or str(p).replace("\\", "/").endswith("/" + tgt)
                for tgt in diff_target_paths
            )
        ]

    findings: list[HallucinatedReference] = []
    scanned = 0
    skipped_block = 0
    skipped_unver = 0
    receiver_cache: dict[str, tuple[str | None, list[str]]] = {}

    for diff_file, lines in added.items():
        for line in lines:
            for m in _REF_PATTERN.finditer(line):
                receiver = m.group("receiver")
                member = m.group("member")
                if receiver in _RECEIVER_BLOCKLIST or member in _MEMBER_BLOCKLIST:
                    skipped_block += 1
                    continue
                if not _PASCAL_CASE.match(receiver):
                    # Non-PascalCase receiver — likely a variable. We
                    # can't reliably infer its type from a single diff
                    # line, so skip. compile_gate is the safety net.
                    skipped_unver += 1
                    continue
                scanned += 1
                if receiver in receiver_cache:
                    decl_path, members = receiver_cache[receiver]
                else:
                    decl_path, decl_text = _find_receiver_declaration(
                        receiver, source_files, repo_root
                    )
                    members = (
                        _extract_members_from_class_body(decl_text)
                        if decl_text else []
                    )
                    receiver_cache[receiver] = (decl_path, members)
                if decl_path is None:
                    # Receiver class not found in repo — could be an
                    # external library. Defer to compile_gate.
                    skipped_unver += 1
                    continue
                if member in members:
                    continue
                findings.append(
                    HallucinatedReference(
                        receiver=receiver,
                        member=member,
                        file=diff_file,
                        line=line.strip(),
                        receiver_resolved_to=decl_path,
                        available_members_sample=members[:8],
                    )
                )

    return VerificationReport(
        findings=findings,
        scanned_refs=scanned,
        skipped_refs_blocklist=skipped_block,
        skipped_refs_unverifiable=skipped_unver,
    )


def _is_jobaddress_demo() -> Iterable[str]:
    """Quick sanity demo so module imports clean."""
    return ()
