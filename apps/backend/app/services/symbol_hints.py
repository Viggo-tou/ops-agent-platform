"""Candidate symbol extractor for codegen evidence pinning (Tier 2).

When an issue says "fix the _arithmetic_mask method" we want the AST
truncator to keep that function body whole even when the file is over
budget. This module extracts symbol-name candidates from free-text
issue / task descriptions and intersects them with what's actually
defined in the candidate files, so we never feed the truncator a
fabricated name.

Heuristics, in order:

1. Underscore-prefixed identifiers (``_foo``, ``__bar``) — almost always
   internal Python names the user would only mention if specifically
   talking about them.
2. ``snake_case_identifier`` mentioned 2+ times in the same text — the
   issue is repeatedly invoking that name.
3. ``CamelCase`` only when followed by ``(`` (likely a class/function
   call) and present in the file content.
4. Backtick-quoted identifiers (```foo```) — explicit user marking.

Returns a deduplicated list, capped at 8 entries (the AST truncator
treats the list as a soft pin set; more than that and we're guessing).
"""
from __future__ import annotations

import re

_UNDERSCORE_NAME = re.compile(r"\b(_[a-zA-Z][a-zA-Z0-9_]*)\b")
_SNAKE_NAME = re.compile(r"\b([a-z][a-z0-9_]*[a-z0-9_])\b")
_CAMEL_CALL = re.compile(r"\b([A-Z][A-Za-z0-9]+)(?=\()")
_BACKTICK = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_MAX_HINTS = 8

# Words to exclude — they look like identifiers but are noise.
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "into", "when",
    "should", "would", "could", "doesn", "isn", "won", "can", "will",
    "must", "are", "not", "but", "yes", "no", "ok", "use", "using",
    "when", "where", "which", "what", "who", "how", "why", "all",
    "any", "some", "fix", "bug", "issue", "test", "tests", "method",
    "methods", "function", "functions", "class", "classes", "file",
    "files", "code", "issue", "error", "problem",
}


def extract_candidate_symbols(text: str, *, file_contents: dict[str, str] | None = None) -> list[str]:
    """Extract likely-relevant symbol names from free-text input.

    When ``file_contents`` is given, candidates are filtered to those
    that actually appear in at least one of the provided files. This
    keeps us from pinning fabricated names.
    """
    if not text:
        return []

    found: dict[str, int] = {}

    def add(name: str) -> None:
        if not name or len(name) < 3:
            return
        if name.lower() in _STOPWORDS:
            return
        found[name] = found.get(name, 0) + 1

    for m in _BACKTICK.finditer(text):
        add(m.group(1))
    for m in _UNDERSCORE_NAME.finditer(text):
        add(m.group(1))
    for m in _CAMEL_CALL.finditer(text):
        add(m.group(1))
    for m in _SNAKE_NAME.finditer(text):
        # Only keep snake_case names that appear multiple times in the
        # text — singletons are too likely to be ordinary English.
        name = m.group(1)
        if "_" in name:
            add(name)

    # Repeat-frequency filter for snake_case (2+ occurrences) — already
    # naturally captured because we increment the count. Backtick /
    # underscore-prefixed names pass with count=1 because they're
    # explicit signals.
    candidates: list[str] = []
    for name, count in found.items():
        if name.startswith("_") or name in _backtick_names(text):
            candidates.append(name)
        elif "_" in name and count >= 2:
            candidates.append(name)
        elif name[:1].isupper() and count >= 1:
            candidates.append(name)

    if file_contents:
        # Filter out anything not actually present in any candidate file.
        joined = "\n".join(file_contents.values())
        candidates = [c for c in candidates if c in joined]

    # Stable order: preserve discovery order, dedupe, cap.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
        if len(deduped) >= _MAX_HINTS:
            break
    return deduped


def _backtick_names(text: str) -> set[str]:
    return {m.group(1) for m in _BACKTICK.finditer(text)}
