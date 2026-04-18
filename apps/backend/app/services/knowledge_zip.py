"""T-026-A: Safe zip archive extraction for knowledge bulk import.

Binding contract: docs/adr/0001-zip-import-security.md (9 MUST controls).
Every control below maps to a named function or branch so a future audit can
grep each ADR section number and find its enforcement site.
"""

from __future__ import annotations

import io
import os
import tempfile
import unicodedata
import zipfile

from app.services.knowledge import UPLOAD_ACCEPTED_EXTENSIONS

# ADR 0001 §2: size bounds.
MAX_TOTAL_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_PER_ENTRY_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_ENTRY_COUNT = 2000

# ADR 0001 §3: compression-ratio bound.
MAX_COMPRESSION_RATIO = 200

# ADR 0001 §4: unix mode bits that mark a symlink entry.
_SYMLINK_MODE = 0o120000
_FILE_TYPE_MASK = 0o170000

_FORBIDDEN_NAME_CHARS = ("\x00",)


class ZipImportError(Exception):
    """Raised when an archive violates an ADR 0001 control.

    reason is one of the ADR §9 codes; entry is the sanitized offending name.
    """

    def __init__(self, *, reason: str, entry: str) -> None:
        super().__init__(f"{reason}: {entry}")
        self.reason = reason
        self.entry = entry


def _sanitize_for_error(name: str) -> str:
    cleaned = name.replace("\x00", "?")
    return cleaned[:200]


def _validate_name(raw_name: str) -> None:
    """ADR 0001 §5 — filename normalization and structural rejection."""

    if not raw_name:
        raise ZipImportError(reason="invalid_name", entry=_sanitize_for_error(raw_name))

    for bad in _FORBIDDEN_NAME_CHARS:
        if bad in raw_name:
            raise ZipImportError(reason="invalid_name", entry=_sanitize_for_error(raw_name))

    if "\\" in raw_name:
        raise ZipImportError(reason="invalid_name", entry=_sanitize_for_error(raw_name))

    normalized = unicodedata.normalize("NFC", raw_name)

    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ZipImportError(reason="invalid_name", entry=_sanitize_for_error(raw_name))

    parts = normalized.split("/")
    for part in parts:
        if part in ("..",):
            raise ZipImportError(reason="invalid_name", entry=_sanitize_for_error(raw_name))


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """ADR 0001 §4."""
    mode = (info.external_attr >> 16) & _FILE_TYPE_MASK
    return mode == _SYMLINK_MODE


def _assert_within_root(candidate_path: str, root_path: str, raw_name: str) -> None:
    """ADR 0001 §1 — resolved absolute path must stay inside the extraction root."""
    resolved = os.path.realpath(candidate_path)
    resolved_root = os.path.realpath(root_path)
    common = os.path.commonpath([resolved, resolved_root])
    if common != resolved_root:
        raise ZipImportError(reason="path_traversal", entry=_sanitize_for_error(raw_name))


def extract_zip_safely(archive_bytes: bytes) -> list[tuple[str, bytes]]:
    """Decode an archive into (filename, content) pairs, enforcing ADR 0001.

    Returns only entries whose extension is in UPLOAD_ACCEPTED_EXTENSIONS.
    Unknown-extension entries are silently skipped (ADR §6); any other
    violation aborts with ZipImportError so the endpoint returns HTTP 400.
    """

    try:
        zf = zipfile.ZipFile(io.BytesIO(archive_bytes))
    except zipfile.BadZipFile:
        raise ZipImportError(reason="invalid_name", entry="<archive>") from None

    infos = zf.infolist()

    if len(infos) > MAX_ENTRY_COUNT:
        raise ZipImportError(
            reason="entry_count_exceeded",
            entry=_sanitize_for_error(f"<{len(infos)} entries>"),
        )

    # First pass: cheap header-level validation. We reject before writing
    # anything to disk so a malicious archive never reaches the extract root.
    total_declared = 0
    for info in infos:
        raw_name = info.filename

        if info.is_dir():
            continue

        if _is_symlink(info):
            raise ZipImportError(reason="symlink", entry=_sanitize_for_error(raw_name))

        _validate_name(raw_name)

        if info.file_size > MAX_PER_ENTRY_UNCOMPRESSED_BYTES:
            raise ZipImportError(reason="size_exceeded", entry=_sanitize_for_error(raw_name))

        if info.compress_size > 0:
            ratio = info.file_size / info.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise ZipImportError(
                    reason="ratio_exceeded", entry=_sanitize_for_error(raw_name)
                )

        total_declared += info.file_size
        if total_declared > MAX_TOTAL_UNCOMPRESSED_BYTES:
            raise ZipImportError(reason="size_exceeded", entry=_sanitize_for_error(raw_name))

    # Second pass: ADR §1 (traversal via realpath) + ADR §6 (extension
    # whitelist) + ADR §8 (atomic via TemporaryDirectory — we only hand back
    # the collected bytes if every entry survives).
    results: list[tuple[str, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="knowledge-zip-") as tmp_dir:
        for info in infos:
            if info.is_dir():
                continue

            raw_name = info.filename
            base_name = os.path.basename(raw_name)
            if not base_name:
                continue

            candidate = os.path.join(tmp_dir, raw_name)
            _assert_within_root(candidate, tmp_dir, raw_name)

            _, ext = os.path.splitext(base_name)
            if ext.lower() not in UPLOAD_ACCEPTED_EXTENSIONS:
                continue

            with zf.open(info, "r") as src:
                data = src.read(MAX_PER_ENTRY_UNCOMPRESSED_BYTES + 1)
            if len(data) > MAX_PER_ENTRY_UNCOMPRESSED_BYTES:
                raise ZipImportError(reason="size_exceeded", entry=_sanitize_for_error(raw_name))

            results.append((base_name, data))

    return results
