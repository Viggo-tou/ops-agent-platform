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


_PY_DEF_RE = re.compile(r"^(?:[ \t]*)(?:async\s+)?def\s+(\w+)\s*\(", re.MULTILINE)


def _function_names_with_ancestors(
    content: str, concept_words: set[str]
) -> list[str]:
    """For each function whose name matches a concept word, return that
    name plus every enclosing function/class name on the path to the
    module root.

    Tries ast.parse for accurate ancestor walking; falls back to a
    regex/indent heuristic when the parser rejects the file. The
    ancestor expansion is what addresses the closure-pinning case
    (e.g. `_get_FIELD_display` defined inside `Field.contribute_to_class`).
    """
    matched: list[str] = []
    try:
        import ast as _ast

        tree = _ast.parse(content)
    except (SyntaxError, ValueError):
        # Regex fallback: same logic by indent.
        return _regex_function_names_with_ancestors(content, concept_words)

    def visit(node: object, ancestors: list[str]) -> None:
        # Walk every body-bearing attribute. Closures may live inside
        # If / For / While / Try / With / orelse / finalbody / handlers
        # — without recursing through those, `_get_FIELD_display`
        # defined inside `if self.choices is not None:` is invisible
        # to the ancestor walk.
        bodies: list[list] = []
        for attr in ("body", "orelse", "finalbody"):
            block = getattr(node, attr, None)
            if isinstance(block, list):
                bodies.append(block)
        # try/except handlers: each handler has its own body
        handlers = getattr(node, "handlers", None)
        if isinstance(handlers, list):
            for h in handlers:
                if isinstance(getattr(h, "body", None), list):
                    bodies.append(h.body)
        for block in bodies:
            for child in block:
                if isinstance(
                    child, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)
                ):
                    child_name = child.name
                    next_ancestors = ancestors + [child_name]
                    if isinstance(
                        child, (_ast.FunctionDef, _ast.AsyncFunctionDef)
                    ):
                        name_lower = child_name.lower()
                        if any(word in name_lower for word in concept_words):
                            for ancestor in next_ancestors:
                                matched.append(ancestor)
                    visit(child, next_ancestors)
                else:
                    # Compound statement (If/For/While/Try/With/etc.)
                    # — preserve ancestors, descend.
                    visit(child, ancestors)

    visit(tree, [])

    seen: set[str] = set()
    deduped: list[str] = []
    for name in matched:
        if name in seen:
            continue
        seen.add(name)
        deduped.append(name)
    return deduped


def _regex_function_names_with_ancestors(
    content: str, concept_words: set[str]
) -> list[str]:
    """Indent-based ancestor recovery for files ast.parse rejects."""
    lines = content.splitlines()
    # Stack of (indent, name) for currently-open def/class scopes.
    stack: list[tuple[int, str]] = []
    matched: list[str] = []
    for raw in lines:
        # Compute leading whitespace columns.
        stripped = raw.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(stripped)
        # Pop scopes that we've exited.
        while stack and stack[-1][0] >= indent:
            stack.pop()
        m_def = re.match(r"(?:async\s+)?def\s+(\w+)\s*\(", stripped)
        m_class = re.match(r"class\s+(\w+)\b", stripped)
        if m_def:
            name = m_def.group(1)
            stack.append((indent, name))
            if any(w in name.lower() for w in concept_words):
                for _ind, ancestor in stack:
                    matched.append(ancestor)
            continue
        if m_class:
            name = m_class.group(1)
            stack.append((indent, name))
    seen: set[str] = set()
    out: list[str] = []
    for n in matched:
        if n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def _function_names_in_python(content: str) -> list[str]:
    """Return all function/method names in a Python source string.

    Tries ast.parse first (precise), falls back to a regex scan when
    the parser rejects the file. Real-world example: Python 3.14
    rejects astropy/nddata/mixins/ndarithmetic.py with a triple-quote
    parity error even though the file otherwise runs fine; the regex
    fallback recovers ~ all top-level function/method names.
    """
    try:
        import ast as _ast

        tree = _ast.parse(content)
        return [
            node.name
            for node in _ast.walk(tree)
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
        ]
    except (SyntaxError, ValueError):
        # Best-effort: scan for line-leading `def name(` / `async def name(`.
        # Misses `def` defined inside string literals, but that's a
        # vanishingly small corner case and not worth fighting for.
        return [m.group(1) for m in _PY_DEF_RE.finditer(content)]


# Concept words: lowercase tokens 4+ chars that look like content words
# in the issue text. Used to fuzzy-match against function names defined
# in candidate files. The 4-char floor + stopword filter keeps "the",
# "with", "this" out without listing every English filler.
# Concept-word splitter. Underscore-joined identifiers (`get_foo_display`)
# need to yield each component (`get`, `foo`, `display`) as separate
# concept-word candidates, so we split on any non-letter — `\b` alone
# stops at letter/digit boundaries but not at underscores.
_CONCEPT_WORD = re.compile(r"[a-z]{4,}")
_CONCEPT_STOPWORDS = _STOPWORDS | {
    "above", "after", "again", "also", "another", "around", "before",
    "below", "between", "during", "every", "first", "found", "from",
    "further", "into", "later", "more", "most", "much", "other", "over",
    "result", "same", "since", "then", "there", "they", "those", "though",
    "through", "under", "very", "while", "within", "without",
    "happens", "happen", "called", "calling", "actually", "during",
}


def extract_keep_symbols_for_files(
    issue_text: str,
    files: dict[str, str],
) -> list[str]:
    """Function/method names worth pinning for the codegen prompt.

    Two complementary signals are unioned:

    1. Direct names mentioned in the issue text (delegates to
       :func:`extract_candidate_symbols`).
    2. Functions defined in ``files`` whose name contains a content word
       from the issue text. Catches the case the SWE-bench astropy
       regression hit on 2026-05-10: the issue says ``mask propagation
       fails`` but never names ``_arithmetic_mask`` — yet that's the
       function the model needs to edit. Cross-referencing the file's
       AST against the issue's concept words bridges the gap.

    Pure Python; non-.py files contribute only to signal 1. Result
    deduplicated, capped at 8 entries.
    """
    direct = list(extract_candidate_symbols(issue_text, file_contents=files))

    if not files:
        return direct

    issue_lower = (issue_text or "").lower()
    concept_words = {
        w for w in _CONCEPT_WORD.findall(issue_lower)
        if w not in _CONCEPT_STOPWORDS
    }
    if not concept_words:
        return direct

    indirect: list[str] = []
    for path, content in files.items():
        if not path.endswith(".py") or not content:
            continue
        # When a concept-word matches a function name, also pin its
        # ancestor functions (e.g. `_get_FIELD_display` is defined as
        # a closure inside `Field.contribute_to_class`; pinning only
        # the closure is insufficient because its surrounding
        # binding/registration context lives in the parent body).
        for name in _function_names_with_ancestors(content, concept_words):
            indirect.append(name)

    seen: set[str] = set(direct)
    merged: list[str] = list(direct)
    for name in indirect:
        if name in seen:
            continue
        seen.add(name)
        merged.append(name)
        if len(merged) >= _MAX_HINTS:
            break
    return merged
