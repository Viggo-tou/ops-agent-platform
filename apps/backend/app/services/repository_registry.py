"""User-managed repository sources (uploads + git clones).

Persists to ``data/repositories/_registry.json`` so user-added sources
survive backend restart.

Sources have one of three origins:
- ``env``: configured via OPS_AGENT_KNOWLEDGE_SOURCE_SPECS (read-only here)
- ``upload``: zip uploaded via /api/repositories/upload, extracted to
  ``data/repositories/<slug>/``
- ``clone``: git URL cloned via /api/repositories/clone

The registry only tracks ``upload`` + ``clone`` origins; ``env`` rows are
merged at /api/repositories/sources read time.

Provider-agnostic. Pure data layer + filesystem; no orchestrator coupling.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.core.config import get_settings

_REGISTRY_DIRNAME = "repositories"
_REGISTRY_FILE = "_registry.json"
_LOCK = threading.Lock()


@dataclass
class SourceRecord:
    name: str
    path: str
    origin: str  # "upload" | "clone" | "env"
    description: str = ""
    git_url: str = ""
    added_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "origin": self.origin,
            "description": self.description,
            "git_url": self.git_url,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SourceRecord":
        return cls(
            name=str(data.get("name", "")),
            path=str(data.get("path", "")),
            origin=str(data.get("origin", "upload")),
            description=str(data.get("description", "") or ""),
            git_url=str(data.get("git_url", "") or ""),
            added_at=str(data.get("added_at", "") or ""),
        )


def _data_root() -> Path:
    """Return the repositories dir (creates if needed)."""
    settings = get_settings()
    backend_dir = Path(__file__).resolve().parents[2]  # apps/backend
    base = getattr(settings, "data_dir", None)
    if base:
        root = Path(base) / _REGISTRY_DIRNAME
    else:
        root = backend_dir / "data" / _REGISTRY_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def _registry_path() -> Path:
    return _data_root() / _REGISTRY_FILE


def _slugify(name: str) -> str:
    """Convert arbitrary user input into a safe directory name."""
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    if not slug:
        raise ValueError("name must contain at least one alphanumeric character")
    if len(slug) > 64:
        slug = slug[:64].rstrip("-")
    return slug


def _read_registry() -> list[SourceRecord]:
    path = _registry_path()
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        return []
    return [SourceRecord.from_dict(item) for item in sources if isinstance(item, dict)]


def _write_registry(records: list[SourceRecord]) -> None:
    path = _registry_path()
    payload = {"sources": [r.to_dict() for r in records]}
    # Atomic write: tmp file + rename.
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def list_managed_sources() -> list[SourceRecord]:
    """Return sources from registry only (upload + clone)."""
    with _LOCK:
        return _read_registry()


def add_managed_source(record: SourceRecord) -> None:
    """Persist a new upload/clone record. Overwrites if name exists."""
    with _LOCK:
        records = _read_registry()
        records = [r for r in records if r.name != record.name]
        records.append(record)
        _write_registry(records)


def remove_managed_source(name: str) -> bool:
    """Remove from registry AND delete on-disk dir. Returns True if removed."""
    with _LOCK:
        records = _read_registry()
        match = next((r for r in records if r.name == name), None)
        if match is None:
            return False
        records = [r for r in records if r.name != name]
        _write_registry(records)
    # Best-effort filesystem delete (ignore errors).
    try:
        target = Path(match.path)
        if target.is_dir() and _data_root() in target.parents:
            shutil.rmtree(target, ignore_errors=True)
    except (OSError, ValueError):
        pass
    return True


def resolve_path_by_name(name: str) -> str | None:
    """Look up the on-disk path for a source by name.

    Used by orchestrator when task.source_name is set. Searches managed
    registry first, then env-configured specs (so env names also work as
    overrides). Returns None when not found — caller falls back to the
    pre-existing resolution logic.
    """
    if not name:
        return None
    for record in list_managed_sources():
        if record.name == name:
            return record.path
    # Env specs lookup (parse same format as repositories API).
    settings = get_settings()
    raw = (getattr(settings, "knowledge_source_specs", None) or "").strip()
    for entry in raw.split(";"):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        env_name, rest = entry.split("=", 1)
        if env_name.strip() != name:
            continue
        path = rest.split("|", 1)[0].strip() if rest else ""
        return path or None
    # Single-source fallback.
    if name == (getattr(settings, "knowledge_source_name", "") or ""):
        return getattr(settings, "knowledge_source_path", None) or None
    return None


# ----------------------------------------------------------------------
# Upload (zip)
# ----------------------------------------------------------------------

# Hard caps to defend against zip bombs / accidental huge archives.
_UPLOAD_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MB extracted
_UPLOAD_MAX_ENTRIES = 5_000


class RegistryError(Exception):
    pass


def _safe_extract_zip(zip_bytes: bytes, dest: Path) -> dict[str, object]:
    """Validate + extract a zip into ``dest``. Raises RegistryError on hostile zips.

    Defenses:
    - reject if total uncompressed size > _UPLOAD_MAX_TOTAL_BYTES
    - reject if entry count > _UPLOAD_MAX_ENTRIES
    - reject path-traversal entries (starting with /, containing ..)
    """
    with zipfile.ZipFile(io_bytes_buffer(zip_bytes)) as zf:
        infos = zf.infolist()
        if len(infos) > _UPLOAD_MAX_ENTRIES:
            raise RegistryError(
                f"zip has {len(infos)} entries (max {_UPLOAD_MAX_ENTRIES})"
            )
        total = sum(info.file_size for info in infos)
        if total > _UPLOAD_MAX_TOTAL_BYTES:
            raise RegistryError(
                f"zip uncompressed size {total} bytes exceeds "
                f"{_UPLOAD_MAX_TOTAL_BYTES} bytes"
            )
        for info in infos:
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/"):
                raise RegistryError(f"unsafe entry path: {info.filename}")
        dest.mkdir(parents=True, exist_ok=True)
        # Strip a single common top-level dir if every entry shares it.
        top = _common_top_dir(infos)
        for info in infos:
            if info.is_dir():
                continue
            target_rel = info.filename.replace("\\", "/")
            if top:
                if not target_rel.startswith(top + "/"):
                    continue
                target_rel = target_rel[len(top) + 1:]
            if not target_rel:
                continue
            out_path = dest / target_rel
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, out_path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
    return {"extracted_files": len([i for i in infos if not i.is_dir()])}


def _common_top_dir(infos: list[zipfile.ZipInfo]) -> str | None:
    """If every non-empty entry is nested under one common top dir, return it.

    Returns None when:
    - any entry sits at zip root (no '/' in path) — would be wrong to strip
      a flat file's name as if it were a directory
    - multiple distinct top-level dirs exist
    - zip is empty
    """
    tops: set[str] = set()
    saw_any = False
    for info in infos:
        name = info.filename.replace("\\", "/").rstrip("/")
        if not name:
            continue
        saw_any = True
        if "/" not in name:
            # Flat file at root — no common top dir to strip.
            return None
        head = name.split("/", 1)[0]
        tops.add(head)
        if len(tops) > 1:
            return None
    if not saw_any:
        return None
    return tops.pop() if len(tops) == 1 else None


def io_bytes_buffer(data: bytes):
    """Tiny indirection so tests can mock if needed."""
    import io
    return io.BytesIO(data)


def upload_zip_source(*, name: str, description: str, zip_bytes: bytes) -> SourceRecord:
    """Extract a zip into data/repositories/<slug>/ and register."""
    slug = _slugify(name)
    target = _data_root() / slug
    if target.exists():
        # Overwrite cleanly.
        shutil.rmtree(target)
    _safe_extract_zip(zip_bytes, target)
    record = SourceRecord(
        name=slug,
        path=str(target.resolve()),
        origin="upload",
        description=description.strip(),
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    add_managed_source(record)
    return record


# ----------------------------------------------------------------------
# Clone (git)
# ----------------------------------------------------------------------

_CLONE_TIMEOUT_SECONDS = 60.0
_CLONE_MAX_BYTES = 200 * 1024 * 1024  # post-clone size cap (advisory)


def _validate_clone_url(url: str) -> None:
    """Public HTTPS only for 1.0. Reject ssh / unknown schemes."""
    url = url.strip()
    if not url:
        raise RegistryError("git URL is required")
    if not url.startswith(("https://", "http://")):
        raise RegistryError(
            "1.0 supports public HTTPS only (no ssh/git protocols). "
            "Private repos via GitHub OAuth ship in 1.1."
        )
    if len(url) > 1024:
        raise RegistryError("git URL too long")


def clone_git_source(*, name: str, description: str, git_url: str) -> SourceRecord:
    """Run git clone <url> into data/repositories/<slug>/ and register."""
    slug = _slugify(name)
    _validate_clone_url(git_url)
    target = _data_root() / slug
    if target.exists():
        shutil.rmtree(target)
    git = shutil.which("git")
    if not git:
        raise RegistryError("git is not installed on the server")

    cmd = [git, "clone", "--depth=1", "--single-branch", git_url, str(target)]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_CLONE_TIMEOUT_SECONDS,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        # Best-effort cleanup if partial clone exists.
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        raise RegistryError(
            f"git clone exceeded {_CLONE_TIMEOUT_SECONDS}s; aborted"
        )
    if proc.returncode != 0:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        stderr = (proc.stderr or "").strip()[-500:]
        raise RegistryError(f"git clone failed (exit {proc.returncode}): {stderr}")

    # Soft size check (advisory only — clone itself succeeded).
    size = sum(p.stat().st_size for p in target.rglob("*") if p.is_file())
    if size > _CLONE_MAX_BYTES:
        # Don't reject, just log — already on disk.
        pass

    record = SourceRecord(
        name=slug,
        path=str(target.resolve()),
        origin="clone",
        description=description.strip(),
        git_url=git_url,
        added_at=datetime.now(timezone.utc).isoformat(),
    )
    add_managed_source(record)
    return record


def list_all_sources_for_api() -> list[dict[str, object]]:
    """Combine env-configured specs + managed registry for /api/repositories/sources.

    Env entries always show first (operator-configured baseline), then
    registry entries in added-order. Ordering is stable for UI rendering.
    """
    settings = get_settings()
    rows: list[dict[str, object]] = []
    seen_names: set[str] = set()

    # Env-configured specs.
    raw_specs = (getattr(settings, "knowledge_source_specs", None) or "").strip()
    if raw_specs:
        for entry in raw_specs.split(";"):
            entry = entry.strip()
            if not entry or "=" not in entry:
                continue
            name, rest = entry.split("=", 1)
            path, desc = (rest.split("|", 1) + [""])[:2]
            name = name.strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            rows.append(
                {
                    "name": name,
                    "path": path.strip(),
                    "description": desc.strip(),
                    "origin": "env",
                    "git_url": "",
                    "added_at": "",
                }
            )
    elif getattr(settings, "knowledge_source_path", None):
        # Single-source fallback.
        single_name = (getattr(settings, "knowledge_source_name", "") or "default").strip()
        rows.append(
            {
                "name": single_name,
                "path": str(getattr(settings, "knowledge_source_path", "") or ""),
                "description": "",
                "origin": "env",
                "git_url": "",
                "added_at": "",
            }
        )
        seen_names.add(single_name)

    # Managed sources.
    for r in list_managed_sources():
        if r.name in seen_names:
            # Same name from env wins for lookup; skip to avoid dupes.
            continue
        rows.append(r.to_dict())
        seen_names.add(r.name)

    return rows
