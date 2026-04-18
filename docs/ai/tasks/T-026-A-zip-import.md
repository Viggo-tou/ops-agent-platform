# T-026-A: Knowledge Zip Archive Import Endpoint

Owner: Codex
Model effort: `-c model_reasoning_effort=medium`
Prereqs: ADR `docs/adr/0001-zip-import-security.md` (9 MUST controls, binding).
Mirror target: existing `POST /api/knowledge/upload` in `apps/backend/app/api/knowledge.py:63`.

## Goal

Add `POST /api/knowledge/upload-zip` that accepts a single `UploadFile` zip archive, extracts each entry under the ADR 0001 security controls, and ingests the surviving files via the same `KnowledgeService.upload_documents` code path as the per-file endpoint.

## Deliverables

1. New route `POST /api/knowledge/upload-zip` in `apps/backend/app/api/knowledge.py`.
   - Form fields: `archive: UploadFile` (required), `source_name: str | None = Form(default=None)`.
   - Guarded by `KnowledgeUploadActorCtx` (same permission as single-file upload).
   - Response: reuse `KnowledgeUploadResponse`; on ADR violation → `HTTPException(400)` with body
     `{"detail": {"reason": "<code>", "entry": "<sanitized name>"}}`
     where `<code>` ∈ `path_traversal | size_exceeded | ratio_exceeded | symlink | invalid_name | entry_count_exceeded`.

2. New module `apps/backend/app/services/knowledge_zip.py`:
   - Function `extract_zip_safely(archive_bytes: bytes) -> list[tuple[str, bytes]]`.
   - Enforces every ADR 0001 control. Raises a local `ZipImportError(reason, entry)` exception on violation.
   - Streams into a `tempfile.TemporaryDirectory`; uses `os.path.realpath` for traversal check; atomic handoff via in-memory list (no persistent tempdir leaks).
   - Extension whitelist MUST be imported from the same constant used by `upload_documents` (dedupe — do not hard-code a second list). If no such constant exists today, create one in `apps/backend/app/services/knowledge.py` and import from there.
   - Symlink detection via `zipinfo.external_attr >> 16 & 0o170000 == 0o120000`.
   - Compression-ratio check only for entries where `compress_size > 0`.

3. Tests under `apps/backend/tests/api/test_knowledge_zip_import.py`:
   - Happy path: 3-entry zip (txt + md + pdf mimic) → 200, returns 3 uploaded.
   - Path traversal: entry `../../etc/passwd` → 400 reason `path_traversal`.
   - Entry count > 2000 → 400 reason `entry_count_exceeded`.
   - Per-entry size > 50 MB (use a mocked large entry, not a real 50 MB blob) → 400 reason `size_exceeded`.
   - Total uncompressed > 200 MB → 400 reason `size_exceeded`.
   - Compression ratio > 200 → 400 reason `ratio_exceeded`.
   - Symlink entry → 400 reason `symlink`.
   - Absolute path / `..` / null byte in name → 400 reason `invalid_name`.
   - Unknown extension inside archive → skipped with warning, archive NOT rejected.
   - RBAC: member role with `knowledge:upload` passes; viewer role denied (403).

4. RBAC fixture update: append the new endpoint row to `apps/backend/tests/fixtures/rbac_expected_matrix.json` for all 4 roles, matching the expectations of `/api/knowledge/upload`.

## Non-goals

- No zip export.
- No nested-zip recursion (reject nested `.zip` entries as unknown extension → skip with warning).
- No UI changes (frontend binding is a separate task).

## Acceptance (gate before merge)

- `pytest apps/backend/tests/api/test_knowledge_zip_import.py` — all new tests green.
- `pytest apps/backend/tests/` — still 129+ passed, 0 failed.
- `scripts/verify-rbac.ps1` — still 88/88 pass (plus new endpoint row if script auto-derives, else update expected count).
- Diff review: every one of the 9 ADR controls has a visible code + test pair. Any missing control = reject.
- No extension list duplication between single-file and zip paths (enforced by grep).

## Out-of-scope / defer

- Background/async extraction (archive stays request-scoped; 200 MB cap keeps this tractable).
- Progress streaming.
- Per-file MIME sniffing beyond extension (ADR control 6 is extension-only).
