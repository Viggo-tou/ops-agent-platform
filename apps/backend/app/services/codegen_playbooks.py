"""Codegen playbook router.

Reads ``docs/agent-playbooks/codegen/*.md`` at startup, indexes them by
``language`` / ``applies_to`` frontmatter, and exposes
``select_playbooks(language, file_paths)`` so codegen can inject the
relevant rules into its prompt.

This is the codegen-side counterpart to ``docs_router.py`` (which
serves chat). They live separately because:

- Chat playbooks are query-driven (free-text user message → trigger
  word match). Codegen playbooks are task-driven (language + file
  globs).
- Chat playbooks are short and ad-hoc. Codegen playbooks are longer,
  structured rules the model is expected to follow.
- Mixing the two indexes makes them harder to maintain.

Frontmatter schema (YAML between ``---`` delimiters at file head):

    language: python | kotlin | typescript | any
    applies_to:
      - "*.py"
      - pyproject.toml
      - any
    audience: codegen-llm
    priority: high | medium | low

Selection rules:

1. ``priority=high`` playbooks targeting ``language=any`` always
   included (e.g. diff-discipline.md).
2. ``language`` matches the task's detected language → playbook
   included.
3. Within priority tier, sort alphabetically by filename for stable
   prompt ordering (caching downstream).

The whole module is sync, no LLM calls; selection is a few hundred μs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# app/services/codegen_playbooks.py is 4 levels deep relative to repo
# root (repo/apps/backend/app/services/codegen_playbooks.py).
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CODEGEN_DIR = _REPO_ROOT / "docs" / "agent-playbooks" / "codegen"

# Language buckets we currently recognize. Anything else falls back to
# `any`-only playbook selection.
_KNOWN_LANGUAGES = frozenset(
    {"python", "kotlin", "typescript", "javascript", "java", "go", "rust", "any"}
)
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class CodegenPlaybook:
    """One parsed codegen playbook file."""

    path: Path
    name: str  # filename without extension
    language: str
    applies_to: tuple[str, ...]
    audience: str
    priority: str
    body: str  # everything after the closing `---` frontmatter line

    @property
    def priority_rank(self) -> int:
        return _PRIORITY_ORDER.get(self.priority, 99)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)\Z", re.DOTALL)
_LIST_LINE_RE = re.compile(r"^\s*-\s+(.+?)\s*$")


def _parse_frontmatter(text: str) -> tuple[dict[str, object], str]:
    """Parse a tiny subset of YAML — just what our playbooks need.

    Supports scalar key:value and list-of-strings under a key:
        key: value
        listkey:
          - a
          - b

    Anything more exotic (nested objects, multi-line strings) is not
    needed and intentionally not supported. Returns ({}, text) when the
    file has no frontmatter so the caller can decide whether to skip.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw_meta, body = match.group(1), match.group(2)
    meta: dict[str, object] = {}
    current_list_key: str | None = None
    for line in raw_meta.splitlines():
        if not line.strip():
            current_list_key = None
            continue
        list_match = _LIST_LINE_RE.match(line)
        if list_match and current_list_key is not None:
            meta.setdefault(current_list_key, []).append(_strip_quotes(list_match.group(1)))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not value:
            current_list_key = key
            meta.setdefault(key, [])
        else:
            meta[key] = _strip_quotes(value)
            current_list_key = None
    return meta, body


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _to_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str):
        return (value,)
    return ()


@dataclass
class _Index:
    playbooks: list[CodegenPlaybook] = field(default_factory=list)


_INDEX = _Index()


def rebuild_index(playbook_dir: Path | None = None) -> int:
    """Re-scan ``docs/agent-playbooks/codegen`` and rebuild the index.

    Returns the count of valid playbooks loaded. Files with no
    frontmatter are skipped with a warning. Hot-reload friendly.
    """
    target_dir = Path(playbook_dir) if playbook_dir is not None else _CODEGEN_DIR
    loaded: list[CodegenPlaybook] = []
    if not target_dir.is_dir():
        logger.info("codegen_playbook_dir_missing", extra={"dir": str(target_dir)})
        _INDEX.playbooks = loaded
        return 0

    for path in sorted(target_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("codegen_playbook_read_failed", extra={"path": str(path), "error": str(exc)})
            continue
        meta, body = _parse_frontmatter(text)
        if not meta:
            logger.info("codegen_playbook_no_frontmatter", extra={"path": str(path)})
            continue
        language = str(meta.get("language") or "any").lower()
        if language not in _KNOWN_LANGUAGES:
            logger.info(
                "codegen_playbook_unknown_language",
                extra={"path": str(path), "language": language},
            )
            language = "any"
        playbook = CodegenPlaybook(
            path=path,
            name=path.stem,
            language=language,
            applies_to=_to_tuple(meta.get("applies_to") or ("any",)),
            audience=str(meta.get("audience") or "codegen-llm"),
            priority=str(meta.get("priority") or "medium").lower(),
            body=body.strip(),
        )
        loaded.append(playbook)

    _INDEX.playbooks = loaded
    logger.info("codegen_playbook_index_built", extra={"count": len(loaded)})
    return len(loaded)


def all_playbooks() -> list[CodegenPlaybook]:
    """Return the loaded playbooks (empty if rebuild_index never ran)."""
    return list(_INDEX.playbooks)


def select_playbooks(
    *,
    language: str,
    file_paths: Iterable[str] = (),
) -> list[CodegenPlaybook]:
    """Pick playbooks relevant to ``language`` and any ``file_paths``.

    Inclusion rules (a playbook is included if any rule matches):
    - ``language == "any"`` and priority is ``high``.
    - ``language`` matches the task's language.
    - Any ``applies_to`` glob matches at least one of ``file_paths``.

    The returned list is sorted by priority (high first), then by name.
    """
    target_lang = (language or "").strip().lower()
    file_list = [str(p) for p in file_paths]
    selected: list[CodegenPlaybook] = []

    for playbook in _INDEX.playbooks:
        if playbook.language == "any" and playbook.priority == "high":
            selected.append(playbook)
            continue
        if target_lang and playbook.language == target_lang:
            selected.append(playbook)
            continue
        if file_list and any(
            _glob_matches(pattern, file_list) for pattern in playbook.applies_to
        ):
            selected.append(playbook)
            continue

    selected.sort(key=lambda p: (p.priority_rank, p.name))
    return selected


def _glob_matches(pattern: str, file_paths: Iterable[str]) -> bool:
    if pattern == "any":
        return False  # `any` is meaningless as a file glob; require explicit lang match
    return any(fnmatch(path, pattern) or fnmatch(Path(path).name, pattern) for path in file_paths)


def render_for_prompt(playbooks: Iterable[CodegenPlaybook]) -> str:
    """Format a list of playbooks for inclusion in a codegen system prompt."""
    sections: list[str] = []
    for playbook in playbooks:
        sections.append(f"## Playbook: {playbook.name}\n\n{playbook.body}\n")
    return "\n".join(sections)
