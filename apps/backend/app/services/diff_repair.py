from __future__ import annotations

import re
from dataclasses import dataclass


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<trailing>.*)$"
)


@dataclass(frozen=True)
class DiffRepairResult:
    repaired_diff: str
    repairs_applied: list[str]
    file_count: int


@dataclass
class _FileSection:
    lines: list[str]
    path: str | None


def repair_diff(raw_diff: str, context_files: dict[str, str] | None = None) -> DiffRepairResult:
    """Parse and repair common structural errors in an LLM-generated unified diff."""
    if not raw_diff:
        return DiffRepairResult(repaired_diff="", repairs_applied=[], file_count=0)

    repairs: list[str] = []
    sections = _split_file_sections(raw_diff)
    if not sections:
        repaired = raw_diff if raw_diff.endswith("\n") else f"{raw_diff}\n"
        return DiffRepairResult(repaired_diff=repaired, repairs_applied=[], file_count=0)

    repaired_sections: list[str] = []
    for section in sections:
        section_text, section_repairs = _repair_file_section(section, context_files or {})
        repaired_sections.append(section_text.rstrip("\n"))
        repairs.extend(section_repairs)

    repaired_diff = "\n\n".join(section for section in repaired_sections if section)
    if repaired_diff:
        repaired_diff += "\n"

    if len(repaired_sections) > 1:
        raw_joined = "\n".join(line for section in sections for line in section.lines)
        if "\n\ndiff --git " not in raw_joined and "\ndiff --git " in raw_joined:
            repairs.append("added blank separators between file diffs")

    return DiffRepairResult(
        repaired_diff=repaired_diff,
        repairs_applied=repairs,
        file_count=len(sections),
    )


def _split_file_sections(raw_diff: str) -> list[_FileSection]:
    sections: list[_FileSection] = []
    current: list[str] | None = None

    for line in raw_diff.splitlines():
        if line.startswith("diff --git "):
            if current:
                sections.append(_FileSection(lines=current, path=_extract_path(current)))
            current = [line.rstrip("\r")]
            continue
        if current is not None:
            current.append(line.rstrip("\r"))

    if current:
        sections.append(_FileSection(lines=current, path=_extract_path(current)))
    return sections


def _extract_path(lines: list[str]) -> str | None:
    for line in lines:
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("--- a/"):
            return line[6:].strip()
    for line in lines:
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                return parts[3][2:]
    return None


def _repair_file_section(
    section: _FileSection,
    context_files: dict[str, str],
) -> tuple[str, list[str]]:
    repairs: list[str] = []
    lines = section.lines
    repaired: list[str] = []
    index = 0

    while index < len(lines):
        line = lines[index]
        match = _HUNK_HEADER_RE.match(line)
        if match is None:
            repaired.append(line)
            index += 1
            continue

        body: list[str] = []
        index += 1
        while index < len(lines):
            candidate = lines[index]
            if candidate.startswith("diff --git ") or _HUNK_HEADER_RE.match(candidate):
                break
            if _is_hunk_body_line(candidate):
                body.append(candidate)
                index += 1
                continue
            repairs.append("stripped trailing non-diff text after hunk")
            while index < len(lines):
                candidate = lines[index]
                if candidate.startswith("diff --git ") or _HUNK_HEADER_RE.match(candidate):
                    break
                index += 1
            break

        old_start = int(match.group("old_start"))
        new_start = int(match.group("new_start"))
        old_count = _count_old_lines(body)
        new_count = _count_new_lines(body)
        corrected_old_start = _find_context_start(section.path, body, context_files, old_start)
        if corrected_old_start is not None and corrected_old_start != old_start:
            old_start = corrected_old_start
            repairs.append(f"corrected hunk start for {section.path or 'unknown file'}")

        original_old_count = int(match.group("old_count") or "1")
        original_new_count = int(match.group("new_count") or "1")
        if original_old_count != old_count or original_new_count != new_count:
            repairs.append(f"corrected hunk line counts for {section.path or 'unknown file'}")

        header = _format_hunk_header(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            old_had_count=match.group("old_count") is not None,
            new_had_count=match.group("new_count") is not None,
            trailing=match.group("trailing"),
        )
        if header != line and not any(
            repair.startswith("corrected hunk") for repair in repairs[-2:]
        ):
            repairs.append(f"corrected hunk header for {section.path or 'unknown file'}")
        repaired.append(header)
        repaired.extend(body)

    return "\n".join(repaired), repairs


def _is_hunk_body_line(line: str) -> bool:
    return line == "" or line.startswith((" ", "+", "-", "\\"))


def _count_old_lines(lines: list[str]) -> int:
    return sum(1 for line in lines if line.startswith((" ", "-")))


def _count_new_lines(lines: list[str]) -> int:
    return sum(1 for line in lines if line.startswith((" ", "+")))


def _format_hunk_header(
    *,
    old_start: int,
    old_count: int,
    new_start: int,
    new_count: int,
    old_had_count: bool,
    new_had_count: bool,
    trailing: str,
) -> str:
    old_range = _format_range(old_start, old_count, old_had_count)
    new_range = _format_range(new_start, new_count, new_had_count)
    return f"@@ -{old_range} +{new_range} @@{trailing}"


def _format_range(start: int, count: int, had_count: bool) -> str:
    if count == 1 and not had_count:
        return str(start)
    return f"{start},{count}"


def _find_context_start(
    path: str | None,
    hunk_body: list[str],
    context_files: dict[str, str],
    current_old_start: int,
) -> int | None:
    if path is None or path not in context_files:
        return None

    old_lines = [line[1:] for line in hunk_body if line.startswith((" ", "-"))]
    if not old_lines:
        return None

    source_lines = context_files[path].splitlines()
    max_start = len(source_lines) - len(old_lines)
    if max_start < 0:
        return None

    current_index = current_old_start - 1
    if 0 <= current_index <= max_start and source_lines[current_index : current_index + len(old_lines)] == old_lines:
        return current_old_start

    for start in range(max_start + 1):
        if source_lines[start : start + len(old_lines)] == old_lines:
            return start + 1
    return None
