# T-026-A Pre-Review Checklist

Purpose: when Codex returns a diff for `T-026-A-zip-import.md`, walk this list top-to-bottom. Any unchecked row = reject and return to Codex with the row id.

Baseline: `session-baseline/2026-04-14-T037`.
Spec: `docs/ai/tasks/T-026-A-zip-import.md`.
ADR: `docs/adr/0001-zip-import-security.md`.

## Structural (fast-fail — check first)

| # | Check | Command / Grep |
|---|---|---|
| S1 | New route exists | `grep -n 'upload-zip' apps/backend/app/api/knowledge.py` |
| S2 | New service module exists | `ls apps/backend/app/services/knowledge_zip.py` |
| S3 | New test file exists | `ls apps/backend/tests/api/test_knowledge_zip_import.py` |
| S4 | RBAC fixture has new endpoint row | `grep upload-zip apps/backend/tests/fixtures/rbac_expected_matrix.json` |
| S5 | No new dependency in `pyproject.toml` (stdlib `zipfile` only) | `git diff session-baseline/2026-04-14-T037..HEAD -- pyproject.toml` → empty |

## ADR 0001 — each control has code + test

| ADR § | Control | Code grep | Test grep |
|---|---|---|---|
| 1 | Path traversal via `realpath` | `realpath` in `knowledge_zip.py` | `test_path_traversal` with `../../etc/passwd` |
| 2a | Total uncompressed ≤ 200 MB | `200 * 1024 * 1024` or named const | `test_total_size_exceeded` |
| 2b | Per-entry ≤ 50 MB | `50 * 1024 * 1024` or named const | `test_entry_size_exceeded` |
| 2c | Entry count ≤ 2000 | literal `2000` or const | `test_entry_count_exceeded` |
| 3 | Ratio > 200 reject | `ratio` / `compress_size` branch | `test_ratio_exceeded` |
| 4 | Symlink reject | `external_attr` + `0o120000` | `test_symlink_rejected` |
| 5 | Invalid name (abs, `..`, null) | name validation function | `test_invalid_name` (≥ 3 cases) |
| 6 | Extension whitelist reused | import from `services/knowledge.py`, NOT redefined | `test_unknown_ext_skipped_not_rejected` |
| 7 | Permission gate `knowledge:upload` | `KnowledgeUploadActorCtx` on route | `test_rbac_viewer_denied` + `test_rbac_member_allowed` |
| 8 | Atomic extraction | `tempfile.TemporaryDirectory` + `os.replace` OR in-memory list | inspection only (no dedicated test required) |
| 9 | Structured 400 body | `HTTPException(detail={"reason": ..., "entry": ...})` | every failure test asserts `detail['reason']` |

## De-duplication

| D1 | Extension whitelist NOT duplicated | `grep -rn "\.txt.*\.md.*\.pdf" apps/backend/app/` returns exactly one definition site |

## Test suite gates

| T1 | New tests green | `pytest apps/backend/tests/api/test_knowledge_zip_import.py -v` — all green |
| T2 | No regression | `pytest apps/backend/tests/` — ≥ 129 passed, 0 failed |
| T3 | RBAC smoke | `powershell -File scripts/verify-rbac.ps1` — 92/92 (88 prior + 4 new roles × 1 endpoint) |

## Review etiquette

- Do not suggest cosmetic changes. Only block on Structural / ADR / Test rows.
- If Codex diff touches files outside `{app/api/knowledge.py, services/knowledge_zip.py, services/knowledge.py (whitelist extraction only), tests/api/test_knowledge_zip_import.py, tests/fixtures/rbac_expected_matrix.json}` → reject as scope creep.
- On approve: merge, tag `t026-a-merged`, update `TASK_QUEUE.md` row to `done`, append manifest to `SESSION_HANDOFF.md`.
