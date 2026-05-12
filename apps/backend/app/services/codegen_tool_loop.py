"""Bounded tool-use loop for codegen (Tier 4-H).

When the codegen model emits ``## EVIDENCE_GAP`` it's signalling
"I can't make a SEARCH/REPLACE region — I'm missing context X". The
existing terminal-marker handler at e2ee413 stops the retry storm but
just fails the codegen call. This module gives the harness one more
chance: parse the model's stated need, fetch the missing span from
disk, re-inject into the prompt, and re-run codegen once.

The codex consult on 2026-05-10 explicitly bounded this:
  - Constrained to existing candidate files / symbols (no arbitrary
    repo browsing)
  - Capped at ONE recovery round per codegen call
  - The model has to name what it wants; we don't speculate

Request grammar (the model emits this *instead* of EVIDENCE_GAP, or
appended to it). Comments on each line are optional:

    ## EVIDENCE_GAP_REQUEST
    file: django/db/models/fields/__init__.py
    symbol: contribute_to_class
    why: need closure binding site for _get_FIELD_display

Multiple requests in one block are allowed — separate with a blank
line. The harness fetches up to ``_MAX_REQUEST_HITS`` spans total.

The fetched content is added as a fresh ``=== EVIDENCE FETCH ===``
section in the user prompt for the second codegen attempt.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path


_MAX_REQUEST_HITS = 4
_MAX_SPAN_BYTES = 4_000


@dataclass(frozen=True)
class GapRequest:
    file: str | None
    symbol: str | None
    why: str = ""


@dataclass(frozen=True)
class FetchedSpan:
    file: str
    symbol: str | None
    body: str
    note: str = ""


_REQUEST_BLOCK_RE = re.compile(
    r"##\s*EVIDENCE[_\- ]?GAP[_\- ]?REQUEST\b(.*?)(?=^##|\Z)",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)
_FILE_RE = re.compile(r"^\s*file\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_SYMBOL_RE = re.compile(r"^\s*symbol\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_WHY_RE = re.compile(r"^\s*why\s*:\s*(.+)", re.IGNORECASE | re.MULTILINE)


_BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_PATH_RE = re.compile(r"\b([\w\-./]+/[\w\-./]+\.py)\b")


def parse_evidence_gap_requests(text: str) -> list[GapRequest]:
    """Parse zero-or-more requests out of an EVIDENCE_GAP response.

    Two paths:
    1. **Structured** — model followed the playbook and emitted
       ``## EVIDENCE_GAP_REQUEST`` blocks with ``file:`` / ``symbol:`` /
       ``why:`` fields. Preferred.
    2. **Implicit** — model wrote prose like
       ``## EVIDENCE_GAP: the full implementation of `_arithmetic_mask`
       in astropy/nddata/mixins/ndarithmetic.py is required``. Extract
       backtick-quoted identifiers + path-shaped tokens and synthesise
       GapRequests so Tier 4-H still fires. (DeepSeek 2026-05-10:
       v11 task 1 emitted plain EVIDENCE_GAP without the structured
       block.)

    Returns an empty list when neither path yields anything — caller
    treats that as "nothing to fetch, give up".
    """
    if not text:
        return []
    requests: list[GapRequest] = []
    for block_match in _REQUEST_BLOCK_RE.finditer(text):
        block = block_match.group(1)
        # Split on blank lines so multiple file/symbol pairs in one
        # block become separate requests.
        for chunk in re.split(r"\n\s*\n", block):
            file_m = _FILE_RE.search(chunk)
            symbol_m = _SYMBOL_RE.search(chunk)
            why_m = _WHY_RE.search(chunk)
            if not (file_m or symbol_m):
                continue
            requests.append(
                GapRequest(
                    file=(file_m.group(1).strip() if file_m else None),
                    symbol=(symbol_m.group(1).strip() if symbol_m else None),
                    why=(why_m.group(1).strip() if why_m else ""),
                )
            )
            if len(requests) >= _MAX_REQUEST_HITS:
                return requests
    if requests:
        return requests
    # Implicit path: scan EVIDENCE_GAP prose for backtick-quoted
    # identifiers + .py path tokens. Common DeepSeek phrasing:
    # "the full implementation of `_arithmetic_mask` in
    # astropy/nddata/mixins/ndarithmetic.py is required"
    if not re.search(r"##\s*EVIDENCE[_\- ]?GAP\b", text, re.IGNORECASE):
        return []
    paths = list(dict.fromkeys(_PATH_RE.findall(text)))
    symbols = list(dict.fromkeys(_BACKTICK_IDENT_RE.findall(text)))
    if not paths and not symbols:
        return []
    primary_path = paths[0] if paths else None
    if symbols and primary_path:
        # One request per symbol, all anchored to the named file.
        for sym in symbols[:_MAX_REQUEST_HITS]:
            requests.append(
                GapRequest(
                    file=primary_path,
                    symbol=sym,
                    why="implicit-from-EVIDENCE_GAP-prose",
                )
            )
    elif primary_path:
        requests.append(
            GapRequest(file=primary_path, symbol=None, why="implicit-from-EVIDENCE_GAP-prose")
        )
    elif symbols:
        for sym in symbols[:_MAX_REQUEST_HITS]:
            requests.append(
                GapRequest(file=None, symbol=sym, why="implicit-from-EVIDENCE_GAP-prose")
            )
    return requests


def fulfil_requests(
    requests: list[GapRequest],
    *,
    candidate_files: dict[str, str],
    repo_root: Path | None = None,
) -> list[FetchedSpan]:
    """For each request, find the named symbol's span and return it.

    Bounded surface: only fetches from ``candidate_files`` (the in-
    memory snapshot the planner already deemed relevant) and ``repo_
    root`` (the cached SWE-bench clone). No arbitrary browsing — the
    model has to have named the file already.

    Returns one FetchedSpan per request, capped at ``_MAX_REQUEST_HITS``.
    Skips requests we can't satisfy (with a note in ``FetchedSpan.note``
    when partial match found).
    """
    spans: list[FetchedSpan] = []
    for req in requests[:_MAX_REQUEST_HITS]:
        span = _fulfil_one(req, candidate_files=candidate_files, repo_root=repo_root)
        if span is not None:
            spans.append(span)
    return spans


def _fulfil_one(
    req: GapRequest,
    *,
    candidate_files: dict[str, str],
    repo_root: Path | None,
) -> FetchedSpan | None:
    # Resolve file: prefer exact match in candidate_files, then basename
    # match, then disk read off repo_root.
    text: str | None = None
    file_path: str | None = req.file
    if req.file:
        text = candidate_files.get(req.file)
        if text is None:
            base = req.file.replace("\\", "/").rsplit("/", 1)[-1]
            for path, body in candidate_files.items():
                if path.replace("\\", "/").endswith("/" + base) or path.endswith(base):
                    text = body
                    file_path = path
                    break
        if text is None and repo_root is not None:
            disk_path = (repo_root / req.file).resolve()
            try:
                # Stay inside the repo — block traversal escapes.
                disk_path.relative_to(repo_root.resolve())
                if disk_path.is_file():
                    text = disk_path.read_text(encoding="utf-8", errors="replace")
                    file_path = req.file
            except (ValueError, OSError):
                text = None

    if text is None:
        return None

    if not req.symbol:
        # Whole-file request — return up to the byte cap.
        truncated = text[:_MAX_SPAN_BYTES]
        note = "" if len(text) <= _MAX_SPAN_BYTES else f"truncated at {_MAX_SPAN_BYTES} bytes"
        return FetchedSpan(file=file_path or "?", symbol=None, body=truncated, note=note)

    span_body = _extract_symbol_span(text, req.symbol)
    if span_body is None:
        return None
    if len(span_body) > _MAX_SPAN_BYTES:
        span_body = span_body[:_MAX_SPAN_BYTES]
        note = f"symbol body truncated at {_MAX_SPAN_BYTES} bytes"
    else:
        note = ""
    return FetchedSpan(file=file_path or "?", symbol=req.symbol, body=span_body, note=note)


def _extract_symbol_span(source: str, symbol: str) -> str | None:
    """Return the source range covering ``symbol``'s definition.

    Uses ast.parse first, falls back to indent-based regex when the
    parser rejects the file (Python 3.14 strict cases).
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _regex_extract_symbol(source, symbol)

    lines = source.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if node.name == symbol:
                start = max(0, node.lineno - 1)
                end = node.end_lineno or len(lines)
                return "".join(lines[start:end])
    return None


_REGEX_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+|class\s+", re.MULTILINE
)


def _regex_extract_symbol(source: str, symbol: str) -> str | None:
    """Indent-tracking fallback for files ast.parse rejects.

    Walks lines, finds `def NAME(` / `class NAME(`, records indent,
    captures lines until the next sibling-or-shallower def/class.
    """
    lines = source.splitlines(keepends=True)
    target_re = re.compile(
        rf"^(?P<indent>[ \t]*)(?:async\s+)?(?:def|class)\s+{re.escape(symbol)}\b"
    )
    start_idx = -1
    start_indent = -1
    for i, line in enumerate(lines):
        m = target_re.match(line)
        if m:
            start_idx = i
            start_indent = len(m.group("indent"))
            break
    if start_idx < 0:
        return None
    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        line = lines[j]
        stripped = line.lstrip(" \t")
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        if indent <= start_indent and re.match(
            r"(?:async\s+)?(?:def|class)\s+\w+", stripped
        ):
            end_idx = j
            break
    return "".join(lines[start_idx:end_idx])


def render_spans_for_prompt(spans: list[FetchedSpan]) -> str:
    """Format fetched spans as a prompt section the model can read.

    Header makes the new context explicit so the model knows it now
    has what it asked for and should not re-emit EVIDENCE_GAP for the
    same names.
    """
    if not spans:
        return ""
    parts = [
        "=== EVIDENCE FETCH (you asked for these in the prior round) ===",
        "These spans come straight from the source repo and are GROUND TRUTH.",
        "Use them as your Aider SEARCH anchors. Do NOT re-emit EVIDENCE_GAP",
        "for any of the names listed below — they are now available verbatim.",
        "",
    ]
    for span in spans:
        header = f"--- {span.file}"
        if span.symbol:
            header += f" :: {span.symbol}"
        if span.note:
            header += f" ({span.note})"
        header += " ---"
        parts.extend([header, span.body, ""])
    return "\n".join(parts)
