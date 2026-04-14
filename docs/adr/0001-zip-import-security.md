# ADR 0001: Zip Archive Import Security Policy

- Status: Accepted
- Date: 2026-04-14
- Supersedes: n/a
- Related: T-026-A (implementation)

## Context

Users with large knowledge bases need to upload hundreds of files efficiently; a zip archive endpoint reduces round trips and upload time compared to sequential single-file uploads. The product gain is measurable: bulk import enables migration workflows, knowledge base resets, and third-party data ingestion that would otherwise be prohibitive. Naive zip extraction is unsafe on a multi-tenant server because malicious archives can contain path-traversal entries that overwrite system files, zip bombs that exhaust memory or disk, and symlinks that redirect writes outside the intended directory. We are writing this ADR before implementation to establish a binding security contract that Codex must follow exactly, preventing security regressions in the zip import feature.

## Decision

The zip import endpoint MUST enforce every control below. Codex MUST implement all of them; reviewers MUST reject a PR missing any.

### 1. Path traversal prevention

- Every zip entry's resolved absolute path MUST start with the per-source extraction directory. Entries whose resolved path escapes the directory MUST abort the whole import with a 400 response.
- Resolution uses `os.path.realpath`, not just `os.path.join`, so symlinks inside already-extracted content cannot redirect later writes.

### 2. Size bounds (zip bomb)

- Total uncompressed size: 200 MB.
- Per-entry uncompressed size: 50 MB.
- Entry count: 2000.
- Any one limit exceeded aborts the import before writing to disk.

### 3. Compression-ratio bound

- Reject any entry whose `uncompressed / compressed` ratio exceeds 200. This catches highly recursive zip bombs that sneak past size bounds.

### 4. Symlink rejection

- Zip entries with external_attr indicating a symlink MUST be rejected. We do not support symlink entries at all; there is no legitimate case for one in a knowledge upload.

### 5. Filename normalization

- Reject entries with absolute paths, `..`, null bytes, backslashes on POSIX, or forward slashes on Windows. Normalize to NFC unicode.

### 6. MIME / extension whitelist

- Only extensions already accepted by `POST /api/knowledge/upload` may appear inside the archive. Binary/unknown types are skipped with a warning in the response; the archive itself is not rejected.

### 7. Permission gating

- Endpoint guarded by `knowledge:upload` (same as single-file upload).

### 8. Atomicity

- Extraction into a temporary directory first, then `os.replace` into the source directory on success. Partial archives never appear in the knowledge index.

### 9. Error reporting

- On abort, respond with HTTP 400 and a JSON body containing `reason` (one of: "path_traversal", "size_exceeded", "ratio_exceeded", "symlink", "invalid_name", "entry_count_exceeded") and the offending entry name (sanitized).

## Consequences

The enforcement of these nine controls adds processing overhead to each archive upload, requiring validation of every entry before extraction begins; this cost is acceptable given the multi-tenant safety guarantees. We explicitly choose not to defend against authenticity verification of archive contents, relying instead on the `knowledge:upload` permission gate to ensure only authorized users import data. The zip endpoint shares the same permission model, metadata handling, and storage layout as the existing `POST /api/knowledge/upload` endpoint, ensuring consistent behavior and avoiding divergent security properties. Operators should monitor for clients repeatedly submitting archives that trigger the size or ratio limits, as this may indicate an automated attack rather than legitimate bulk import.

## References

- CWE-22 Path Traversal
- CWE-409 Improper Handling of Highly Compressed Data (Zip Bomb)
- OWASP Unrestricted File Upload
