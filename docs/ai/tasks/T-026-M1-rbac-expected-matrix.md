# T-026-M1 — RBAC Expected-Response Matrix (MiniMax)

## Owner

MiniMax (low-difficulty, deterministic).

## Goal

Produce a fixture JSON file that enumerates, for every combination of (role, endpoint), the expected HTTP status code. This fixture is consumed by `scripts/verify-rbac.ps1` (Claude will build that later) to smoke-test RBAC end-to-end.

## Output

File: `apps/backend/tests/fixtures/rbac_expected_matrix.json`

Exact structure:

```json
{
  "roles": ["admin", "operator", "member", "viewer"],
  "endpoints": [
    {
      "method": "GET",
      "path": "/api/knowledge/documents",
      "requires": [],
      "expected": { "admin": 200, "operator": 200, "member": 200, "viewer": 200 }
    },
    { "...": "..." }
  ]
}
```

## Inputs (authoritative — do not infer from elsewhere)

### Permission map (from `apps/backend/app/core/security.py::PERMISSION_MAP`)

- `admin`: all 8 permissions
- `operator`: `task:create`, `task:create_high_risk`, `knowledge:upload`, `memory:edit`, `settings:view`, `settings:model_config`, `approval:decide`
- `member`: `task:create`, `memory:edit`
- `viewer`: (none)

### Endpoints to cover

| Method | Path | Required permission |
|---|---|---|
| GET    | /api/knowledge/documents            | (none — public read) |
| GET    | /api/knowledge/sources              | (none — public read) |
| GET    | /api/knowledge/search?query=x       | (none — public read) |
| POST   | /api/knowledge/sync                 | knowledge:upload |
| POST   | /api/knowledge/upload               | knowledge:upload |
| DELETE | /api/knowledge/documents/{id}       | knowledge:delete |
| DELETE | /api/knowledge/sources/{name}       | knowledge:delete |
| GET    | /api/memory/items                   | (none — public read) |
| POST   | /api/memory/items                   | memory:edit |
| PATCH  | /api/memory/items/{id}              | memory:edit |
| DELETE | /api/memory/items/{id}              | memory:edit |
| GET    | /api/memory/settings                | (none — public read) |
| PATCH  | /api/memory/settings                | memory:edit |
| GET    | /api/model-config/providers         | (none — public read) |
| GET    | /api/model-config/selected          | (none — public read) |
| PATCH  | /api/model-config/selected          | settings:model_config |
| GET    | /api/governance/roles               | (none — public read) |
| GET    | /api/governance/policy-rules        | (none — public read) |
| GET    | /api/approvals                      | (none — public read) |
| POST   | /api/approvals/{id}/grant           | approval:decide |
| POST   | /api/approvals/{id}/reject          | approval:decide |
| POST   | /api/tasks                          | task:create |

## Rules for filling `expected`

For each endpoint × role cell:

- If the endpoint has `requires: []`, set expected = `200` for all four roles. Exception: write endpoints that also need a request body will return `422` on empty body when called anonymously — but for this fixture assume a well-formed body is supplied, so it's `200` when allowed.
- If the endpoint requires any permission the role does not have, set expected = `403`.
- If the role has ALL required permissions, set expected = `200` (or `201` for POST-create endpoints; list them explicitly below).

### Endpoints that return 201 instead of 200 on success

- `POST /api/tasks`
- `POST /api/memory/items`
- `POST /api/knowledge/upload` — returns `200` (not `201`) per current code; keep `200`.

### Edge cases — be explicit in the JSON

- `viewer` has no `memory:edit`, so `POST/PATCH/DELETE /api/memory/items` → `403`.
- `member` lacks `knowledge:upload`, `knowledge:delete`, `settings:model_config`, `approval:decide` → those endpoints `403` for member.
- `operator` lacks only `knowledge:delete` → those DELETE endpoints are the only 403s for operator.

## Constraints

- Output must be valid JSON (no comments, no trailing commas).
- Keep endpoint order identical to the table above.
- Do not invent new endpoints. If unsure, omit.
- Do not add extra fields.

## Acceptance

- File exists at the exact path.
- `python -c "import json; json.load(open('apps/backend/tests/fixtures/rbac_expected_matrix.json'))"` passes.
- Manual spot-check: `viewer` + `POST /api/tasks` → `403`; `admin` + `DELETE /api/knowledge/sources/{name}` → `200`.
