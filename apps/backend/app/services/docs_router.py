"""Playbook router — scans docs/agent-playbooks/ for files with frontmatter
``triggers:`` lists and returns the top-N matches for a chat query.

Design choices:

- **No INDEX.md.** Each playbook is self-describing via YAML frontmatter
  with a ``triggers`` array. We scan all .md files at startup and keep
  an in-memory index. Adding a playbook = drop a file; removing one =
  delete the file. Zero index-maintenance burden.

- **Cold-start aware.** When no playbook matches a query, we don't pretend
  to have one — the caller logs a "playbook miss" so the team knows what
  to write next. The chat path falls back to the generic system prompt.

- **Cheap match.** Tokenize the query, score each playbook by how many
  trigger keywords appear (substring match, case-insensitive). No LLM.

- **Hot reload.** ``rebuild_index()`` is exposed so dev mode can refresh
  without a backend restart. Production uses startup-time index.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# docs/agent-playbooks/ lives at the repo root. Service module is at
# apps/backend/app/services/docs_router.py — five parents up = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLAYBOOKS_DIR = _REPO_ROOT / "docs" / "agent-playbooks"

# Where playbook misses are appended (one JSON line per miss).
_MISS_LOG = _REPO_ROOT / "apps" / "backend" / "data" / "playbook_miss.jsonl"


@dataclass
class Playbook:
    name: str                  # filename stem
    relpath: str               # relative to repo root, for citation
    triggers: list[str]        # lowercase trigger keywords
    stack: list[str]           # optional stack hints (e.g. android, kotlin)
    task_type: list[str]       # optional task-type hints (codegen, debug, plan)
    title: str                 # h1 from the doc body
    body: str                  # full markdown body (after frontmatter)


@dataclass
class RouterMatch:
    playbook: Playbook
    score: int
    matched_triggers: list[str]


@dataclass
class _Index:
    playbooks: list[Playbook] = field(default_factory=list)
    last_built_mtime: float = 0.0


_index = _Index()


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, list[str]], str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    fields: dict[str, list[str]] = {}
    current_key: str | None = None
    for line in raw.splitlines():
        if not line.strip():
            continue
        # Inline form: `triggers: [a, b, c]`.
        m_inline = re.match(r"^([a-zA-Z_]+):\s*\[(.*)\]\s*$", line)
        if m_inline:
            key = m_inline.group(1)
            items = [t.strip().strip('"').strip("'") for t in m_inline.group(2).split(",") if t.strip()]
            fields[key] = items
            current_key = None
            continue
        # Block form: `triggers:` then `  - foo`.
        m_block = re.match(r"^([a-zA-Z_]+):\s*$", line)
        if m_block:
            current_key = m_block.group(1)
            fields[current_key] = []
            continue
        m_item = re.match(r"^\s*-\s+(.*)$", line)
        if m_item and current_key is not None:
            fields[current_key].append(m_item.group(1).strip().strip('"').strip("'"))
            continue
        # Scalar field like `title: foo` — store as single-item list for uniformity.
        m_scalar = re.match(r"^([a-zA-Z_]+):\s*(.+)$", line)
        if m_scalar:
            fields[m_scalar.group(1)] = [m_scalar.group(2).strip().strip('"').strip("'")]
            current_key = None
    return fields, body


def _load_one(path: Path) -> Playbook | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("docs_router: cannot read %s: %s", path, exc)
        return None
    fields, body = _parse_frontmatter(text)
    triggers = [t.lower() for t in fields.get("triggers", []) if t]
    if not triggers:
        # Skip files without a triggers list — they can't route.
        return None
    title_match = re.match(r"^\s*#\s+(.+?)\s*$", body, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    return Playbook(
        name=path.stem,
        relpath=str(path.relative_to(_REPO_ROOT)).replace("\\", "/"),
        triggers=triggers,
        stack=[s.lower() for s in fields.get("stack", [])],
        task_type=[t.lower() for t in fields.get("task_type", [])],
        title=title,
        body=body.strip(),
    )


def rebuild_index() -> int:
    """Scan docs/agent-playbooks/**/*.md and rebuild the in-memory index.

    Returns the number of playbooks loaded.
    """
    if not _PLAYBOOKS_DIR.is_dir():
        _index.playbooks = []
        return 0
    found: list[Playbook] = []
    for md in _PLAYBOOKS_DIR.rglob("*.md"):
        pb = _load_one(md)
        if pb is not None:
            found.append(pb)
    _index.playbooks = found
    return len(found)


def get_index() -> list[Playbook]:
    if not _index.playbooks:
        rebuild_index()
    return list(_index.playbooks)


def _score_playbook(pb: Playbook, query_lc: str) -> tuple[int, list[str]]:
    matched: list[str] = []
    score = 0
    for trig in pb.triggers:
        if trig and trig in query_lc:
            matched.append(trig)
            # Longer trigger = stronger signal (so 'session' beats 'a').
            score += max(1, len(trig))
    return score, matched


def find_matching(query: str, top_k: int = 3) -> list[RouterMatch]:
    """Return up to ``top_k`` playbooks whose triggers appear in the query.

    No fancy ranking — just sum-of-trigger-lengths. Easy to reason about,
    good enough for short chat queries. Use full-text search later if this
    proves too coarse.
    """
    query_lc = (query or "").lower()
    if not query_lc:
        return []
    out: list[RouterMatch] = []
    for pb in get_index():
        score, matched = _score_playbook(pb, query_lc)
        if score > 0:
            out.append(RouterMatch(playbook=pb, score=score, matched_triggers=matched))
    out.sort(key=lambda m: m.score, reverse=True)
    return out[:top_k]


def record_miss(query: str, intent: str, *, signals: list[str] | None = None) -> None:
    """Append a playbook-miss event for later review.

    Cold-start safety: when no playbook matches and the intent really wanted
    one (find_in_docs / develop_task), we want the user / dev to see what
    playbooks are missing so the backlog grows organically.
    """
    try:
        _MISS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _MISS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": __import__("time").time(),
                        "query": (query or "")[:300],
                        "intent": intent,
                        "signals": list(signals or []),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except OSError as exc:
        logger.debug("docs_router: cannot write miss log: %s", exc)


def format_playbooks_for_prompt(matches: Iterable[RouterMatch], max_chars: int = 3000) -> str:
    """Render matches as a markdown block to inject into system prompt."""
    parts: list[str] = []
    used = 0
    for m in matches:
        pb = m.playbook
        header = f"\n### Playbook: **{pb.title}**  (`{pb.relpath}`)"
        body = pb.body.strip()
        chunk = f"{header}\n{body}\n"
        if used + len(chunk) > max_chars and parts:
            break
        parts.append(chunk)
        used += len(chunk)
    if not parts:
        return ""
    return "\n## 相关 playbook(查到下面这些,先依据它们再回答)\n" + "\n".join(parts)
