"""Detect whether a diff's non-trivial changes are comment-only.

Used to harden goal_decomposition: a file flagged as "unjustified" (modified
but not clearly advancing any goal) is merely a warn; but if that file's
modifications are ALL comments/whitespace, it's almost certainly a CLI agent
placating the review with self-documenting notes — escalate to block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Lightweight language detection: extension -> (single-line comment marker,
# block comment open, block comment close). Only used to classify lines.
# ``None`` means "no block-comment syntax" for that family.
_LANG_COMMENTS: dict[str, tuple[str | None, str | None, str | None]] = {
    ".js": ("//", "/*", "*/"),
    ".jsx": ("//", "/*", "*/"),
    ".ts": ("//", "/*", "*/"),
    ".tsx": ("//", "/*", "*/"),
    ".mjs": ("//", "/*", "*/"),
    ".cjs": ("//", "/*", "*/"),
    ".py": ("#", None, None),
    ".go": ("//", "/*", "*/"),
    ".rs": ("//", "/*", "*/"),
    ".java": ("//", "/*", "*/"),
    ".c": ("//", "/*", "*/"),
    ".cpp": ("//", "/*", "*/"),
    ".cs": ("//", "/*", "*/"),
    ".sh": ("#", None, None),
    ".bash": ("#", None, None),
    ".rb": ("#", None, None),
    ".yml": ("#", None, None),
    ".yaml": ("#", None, None),
    ".toml": ("#", None, None),
    ".ini": (";", None, None),
    ".html": ("<!--", "<!--", "-->"),
    ".md": (None, "<!--", "-->"),
    # Unknown / not listed: treat as no comment syntax (all lines non-comment).
}


@dataclass(frozen=True)
class CommentOnlyReport:
    file_path: str
    is_comment_only: bool
    added_lines: int
    removed_lines: int
    added_comment_lines: int
    added_code_lines: int


def _single_line_is_comment(stripped: str, single: str | None) -> bool:
    if single is None:
        return False
    return stripped.startswith(single)


def _classify_added_line(stripped: str, ext: str, block_state: list[bool]) -> bool:
    """Return True if the added line is comment-only (no code contribution)."""
    if not stripped:
        return True  # pure whitespace
    lang = _LANG_COMMENTS.get(ext)
    if lang is None:
        return False  # unknown language, be conservative
    single, bopen, bclose = lang
    in_block = block_state[0]

    if in_block:
        if bclose and bclose in stripped:
            block_state[0] = False
        return True

    if _single_line_is_comment(stripped, single):
        return True

    if bopen and bopen in stripped:
        # Enter a block comment; if it closes on same line, we stay out.
        if bclose and bclose in stripped[stripped.index(bopen) + len(bopen):]:
            return True
        block_state[0] = True
        return True

    return False


def analyze_file_hunks(file_path: str, hunks_text: str) -> CommentOnlyReport:
    """Classify whether the diff hunks for a single file are comment-only.

    hunks_text: the diff section for this file (from 'diff --git' line through
    the end of this file's hunks). Counts only ``+`` lines (additions) toward
    comment-vs-code classification, since a pure-removal is always a change.
    """
    _, ext = _split_ext(file_path)
    added_lines = 0
    removed_lines = 0
    added_comment_lines = 0
    added_code_lines = 0
    block_state = [False]

    for line in hunks_text.splitlines():
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added_lines += 1
            content = line[1:]
            stripped = content.strip()
            if _classify_added_line(stripped, ext, block_state):
                added_comment_lines += 1
            else:
                added_code_lines += 1
        elif line.startswith("-"):
            removed_lines += 1

    is_comment_only = (
        added_code_lines == 0
        and removed_lines == 0
        and added_lines > 0
    )
    return CommentOnlyReport(
        file_path=file_path,
        is_comment_only=is_comment_only,
        added_lines=added_lines,
        removed_lines=removed_lines,
        added_comment_lines=added_comment_lines,
        added_code_lines=added_code_lines,
    )


def _split_ext(path: str) -> tuple[str, str]:
    idx = path.rfind(".")
    if idx < 0:
        return path, ""
    return path[:idx], path[idx:].lower()


def split_diff_by_file(diff_text: str) -> dict[str, str]:
    """Split a multi-file unified diff into {path: hunks_text} segments."""
    out: dict[str, str] = {}
    if not diff_text:
        return out
    for section in re.split(r"(?=^diff --git )", diff_text, flags=re.MULTILINE):
        section = section.strip()
        if not section:
            continue
        m = re.match(r"diff --git a/(.+?) b/", section)
        if m is None:
            continue
        out[m.group(1).strip()] = section
    return out


def classify_diff(diff_text: str) -> dict[str, CommentOnlyReport]:
    """Return a {path: CommentOnlyReport} mapping for every file in the diff."""
    out: dict[str, CommentOnlyReport] = {}
    for path, section in split_diff_by_file(diff_text).items():
        out[path] = analyze_file_hunks(path, section)
    return out
