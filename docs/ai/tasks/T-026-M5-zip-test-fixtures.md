# T-026-M5: Zip Test Fixture Builder (MiniMax)

Owner: MiniMax
Purpose: reusable helper for `T-026-A-zip-import.md` tests. Produces in-memory zip bytes with each ADR 0001 violation pattern so Codex's test file can be declarative instead of re-building zips by hand.

## Deliverable

One new file: `apps/backend/tests/fixtures/zip_archives.py`.

### Public API (exactly these names)

```python
def clean_archive(entries: list[tuple[str, bytes]]) -> bytes: ...
def archive_with_path_traversal() -> bytes: ...
def archive_with_symlink() -> bytes: ...
def archive_with_entry_count(n: int) -> bytes: ...
def archive_with_oversized_entry(size_bytes: int) -> bytes: ...
def archive_with_bomb_ratio(ratio: int) -> bytes: ...
def archive_with_invalid_name(name: str) -> bytes: ...
def archive_with_unknown_extension() -> bytes: ...
```

Each returns raw `bytes` suitable for passing to `UploadFile` in a TestClient request.

### Constraints

- stdlib `zipfile` only. No new dependencies.
- `archive_with_oversized_entry(50 * 1024 * 1024 + 1)` MUST NOT allocate a real 50MB payload. Use `zipfile.ZipInfo` + `file_size` header manipulation so the advertised `file_size` exceeds the cap while the actual stored bytes stay small. Document the technique in a one-line comment.
- `archive_with_bomb_ratio(300)` similarly avoids real allocation: write ≤ 1 KiB of compressed bytes with an advertised `file_size` 300× larger.
- `archive_with_symlink` sets `external_attr = 0o120000 << 16` on a ZipInfo entry.
- `clean_archive` is the baseline happy-path helper; default caller uses `[("doc1.md", b"hello"), ("doc2.txt", b"world")]`.

### Test for the helpers themselves

Add `apps/backend/tests/fixtures/test_zip_archives.py`:

- Each helper returns non-empty bytes.
- `zipfile.is_zipfile(io.BytesIO(result))` is True for every helper.
- `archive_with_symlink()` → the single entry has `external_attr >> 16 & 0o170000 == 0o120000`.
- `archive_with_entry_count(5)` → exactly 5 entries.
- `archive_with_oversized_entry(1_000_000)` → the one entry's `file_size` attribute ≥ 1_000_000.

## Acceptance

- `pytest apps/backend/tests/fixtures/test_zip_archives.py` — all green.
- `pytest apps/backend/tests/` — still 129+ passed, 0 failed (no collateral).
- No changes outside `apps/backend/tests/fixtures/`.
- File has a 3–5 line module docstring pointing at ADR 0001 and T-026-A.

## Out of scope

- Do not touch `services/knowledge.py` or any API module.
- Do not pre-create zip files on disk. Everything in-memory.
- Do not add a pytest fixture (`@pytest.fixture`); these are plain functions Codex will call directly.
