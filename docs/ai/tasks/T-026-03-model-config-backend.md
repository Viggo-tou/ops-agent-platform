# T-026-03 — Model / Provider Config Backend

## Goal

Move the model selector's provider catalog and the currently-selected model out of the frontend (`apps/web/src/components/settings/ModelSelector.tsx`, `window.localStorage["ops-agent-selected-model"]`) and into backend-owned config. The frontend should load the catalog and the selected model from API calls, and should persist the user's selection server-side.

Scope is read-heavy: a static catalog (shipped in config / seeded in the DB) and a tiny `selected_model` singleton. No API key storage in this task — keys stay out of the DB per `AGENTS.md` constraints. The "API configuration" tab can continue to show masked placeholders.

## Background

- Backend: FastAPI + SQLAlchemy, see the knowledge / memory feature slices for the canonical pattern.
- Frontend: React + TanStack Query. The relevant file is `apps/web/src/components/settings/ModelSelector.tsx`. It currently hardcodes a `modelGroups` array and reads / writes `window.localStorage.getItem("ops-agent-selected-model")`.
- RBAC: `can("settings:model_config")` is the gate today. Keep it. Backend enforcement comes in T-026-04; leave a `# TODO(T-026-04)` comment above any mutating endpoint.
- The current hardcoded catalog is the source of truth for the seed data. Copy the 9 providers and their models verbatim so the UI does not visibly change on first load.

## Files to create

1. `apps/backend/app/models/model_config.py`
   - `ModelProvider` ORM:
     - `name: str` (`String(64)`, primary key) — e.g. `"Anthropic"`, `"OpenAI"`.
     - `note: str` (`String(255)`, default `""`) — human-readable description.
     - `sort_order: int` (`Integer`, default `0`) — for stable ordering.
     - `created_at`, `updated_at`: `DateTime(timezone=True)` via `utcnow` / `onupdate=utcnow`.
   - `ModelEntry` ORM:
     - `id: str` (`String(64)`, primary key) — slug like `"claude-opus-4-6"`. Can match the display name if no dedicated slug.
     - `provider_name: str` (`String(64)`, `ForeignKey("model_provider.name")`, indexed).
     - `display_name: str` (`String(128)`).
     - `sort_order: int` (default `0`).
     - `created_at`, `updated_at` same pattern.
     - Relationship back to `ModelProvider` is optional; if added, use `relationship("ModelProvider", back_populates=None)` and keep it simple.
   - `SelectedModel` ORM (singleton):
     - `id: str` (`String(32)`, primary key, default `"default"`).
     - `model_id: str | None` (`String(64)`, `ForeignKey("model_entry.id")`, nullable).
     - `updated_at` same pattern.

2. `apps/backend/app/schemas/model_config.py`
   - `ModelEntryRead` with `id: str`, `display_name: str`, `sort_order: int`. `ConfigDict(from_attributes=True)`.
   - `ModelProviderRead` with `name: str`, `note: str`, `sort_order: int`, `models: list[ModelEntryRead]`. `ConfigDict(from_attributes=True)`.
   - `SelectedModelRead` with `model_id: str | None`, `updated_at: datetime`. `ConfigDict(from_attributes=True)`.
   - `SelectedModelUpdate` with `model_id: str | None` (explicit None is allowed to clear).

3. `apps/backend/app/services/model_config.py`
   - `DEFAULT_PROVIDER_CATALOG: list[dict]` that mirrors the frontend's existing `modelGroups` array. Suggested shape:
     ```python
     DEFAULT_PROVIDER_CATALOG = [
         {
             "name": "OpenAI",
             "note": "General reasoning and tool use",
             "models": ["GPT-5.4", "GPT-5.4 Mini", "GPT-4.1"],
         },
         {
             "name": "Anthropic",
             "note": "Long-context writing and coding",
             "models": ["Claude Opus 4.6", "Claude Sonnet 4.6", "Claude Haiku 4.5"],
         },
         {"name": "Google AI", "note": "Multimodal and fast assistant work", "models": ["Gemini 2.5 Pro", "Gemini 2.5 Flash"]},
         {"name": "DeepSeek", "note": "Reasoning and code-oriented tasks", "models": ["DeepSeek V3", "DeepSeek R1"]},
         {"name": "阿里云", "note": "Domestic provider compatibility", "models": ["Qwen Max"]},
         {"name": "智谱 AI", "note": "Domestic provider compatibility", "models": ["GLM-5"]},
         {"name": "Moonshot", "note": "Long-context Chinese and mixed-language tasks", "models": ["Kimi K2", "Kimi Turbo"]},
         {"name": "Mistral", "note": "Enterprise and coding workflows", "models": ["Mistral Large", "Codestral"]},
         {"name": "Cohere", "note": "RAG and enterprise retrieval", "models": ["Command R+", "Command A"]},
     ]
     ```
   - Model `id` can be derived by slugifying the display name: lowercase, replace spaces with `-`, strip non `[a-z0-9-]`. Preserve display names verbatim.
   - Functions (all taking a `Session`):
     - `bootstrap_model_catalog(db)` — idempotent seed. For each provider in `DEFAULT_PROVIDER_CATALOG`, upsert a `ModelProvider` row (by `name`), then upsert each `ModelEntry` (by `id`) under it. Maintain `sort_order` from the catalog order. Do **not** delete providers/models that are no longer in the default catalog — the catalog can be extended live later.
     - `list_providers(db) -> list[ModelProvider]` — ordered by `sort_order` then `name`; each provider's `models` ordered by `sort_order` then `display_name`. Return ORM objects; the API layer converts to schemas.
     - `get_selected_model(db) -> SelectedModel` — upsert the singleton `"default"` row on first read; if no model is selected yet, default to the first model in the first provider (by sort order) — this matches the frontend's prior default of `"GLM-5"` only by accident, so just pick the first ordered entry.
     - `set_selected_model(db, model_id: str | None) -> SelectedModel` — if `model_id` is not None, verify the `ModelEntry` exists, raise `LookupError` if not. Update and return.

4. `apps/backend/app/api/model_config.py`
   - `router = APIRouter(prefix="/model-config", tags=["model-config"])`.
   - Endpoints:
     - `GET /providers` → `list[ModelProviderRead]`.
     - `GET /selected` → `SelectedModelRead`.
     - `PATCH /selected` → `SelectedModelRead`. Body: `SelectedModelUpdate`. 404 on `LookupError`.
   - Use the `DbSession = Annotated[Session, Depends(get_db)]` pattern.
   - `# TODO(T-026-04): enforce settings:model_config via require_actor_role` above `PATCH /selected`.

## Files to edit

5. `apps/backend/app/models/__init__.py`
   - Import and re-export `ModelProvider`, `ModelEntry`, `SelectedModel`.

6. `apps/backend/app/main.py`
   - Import `from app.api.model_config import router as model_config_router`.
   - `app.include_router(model_config_router, prefix=settings.api_prefix)` alongside the other feature routers.
   - In the `lifespan` context manager, after `bootstrap_governance_data()`, call `bootstrap_model_catalog`. Use a short-lived session:
     ```python
     from app.core.db import SessionLocal
     from app.services.model_config import bootstrap_model_catalog
     with SessionLocal() as db:
         bootstrap_model_catalog(db)
     ```
   - If `SessionLocal` is named differently in `app/core/db.py`, use whatever helper that module exposes for one-shot sessions.

7. `apps/web/src/types.ts`
   - Add:
     ```ts
     export interface ModelEntry {
       id: string;
       display_name: string;
       sort_order: number;
     }
     export interface ModelProvider {
       name: string;
       note: string;
       sort_order: number;
       models: ModelEntry[];
     }
     export interface SelectedModel {
       model_id: string | null;
       updated_at: string;
     }
     export interface SelectedModelUpdate {
       model_id: string | null;
     }
     ```

8. `apps/web/src/lib/api.ts`
   - Add methods on `api`:
     - `getModelProviders()` → `GET /model-config/providers`.
     - `getSelectedModel()` → `GET /model-config/selected`.
     - `setSelectedModel(payload: SelectedModelUpdate)` → `PATCH /model-config/selected`.

9. `apps/web/src/components/settings/ModelSelector.tsx`
   - Remove the hardcoded `modelGroups` constant and the `window.localStorage` read/write.
   - Load providers via `useQuery(["model-providers"], () => api.getModelProviders())`.
   - Load the selected model via `useQuery(["selected-model"], () => api.getSelectedModel())`.
   - `selectModel` becomes a `useMutation` that calls `api.setSelectedModel({ model_id })` and invalidates `["selected-model"]` on success.
   - `providerChips` is derived from the loaded providers (keep `"全部"` as the first entry).
   - `selectedProvider` derived from the currently-selected model's provider (resolve by searching the loaded providers).
   - The "API configuration" tab's `provider-key-list` should iterate loaded providers instead of the old constant, but keep the password inputs as purely local state (no API call) with the same `"Managed by backend"` placeholder copy — no key storage in this task.
   - Preserve all existing class names, Chinese copy, RBAC guards, and visual structure. The page should look the same as before on first load.
   - While the provider query is loading, render an empty state (no provider chips, empty model list) — do not crash.

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- Backend starts cleanly. On first startup against an empty DB, `model_provider`, `model_entry`, `selected_model` tables are created and seeded with the 9 providers and all models from `DEFAULT_PROVIDER_CATALOG`.
- Round-trips (saved to `docs/ai/runs/T-026-03.log`):
  1. `GET /api/model-config/providers` returns 9 providers in the declared order, each with the expected model list.
  2. `GET /api/model-config/selected` returns `{model_id: "<first-entry-id>", updated_at: ...}` on a fresh DB.
  3. `PATCH /api/model-config/selected` with `{"model_id":"claude-opus-4-6"}` (or whatever slug the seeder generates for "Claude Opus 4.6") updates and returns the new value.
  4. `PATCH /api/model-config/selected` with `{"model_id":"not-a-real-id"}` returns 404.
- `npx tsc --noEmit -p apps/web/tsconfig.app.json` exits 0.
- `ModelSelector.tsx` contains no `localStorage` references and no hardcoded `modelGroups` constant.
- Visual regression: the Settings → 模型选择 tab, when loaded against a fresh backend, shows the same 9 providers and the same models in the same order as before this task. The selected state is preserved after a page refresh.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/models/memory.py`, `apps/backend/app/schemas/memory.py`, `apps/backend/app/services/memory.py`, `apps/backend/app/api/memory.py`, `apps/backend/app/main.py`, `apps/backend/app/core/db.py`, `apps/backend/app/services/governance.py` (for the `bootstrap_*` pattern), and `apps/web/src/components/settings/ModelSelector.tsx` before writing code.
2. Backend first: models → schemas → services → api → register in `models/__init__.py` + `main.py` lifespan. Verify with `python -m compileall app`.
3. Start the backend on a free port (port 8000 may be held by a stale process; use `--port 8010` or similar as needed — same approach as the T-026-02 run), run the 4 round-trips, save the transcript to `docs/ai/runs/T-026-03.log`.
4. Frontend: types → api client → `ModelSelector.tsx`. Run `npx tsc --noEmit -p apps/web/tsconfig.app.json`.
5. Do not change RBAC enforcement — leave `# TODO(T-026-04)` where appropriate.
6. Do not touch any file outside the lists above.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-026-03-model-config-backend.md
```
