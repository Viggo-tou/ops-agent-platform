# T-026-02 — Memory Backend Persistence

## Goal

Replace the frontend-only `localStorage` memory store with a backend-backed API. The frontend Memory page currently keeps items and settings under `ops-agent-memory-items` / `ops-agent-memory-settings` in `window.localStorage`. After this task, memory items and memory controls (enabled flag, allow/block topic lists) must persist server-side via the existing FastAPI app and be shared across sessions/devices.

Scope of this task is CRUD + settings only. No automatic extraction, no embeddings, no cross-user partitioning beyond a single global owner — that comes later.

## Background

- Backend stack: FastAPI + SQLAlchemy (SQLite default) in `apps/backend/app/`. Pattern for a feature slice: `models/<name>.py` + `schemas/<name>.py` + `services/<name>.py` + `api/<name>.py`, wired in `app/models/__init__.py` and `app/main.py`. See the `knowledge_document` / knowledge feature for a full reference.
- Tables are auto-created on startup by `Base.metadata.create_all(bind=engine)` in `app/main.py` lifespan — no Alembic, no migration files needed.
- Frontend stack: React 18 + Vite + TanStack Query. API client lives in `apps/web/src/lib/api.ts`. Shared types live in `apps/web/src/types.ts`. The memory panel is `apps/web/src/components/memory/MemoryPanel.tsx` and is rendered on `apps/web/src/pages/memory/MemoryPage.tsx`.
- The frontend RBAC check for memory edits is `can("memory:edit")` and must be preserved. Viewers may GET; only roles with `memory:edit` may POST/PATCH/DELETE. Backend governance wiring is handled in subitem 4, so for now the backend should simply accept the operations without RBAC enforcement (add a `# TODO(T-026-04)` comment where the `require_actor_role` dependency will later go).

## Files to create

1. `apps/backend/app/models/memory.py`
   - `MemoryItem` ORM model. Columns:
     - `id: str` (UUID4, primary key, default via `lambda: str(uuid4())`, `String(36)`)
     - `title: str` (`String(255)`, indexed)
     - `body: str` (`Text`)
     - `topic: str` (`String(64)`, indexed, default `"general"`)
     - `created_at`, `updated_at`: `DateTime(timezone=True)` via `utcnow` from `app.models.base`, with `onupdate=utcnow` on `updated_at`.
   - `MemorySettings` ORM model (single-row table keyed on a constant primary key, e.g. `id: str` default `"default"`). Columns:
     - `id: str` (`String(32)`, primary key, default `"default"`)
     - `enabled: bool` (`Boolean`, default `False`)
     - `allow_list: str` (`Text`, default `""`) — stored as a comma-separated string to match the current UI contract.
     - `block_list: str` (`Text`, default `""`)
     - `updated_at` via `utcnow`/`onupdate=utcnow`.

2. `apps/backend/app/schemas/memory.py`
   - `MemoryItemBase` with `title: str`, `body: str`, `topic: str = "general"` (use `Field(min_length=1)` on `title` and `body`; strip whitespace).
   - `MemoryItemCreate(MemoryItemBase)`.
   - `MemoryItemUpdate` with optional `title`, `body`, `topic` (all optional, same validators when present).
   - `MemoryItemRead(MemoryItemBase)` with `id: str`, `created_at: datetime`, `updated_at: datetime`. Use `model_config = ConfigDict(from_attributes=True)`.
   - `MemorySettingsRead` with `enabled: bool`, `allow_list: str`, `block_list: str`, `updated_at: datetime`.
   - `MemorySettingsUpdate` with all three fields optional.

3. `apps/backend/app/services/memory.py`
   - Pure functions taking a SQLAlchemy `Session`:
     - `list_memory_items(db, search: str | None = None) -> list[MemoryItem]` — case-insensitive `LIKE` over title/body/topic when `search` is non-empty; order by `updated_at DESC`.
     - `create_memory_item(db, payload: MemoryItemCreate) -> MemoryItem`
     - `update_memory_item(db, item_id: str, payload: MemoryItemUpdate) -> MemoryItem` — raise `LookupError` if missing.
     - `delete_memory_item(db, item_id: str) -> None` — raise `LookupError` if missing.
     - `get_memory_settings(db) -> MemorySettings` — upsert the singleton row (`id="default"`) on first read.
     - `update_memory_settings(db, payload: MemorySettingsUpdate) -> MemorySettings` — merges only provided fields.

4. `apps/backend/app/api/memory.py`
   - `router = APIRouter(prefix="/memory", tags=["memory"])`.
   - Endpoints:
     - `GET /items` → `list[MemoryItemRead]`, optional `search: str | None = None` query param.
     - `POST /items` → `MemoryItemRead` (201).
     - `PATCH /items/{item_id}` → `MemoryItemRead`; `HTTPException(404)` on `LookupError`.
     - `DELETE /items/{item_id}` → `{"ok": True}`; 404 on `LookupError`.
     - `GET /settings` → `MemorySettingsRead`.
     - `PATCH /settings` → `MemorySettingsRead`.
   - Use `DbSession = Annotated[Session, Depends(get_db)]` following the existing `app/api/knowledge.py` pattern (import `get_db` from wherever knowledge.py imports it).
   - Add a `# TODO(T-026-04): enforce memory:edit via require_actor_role` above the mutating endpoints.

## Files to edit

5. `apps/backend/app/models/__init__.py`
   - Import `MemoryItem` and `MemorySettings`; add to `__all__`.

6. `apps/backend/app/main.py`
   - Import `from app.api.memory import router as memory_router` and `app.include_router(memory_router, prefix=settings.api_prefix)` alongside the other feature routers.

7. `apps/web/src/types.ts`
   - Add:
     ```ts
     export interface MemoryItem {
       id: string;
       title: string;
       body: string;
       topic: string;
       created_at: string;
       updated_at: string;
     }
     export interface MemoryItemCreate {
       title: string;
       body: string;
       topic?: string;
     }
     export interface MemoryItemUpdate {
       title?: string;
       body?: string;
       topic?: string;
     }
     export interface MemorySettings {
       enabled: boolean;
       allow_list: string;
       block_list: string;
       updated_at: string;
     }
     export interface MemorySettingsUpdate {
       enabled?: boolean;
       allow_list?: string;
       block_list?: string;
     }
     ```

8. `apps/web/src/lib/api.ts`
   - Add methods on the exported `api` object:
     - `listMemoryItems(search?: string)` → `GET /memory/items` (append `?search=` when non-empty).
     - `createMemoryItem(payload: MemoryItemCreate)` → `POST /memory/items`.
     - `updateMemoryItem(itemId: string, payload: MemoryItemUpdate)` → `PATCH /memory/items/{id}`.
     - `deleteMemoryItem(itemId: string)` → `DELETE /memory/items/{id}`.
     - `getMemorySettings()` → `GET /memory/settings`.
     - `updateMemorySettings(payload: MemorySettingsUpdate)` → `PATCH /memory/settings`.
   - Follow the existing `request<T>()` pattern (JSON body, error formatting).

9. `apps/web/src/components/memory/MemoryPanel.tsx`
   - Remove `STORAGE_KEY`, `SETTINGS_STORAGE_KEY`, `readMemoryItems`, `storeMemoryItems`, `readMemorySettings`, and the `useEffect` that writes settings to `localStorage`.
   - Load items via `useQuery(["memory-items", search], () => api.listMemoryItems(search || undefined))`. Debounce or just refetch on search submit — simple refetch is fine.
   - Load settings via `useQuery(["memory-settings"], () => api.getMemorySettings())`. Seed local `enabled`/`allowList`/`blockList` state from query data in a `useEffect` when the query resolves; writes go through a `useMutation` that calls `updateMemorySettings`. Persist settings on a short debounce OR on blur — a debounced `useEffect` (~500 ms) is acceptable.
   - Add/edit/delete operations use `useMutation` hooks that call the api methods, then `queryClient.invalidateQueries({ queryKey: ["memory-items"] })` in `onSuccess`.
   - Keep the existing UI layout, class names, RBAC `can("memory:edit")` guards, and Chinese copy unchanged.
   - While queries are loading, keep the existing empty-state visual (no new spinner required).

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- Backend starts via `python -m uvicorn app.main:app --port 8000` without error; the following curl round-trips succeed:
  1. `POST /api/memory/items` with `{"title":"t","body":"b","topic":"general"}` returns 200 and a generated `id`.
  2. `GET /api/memory/items` returns the newly created row.
  3. `PATCH /api/memory/items/<id>` with `{"title":"t2"}` updates and returns the row.
  4. `DELETE /api/memory/items/<id>` returns `{"ok":true}`; subsequent GET no longer lists it.
  5. `GET /api/memory/settings` returns the default `{enabled:false, allow_list:"", block_list:"", updated_at:...}` row (auto-created).
  6. `PATCH /api/memory/settings` with `{"enabled":true,"allow_list":"a,b"}` updates the singleton.
- `npm run -s lint --workspace apps/web` (if a lint script exists) and `npx -y tsc --noEmit -p apps/web/tsconfig.app.json` both exit 0.
- Frontend memory page loads without console errors when the backend is running; creating / editing / deleting a memory item and toggling the automatic-memory switch all persist across a page reload.
- No `localStorage` reads/writes remain in `MemoryPanel.tsx`.

## Workflow (for the executor, i.e. Codex)

1. Read the canonical references first: `apps/backend/app/models/knowledge_document.py`, `apps/backend/app/schemas/knowledge.py`, `apps/backend/app/services/knowledge.py`, `apps/backend/app/api/knowledge.py`, `apps/backend/app/main.py`, `apps/backend/app/models/base.py`, `apps/web/src/lib/api.ts`, and `apps/web/src/components/memory/MemoryPanel.tsx`. Match their import style, type conventions, and error-handling patterns.
2. Implement backend first (models → schemas → services → api → register in `models/__init__.py` and `main.py`). Verify with `python -m compileall app`.
3. Start the backend locally, run the six curl round-trips from the acceptance criteria, save the output to `docs/ai/runs/T-026-02.log`.
4. Implement frontend changes (types → api client → MemoryPanel). Run `npx -y tsc --noEmit -p apps/web/tsconfig.app.json`.
5. Do not change RBAC enforcement — leave the TODO for subitem T-026-04.
6. Do not touch any file outside the lists above.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-026-02-memory-backend.md
```
