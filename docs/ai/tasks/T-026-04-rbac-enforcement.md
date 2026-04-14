# T-026-04 — Frontend RBAC ↔ Backend Enforcement

## Goal

Close the loop between the frontend permission gates (`can("memory:edit")`, `can("knowledge:upload")`, etc.) and the backend. Today the backend accepts any anonymous request. After this task:

1. Every mutating backend endpoint requires an `X-Actor-Role` request header. Missing / unknown values are rejected with 401.
2. A backend permission map (the authoritative one) decides whether a given `ActorRole` may call a given endpoint. Forbidden roles get 403.
3. The frontend `api` client automatically sends `X-Actor-Role` based on the logged-in user. There is no per-call opt-in.
4. All the `# TODO(T-026-04)` markers left by T-026-02 and T-026-03 are resolved.

Scope is limited to **header-based role enforcement on endpoint mutations**. This is deliberately simpler than the `PolicyRule` engine — policy-rule evaluation is still only used for task/tool governance, not for API-endpoint gating. Endpoint gating is a static map because it needs to exist synchronously on every request.

## Background

- Frontend role model (`apps/web/src/lib/auth.tsx`): `AppRole = "admin" | "operator" | "member" | "viewer"`. `toBackendActorRole` already maps these to `"admin" | "team_lead" | "employee"`. Viewers currently fall through to `"employee"` on the backend, which is wrong for enforcement — viewers should be gated out of mutations. Fix: map `viewer → "employee"` on the backend side still, but make viewer explicitly forbidden from mutations by giving `member`/`viewer` the same backend role and letting the permission map handle it. Actually simpler: send the literal frontend role as well, via a second header `X-Actor-App-Role`, so the backend can distinguish `member` from `viewer`. See "Header format" below.
- Frontend `Permission` type has 8 entries. Backend should mirror those as a string literal set so both sides agree on the spelling.
- Backend `ActorRole` enum values: `employee`, `team_lead`, `manager`, `admin`, `system`. Use these.
- Existing endpoints that mutate and must be guarded:
  - `apps/backend/app/api/memory.py`: POST/PATCH/DELETE items, PATCH settings → permission `"memory:edit"`.
  - `apps/backend/app/api/knowledge.py`: POST /upload, POST /sync, DELETE /documents/{id}, DELETE /sources/{name} → permissions `"knowledge:upload"` (upload + sync) and `"knowledge:delete"` (delete document/source).
  - `apps/backend/app/api/model_config.py`: PATCH /selected → `"settings:model_config"`.
  - `apps/backend/app/api/tasks.py`: POST /tasks → `"task:create"` (plus `"task:create_high_risk"` if the risk level crosses a threshold — keep it simple for now and only require `"task:create"`, mark `"task:create_high_risk"` as a TODO for a later task). POST /tasks/{id}/rollback → `"approval:decide"`.
  - `apps/backend/app/api/approvals.py`: POST /approvals/{id}/grant and /reject → `"approval:decide"`.
- Read endpoints (`GET`) stay open for now — don't gate them. The frontend already hides them when a viewer has no permissions. Backend GETs are low-risk and we are not building a public-facing API.

## Header format

Two headers so we can cleanly enforce both:

- `X-Actor-Role` — the backend `ActorRole` value: one of `employee | team_lead | manager | admin | system`. Required on all mutations.
- `X-Actor-App-Role` — the frontend `AppRole` value: `admin | operator | member | viewer`. Optional but recommended; if present and inconsistent with `X-Actor-Role`, the backend prefers `X-Actor-App-Role` (it is more specific). If missing, fall back to `X-Actor-Role`.

The permission map is keyed on the **frontend `AppRole`** (to match the frontend source of truth exactly):

```python
PERMISSION_MAP: dict[str, set[str]] = {
    "admin":    {"task:create", "task:create_high_risk", "knowledge:upload", "knowledge:delete",
                 "memory:edit", "settings:view", "settings:model_config", "approval:decide"},
    "operator": {"task:create", "task:create_high_risk", "knowledge:upload",
                 "memory:edit", "settings:view", "settings:model_config", "approval:decide"},
    "member":   {"task:create", "memory:edit"},
    "viewer":   set(),
}
```

This exactly mirrors `rolePermissions` in `apps/web/src/lib/auth.tsx`. Keep them in sync; add a comment in both places pointing at the other.

## Files to create

1. `apps/backend/app/core/security.py`
   - `Permission = Literal["task:create", "task:create_high_risk", "knowledge:upload", "knowledge:delete", "memory:edit", "settings:view", "settings:model_config", "approval:decide"]`.
   - `AppRole = Literal["admin", "operator", "member", "viewer"]`.
   - `PERMISSION_MAP: dict[AppRole, frozenset[Permission]]` as above (use `frozenset` for immutability).
   - `ACTOR_ROLE_TO_APP_ROLE` fallback table for when only `X-Actor-Role` is present:
     - `admin → admin`
     - `team_lead → operator`
     - `manager → operator`
     - `employee → member` (cannot distinguish member from viewer without the app-role header — be permissive here; viewer callers should always send both headers)
     - `system → admin` (system calls have full privilege)
   - `class ActorContext` (dataclass): `app_role: AppRole`, `actor_role: ActorRole`.
   - Dependency factory:
     ```python
     def require_permission(*permissions: Permission) -> Callable[..., ActorContext]:
         def dependency(
             x_actor_role: Annotated[str | None, Header(alias="X-Actor-Role")] = None,
             x_actor_app_role: Annotated[str | None, Header(alias="X-Actor-App-Role")] = None,
         ) -> ActorContext:
             # resolve backend ActorRole
             if not x_actor_role:
                 raise HTTPException(401, "Missing X-Actor-Role header")
             try:
                 actor_role = ActorRole(x_actor_role)
             except ValueError:
                 raise HTTPException(401, f"Unknown actor role: {x_actor_role}")
             # resolve app role
             if x_actor_app_role and x_actor_app_role in PERMISSION_MAP:
                 app_role: AppRole = x_actor_app_role  # type: ignore[assignment]
             else:
                 app_role = ACTOR_ROLE_TO_APP_ROLE.get(actor_role, "viewer")
             granted = PERMISSION_MAP[app_role]
             missing = [p for p in permissions if p not in granted]
             if missing:
                 raise HTTPException(403, f"Missing permissions: {', '.join(missing)}")
             return ActorContext(app_role=app_role, actor_role=actor_role)
         return dependency
     ```
   - Export both `require_permission` and a lighter `get_actor_context` dependency for endpoints that need the context but no specific permission.

## Files to edit

2. `apps/backend/app/api/memory.py`
   - Add `ActorCtx = Annotated[ActorContext, Depends(require_permission("memory:edit"))]` and use it on `create_item`, `update_item`, `delete_item`, `update_settings`.
   - Remove the 4 `# TODO(T-026-04)` comments.

3. `apps/backend/app/api/knowledge.py`
   - `upload_knowledge_documents` and `sync_knowledge` → `require_permission("knowledge:upload")`.
   - `delete_knowledge_document` and `delete_knowledge_source` → `require_permission("knowledge:delete")`.
   - Read endpoints (`list_knowledge_sources`, `list_knowledge_documents`) stay open.

4. `apps/backend/app/api/model_config.py`
   - `update_selected_model` → `require_permission("settings:model_config")`.
   - Remove the `# TODO(T-026-04)` comment.

5. `apps/backend/app/api/tasks.py`
   - `create_task` → `require_permission("task:create")`. Add a comment `# TODO: task:create_high_risk gate based on risk_level`.
   - `rollback_task` (if present — check the file; if it lives elsewhere, leave a TODO and adjust the scope note) → `require_permission("approval:decide")`.
   - List/get endpoints stay open.

6. `apps/backend/app/api/approvals.py`
   - `grant_approval`, `reject_approval` → `require_permission("approval:decide")`.

7. `apps/web/src/lib/api.ts`
   - Add a module-level mutable holder for the actor context:
     ```ts
     let currentActorRole: string | null = null;
     let currentAppRole: string | null = null;
     export function setApiActor(actorRole: string | null, appRole: string | null) {
         currentActorRole = actorRole;
         currentAppRole = appRole;
     }
     ```
   - In `request()` and `requestMultipart()`, if `currentActorRole` is set, add headers:
     ```
     "X-Actor-Role": currentActorRole,
     "X-Actor-App-Role": currentAppRole ?? "",
     ```
     (only add `X-Actor-App-Role` if non-null.)

8. `apps/web/src/lib/auth.tsx`
   - In `AuthProvider`, `useEffect` on `user` change: call `setApiActor(backendActorRole, user?.role ?? null)` when user logs in; call `setApiActor(null, null)` on logout. Import `setApiActor` from `../lib/api`.
   - Add a top-of-file comment pointing at `apps/backend/app/core/security.py::PERMISSION_MAP` and note that `rolePermissions` must be kept in sync.

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- Smoke test from `docs/ai/runs/T-026-04.log` (start backend on a free port such as 8010, run curl directly or via PowerShell `Invoke-RestMethod`):
  1. `POST /api/memory/items` **without** `X-Actor-Role` header → 401.
  2. `POST /api/memory/items` with `X-Actor-Role: employee` and `X-Actor-App-Role: viewer` → 403.
  3. `POST /api/memory/items` with `X-Actor-Role: employee` and `X-Actor-App-Role: member` → 201.
  4. `DELETE /api/knowledge/documents/<nonexistent>` with `X-Actor-Role: employee, X-Actor-App-Role: member` → 403 (member lacks `knowledge:delete`; check this happens **before** the 404 lookup).
  5. `DELETE /api/knowledge/documents/<nonexistent>` with `X-Actor-Role: admin, X-Actor-App-Role: admin` → 404 (permission check passes, doc-not-found is the real failure).
  6. `PATCH /api/model-config/selected` with `X-Actor-Role: team_lead, X-Actor-App-Role: operator` and a valid `model_id` → 200.
  7. `POST /api/approvals/<invalid-id>/grant` with `X-Actor-Role: employee, X-Actor-App-Role: member` → 403.
- `npx tsc --noEmit -p apps/web/tsconfig.app.json` exits 0.
- No `# TODO(T-026-04)` markers remain in any api file.
- `grep -n TODO.*T-026-04 apps/backend/app` returns nothing.

## Out of scope

- Policy-rule evaluation for endpoint gating (that's the existing `PolicyRule` engine; endpoint gating stays on the static `PERMISSION_MAP`).
- High-risk gate split on task creation — leave the TODO.
- UI changes beyond plumbing the header — the existing `can()` gates stay as-is and already hide buttons for viewers.
- Any session / cookie / JWT work. Headers from frontend state are fine for the MVP.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/web/src/lib/auth.tsx` (line 31 `rolePermissions`), `apps/backend/app/core/enums.py` (`ActorRole`), `apps/backend/app/api/memory.py`, `apps/backend/app/api/knowledge.py`, `apps/backend/app/api/model_config.py`, `apps/backend/app/api/tasks.py`, `apps/backend/app/api/approvals.py`, `apps/web/src/lib/api.ts`. Confirm the exact shape of each endpoint before wiring dependencies.
2. Create `app/core/security.py`. Double-check that `frozenset` values hash correctly and the dependency factory is importable. Run `python -m compileall app`.
3. Edit each api file in the list above to use `require_permission`. Re-run `compileall`.
4. Start the backend on a free port and run the 7 smoke round-trips from the acceptance criteria. Save the transcript (including status codes) to `docs/ai/runs/T-026-04.log`. Use `Invoke-RestMethod` or `curl.exe` — whichever works cleanly on the local Windows shell.
5. Edit `apps/web/src/lib/api.ts` to add the header-injection logic, then `apps/web/src/lib/auth.tsx` to call `setApiActor`. Run `npx tsc --noEmit -p apps/web/tsconfig.app.json`.
6. Sanity-check that the frontend still renders the workbench pages without runtime errors (`npm run build` is acceptable as a proxy if browser launch is not possible; otherwise skip and report).
7. Do not touch any file outside the lists above.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-026-04-rbac-enforcement.md
```
