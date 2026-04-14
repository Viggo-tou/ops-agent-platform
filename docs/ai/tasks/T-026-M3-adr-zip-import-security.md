# T-026-M3 — ADR 0001: Zip Import Security Policy (MiniMax)

## Owner

MiniMax (expands an outline into prose; no judgment calls).

## Goal

Produce an Architecture Decision Record (ADR) that encodes the security policy for the upcoming `POST /api/knowledge/upload-archive` endpoint. This ADR will be used by Codex (post-2026-04-17) as the binding spec when it implements the zip import feature (T-026-A).

## Output

File: `docs/adr/0001-zip-import-security.md`

## Format (must follow exactly)

Use this skeleton. Fill the `Context`, `Consequences`, and body — do not change the section order or names.

```markdown
# ADR 0001: Zip Archive Import Security Policy

- Status: Accepted
- Date: 2026-04-14
- Supersedes: n/a
- Related: T-026-A (implementation)

## Context

<3–5 sentences: why we need zip import, what the product gain is, why
naive unzip is unsafe in a multi-tenant server, and why we are writing
this ADR before implementation.>

## Decision

The zip import endpoint MUST enforce every control below. Codex MUST
implement all of them; reviewers MUST reject a PR missing any.

### 1. Path traversal prevention

- Every zip entry's resolved absolute path MUST start with the per-source
  extraction directory. Entries whose resolved path escapes the directory
  MUST abort the whole import with a 400 response.
- Resolution uses `os.path.realpath`, not just `os.path.join`, so symlinks
  inside already-extracted content cannot redirect later writes.

### 2. Size bounds (zip bomb)

- Total uncompressed size: 200 MB.
- Per-entry uncompressed size: 50 MB.
- Entry count: 2000.
- Any one limit exceeded aborts the import before writing to disk.

### 3. Compression-ratio bound

- Reject any entry whose `uncompressed / compressed` ratio exceeds 200.
  This catches highly recursive zip bombs that sneak past size bounds.

### 4. Symlink rejection

- Zip entries with external_attr indicating a symlink MUST be rejected.
  We do not support symlink entries at all; there is no legitimate case
  for one in a knowledge upload.

### 5. Filename normalization

- Reject entries with absolute paths, `..`, null bytes, backslashes on
  POSIX, or forward slashes on Windows. Normalize to NFC unicode.

### 6. MIME / extension whitelist

- Only extensions already accepted by `POST /api/knowledge/upload` may
  appear inside the archive. Binary/unknown types are skipped with a
  warning in the response; the archive itself is not rejected.

### 7. Permission gating

- Endpoint guarded by `knowledge:upload` (same as single-file upload).

### 8. Atomicity

- Extraction into a temporary directory first, then `os.replace` into the
  source directory on success. Partial archives never appear in the
  knowledge index.

### 9. Error reporting

- On abort, respond with HTTP 400 and a JSON body containing `reason`
  (one of: "path_traversal", "size_exceeded", "ratio_exceeded", "symlink",
  "invalid_name", "entry_count_exceeded") and the offending entry name
  (sanitized).

## Consequences

<3–5 sentences: operational impact, cost of enforcement, what we're
explicitly choosing not to defend against (e.g., out-of-scope
authenticity verification), and how this interacts with the existing
knowledge.upload endpoint.>

## References

- CWE-22 Path Traversal
- CWE-409 Improper Handling of Highly Compressed Data (Zip Bomb)
- OWASP Unrestricted File Upload
```

## Constraints

- 300–500 words total across all prose sections.
- No emojis, no decorative text.
- Do not invent additional controls beyond the 9 above.
- Do not propose implementation code — this is a policy doc only.

## Acceptance

- File exists at `docs/adr/0001-zip-import-security.md`.
- All 9 numbered controls present and phrased as MUST.
- Context and Consequences sections are filled (not placeholders).
- Markdown lints cleanly.
