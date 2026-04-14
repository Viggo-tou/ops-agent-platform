# T-026-05 — End-to-end RBAC Smoke (admin / operator / member / viewer)

## Goal

Produce a reproducible, checked-in Python smoke script that exercises the four frontend roles against the live backend and asserts the expected HTTP outcome for each mutation. This is the closing verification for T-026 — after this task, the workbench persistence + governance integration slice is considered complete.

Out of scope: UI-level verification (we cannot drive a browser in this environment). The script covers the backend surface only. UI verification will happen manually by the user after this lands.

## Background

- Permission map lives in two places and must agree:
  - `apps/backend/app/core/security.py::PERMISSION_MAP` (canonical, enforces at request time)
  - `apps/web/src/lib/auth.tsx::rolePermissions` (used by the `can()` gate)
- The 4 frontend roles: `admin`, `operator`, `member`, `viewer`.
- The backend expects two headers on mutating requests: `X-Actor-Role` (backend `ActorRole` value) and `X-Actor-App-Role` (frontend `AppRole` value). The script uses the same mapping the frontend uses (`apps/web/src/lib/auth.tsx::toBackendActorRole`):
  - `admin → admin / admin`
  - `operator → team_lead / operator`
  - `member → employee / member`
  - `viewer → employee / viewer`

## What to build

Create a single file: `scripts/smoke/rbac_roles.py`. No new modules, no dependencies beyond the Python stdlib (`urllib.request` is fine) and whatever is already in `apps/backend/requirements.txt`. Prefer `httpx` only if it is already a backend dep.

Structure:

```python
# scripts/smoke/rbac_roles.py
"""
T-026-05 end-to-end RBAC smoke.

Runs a fixed matrix of (role, endpoint) checks against a locally running
backend and asserts the expected HTTP status. Exits 0 on full PASS, non-zero
otherwise. Prints a per-case line and a final summary.

Usage:
    python scripts/smoke/rbac_roles.py --base-url http://127.0.0.1:8000

The script assumes the backend has been started separately (via
`scripts/start-backend.ps1`) and that the model catalog / governance seeds
have run at least once (they run on lifespan startup).
"""
```

Hard requirements:

1. **Role header pairs** — constant `ROLE_HEADERS` dict keyed by the 4 frontend roles, mapping to `{"X-Actor-Role": ..., "X-Actor-App-Role": ...}` dicts, plus one extra entry `"anonymous"` with empty headers for the "no header at all" baseline.

2. **Matrix cases** — list of `Case` dataclass instances with fields `name`, `method`, `path`, `role`, `expected_status`, `body` (optional dict → JSON). The matrix must cover at minimum:

   | # | Endpoint                                         | Method | admin | operator | member | viewer | anonymous |
   |---|--------------------------------------------------|--------|-------|----------|--------|--------|-----------|
   | 1 | `/api/memory/items`                              | POST   | 201   | 201      | 201    | 403    | 401       |
   | 2 | `/api/memory/settings`                           | PATCH  | 200   | 200      | 403    | 403    | 401       |
   | 3 | `/api/knowledge/documents/<fake-id>`             | DELETE | 404   | 403      | 403    | 403    | 401       |
   | 4 | `/api/knowledge/sync`                            | POST   | 200   | 200      | 403    | 403    | 401       |
   | 5 | `/api/model-config/selected`                     | PATCH  | 200   | 200      | 403    | 403    | 401       |
   | 6 | `/api/approvals/<fake-id>/grant`                 | POST   | 404   | 404      | 403    | 403    | 401       |
   | 7 | `/api/memory/items` (read)                       | GET    | 200   | 200      | 200    | 200    | 200       |
   | 8 | `/api/model-config/providers` (read)             | GET    | 200   | 200      | 200    | 200    | 200       |

   Row 2 expects `member` → 403 because `member` has `memory:edit` for item CRUD but the memory **settings** endpoint requires the same permission — members can actually edit memory. Double-check against `PERMISSION_MAP`: `member` has `memory:edit`, so row 2 should be `member → 200`. **Adjust the matrix if `rolePermissions` says otherwise.**

   For row 4 (`/api/knowledge/sync`): sync requires `knowledge:upload`. Per `rolePermissions`, `member` does NOT have `knowledge:upload`, so `member → 403` is correct.

   For row 3, item 1 creation and cleanup: after the matrix, delete the memory item created in row 1 using an admin header to clean up. Not part of the matrix; explicit teardown.

   Row 5: the `model_id` in the PATCH body should be `"claude-opus-4-6"` (seeded). If that ID is missing, the script should fail loudly with a clear error telling the user to seed the model catalog.

3. **Idempotency** — because rows 1 and 2 can mutate DB state, generate a unique `topic` or `title` suffix via `uuid.uuid4().hex[:8]` to avoid accidental collisions with prior runs. Delete created items in teardown.

4. **Output format** — one line per case: `[PASS|FAIL] #<n> <method> <path> as <role> -> <got> (expected <expected>)`. A summary at the end with `PASSED: n/N` and an exit code.

5. **Save the transcript** — when the script exits, have the caller (not the script itself) pipe output to `docs/ai/runs/T-026-05.log`. Keep the script simple — it just writes to stdout.

## Files to create

1. `scripts/smoke/rbac_roles.py` — the smoke script described above. Use only stdlib (`urllib.request`, `json`, `argparse`, `dataclasses`, `uuid`, `sys`).

## Files to edit

None. The backend and frontend are already in place.

## Acceptance criteria

- `python scripts/smoke/rbac_roles.py --base-url http://127.0.0.1:<port>` runs to completion against a live backend.
- All 8 matrix rows × 5 roles = 40 cases report `PASS`.
- Final summary line: `PASSED: 40/40` and the process exits 0.
- A copy of the transcript is saved at `docs/ai/runs/T-026-05.log` (you can redirect while running).
- Re-running the script on the same backend without restart still produces `PASSED: 40/40` (teardown works).

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/core/security.py::PERMISSION_MAP`, `apps/web/src/lib/auth.tsx::rolePermissions`, and `apps/web/src/lib/auth.tsx::toBackendActorRole`. Reconcile any drift against the matrix in this spec. If there's a conflict, trust `PERMISSION_MAP` and adjust the matrix — log the adjustment in the smoke transcript header.
2. Read `apps/backend/app/api/memory.py`, `apps/backend/app/api/knowledge.py`, `apps/backend/app/api/model_config.py`, `apps/backend/app/api/approvals.py`. Confirm exact paths and HTTP methods before encoding them in the matrix.
3. Write `scripts/smoke/rbac_roles.py`. Stdlib only. No type-checking configuration needed — it's a script.
4. Start the backend on a free port (port 8000 may be held). Run the script twice:
   ```
   python scripts/smoke/rbac_roles.py --base-url http://127.0.0.1:<port> > docs/ai/runs/T-026-05.log
   python scripts/smoke/rbac_roles.py --base-url http://127.0.0.1:<port> >> docs/ai/runs/T-026-05.log
   ```
   The second run proves idempotency.
5. Ensure the log ends with two `PASSED: 40/40` lines. If anything fails, inspect, fix the root cause (matrix or the endpoint), and re-run — don't paper over with `|| true`.
6. Do not touch any product code in this task. If you find a mismatch that can't be fixed by adjusting the matrix, STOP and write the issue into the log; do not silently patch backend/frontend.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-026-05-rbac-e2e-smoke.md
```
