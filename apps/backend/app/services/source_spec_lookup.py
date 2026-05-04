"""Helper to look up source-name -> filesystem-path from knowledge_source_specs.

Mirrors the parsing logic in KnowledgeService._resolve_source_specs but
without the SQLAlchemy / KnowledgeService dependency, so the orchestrator
can call it without instantiating a full KnowledgeService.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def lookup_source_path(source_name: str, settings: Any) -> str | None:
    """Return absolute path string for ``source_name`` if configured.

    Parses the ``knowledge_source_specs`` env (semicolon-separated entries
    of form ``name=path[|description]``), case-insensitive on name.
    Returns None when:
      - settings has no knowledge_source_specs
      - source_name is empty / not in specs
      - the parsed path does not exist on disk
    """
    if not source_name or not source_name.strip():
        return None
    raw = getattr(settings, "knowledge_source_specs", None)
    if not raw:
        return None
    target = source_name.strip().lower()
    for entry in str(raw).split(";"):
        item = entry.strip()
        if not item or "=" not in item:
            continue
        name, path_with_desc = item.split("=", 1)
        if name.strip().lower() != target:
            continue
        path_str = path_with_desc.split("|", 1)[0].strip() if "|" in path_with_desc else path_with_desc.strip()
        if not path_str:
            continue
        path = Path(path_str)
        if path.exists():
            return str(path)
        return None
    return None
