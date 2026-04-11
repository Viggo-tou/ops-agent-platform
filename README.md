# Enterprise Ops Agent Platform

Internal enterprise AI assistant console and governed agent workflow platform.

## Current MVP

- Single runtime
- Single primary agent orchestration flow
- Persistent `task`, `event`, and `approval` state
- React AI workbench with chat-first navigation, knowledge, memory, settings, login state, and RBAC-aware controls
- Unified tool runtime with knowledge, Slack, Jira, internal API, and internal DB connectors
- Governance foundation with actor roles, risk categories, policy rules, approval metadata, and read APIs

## Current Status

The latest completed frontend task is `T-025 Minimal AI Workbench Frontend Refactor`.

The latest completed recovery task is `T-027 Resumable Development State Files`.

The latest completed chain fix is `T-028 Fix Chat Knowledge Answer Chain`.

The latest completed UI pass is `T-029 Strict Reference UI Pass`.

The latest completed chat behavior fix is `T-032 Same-Conversation Follow-up Turns`.

The latest completed handoff task is `T-033 Environment Handoff Documentation`.

Implemented in that pass:

- fixed left sidebar with recent conversation switching and local title rename support
- chat page that creates backend tasks and renders agent output as readable natural-language replies
- knowledge page with drag/drop, file, folder, zip, and future local-path import affordances
- memory page with local add, edit, delete, search, automatic memory toggle, whitelist, and blacklist controls
- settings page with provider/model groups for OpenAI, Anthropic, Google, DeepSeek, Moonshot, Mistral, Cohere, and domestic model providers
- local login state and frontend RBAC guards for admin, operator, member, and viewer roles
- restrained white/black/gray UI styling with low-noise borders and consistent component sizing

Known frontend scaffolding still waiting on backend persistence:

- multipart knowledge upload and zip ingestion endpoint
- server-side knowledge source delete or disable action
- backend memory store
- backend model/provider configuration read and safe admin write path
- server-confirmed RBAC checks for all sensitive UI mutations

Known active persistence gap:

- Knowledge upload, memory, and model/provider configuration still need backend-backed persistence and server-side enforcement.

## Recovery Files

For restart-safe development, read these files at the start of a new agent session:

1. `AGENTS.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_STATE.md`
4. `DECISIONS.md`
5. `TASK_QUEUE.md`
6. `SESSION_HANDOFF.md`

These files record the active blocker, decisions, task queue, runtime evidence, and the next first action after an interrupted session.

## Project Structure

```text
apps/backend  FastAPI API, orchestrator, persistence
apps/web      React + Vite task console
docs/         Scope notes and task card log
scripts/      Local startup scripts for Windows
```

## First-Time Setup

From the repo root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

That installs:

- backend Python dependencies from `apps/backend/requirements.txt`
- frontend npm dependencies from `apps/web/package.json`

## Start The MVP

Open two terminals in the repo root.

Terminal 1, backend:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1
```

This starts the backend in normal mode.  
If you explicitly want auto-reload:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1 -Reload
```

Terminal 2, frontend:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1
```

This starts the built frontend with a lightweight Python static server, which is the most reliable local mode in this environment.  
If you explicitly want Vite dev mode:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1 -Dev
```

## What To Open

Once both servers are running:

- Frontend app: `http://127.0.0.1:5173`
- Backend API docs: `http://127.0.0.1:8000/docs`
- Backend health check: `http://127.0.0.1:8000/health`

## Direct Commands

If you do not want to use the scripts:

Backend:

```powershell
Set-Location .\apps\backend
& "$env:LOCALAPPDATA\Python\bin\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Frontend:

```powershell
Set-Location .\apps\web
& "$env:LOCALAPPDATA\Python\bin\python.exe" ..\..\scripts\serve-web.py --host 127.0.0.1 --port 5173 --dir .\dist
```

## Notes

- The backend defaults to a local SQLite database at `apps/backend/ops_agent_platform.db`
- The frontend defaults to calling `http://127.0.0.1:8000/api`
- The primary agent can run in mock or OpenAI-backed mode via:
- `OPS_AGENT_PRIMARY_AGENT_PROVIDER=auto|mock|openai`
- `OPS_AGENT_PRIMARY_AGENT_MODEL=gpt-4o-mini` by default
- `OPS_AGENT_OPENAI_API_KEY=...` to enable real provider calls
- The semantic translation layer can run in deterministic or MiniMax-backed mode via:
- `OPS_AGENT_SEMANTIC_TRANSLATOR_PROVIDER=auto|mock|minimax`
- `OPS_AGENT_SEMANTIC_TRANSLATOR_MODEL=MiniMax-M2.7` by default
- `OPS_AGENT_MINIMAX_API_KEY=...` to enable real MiniMax normalization before planning and retrieval
- The knowledge agent defaults to the detected local Handyman repository, or you can override it with:
- `OPS_AGENT_KNOWLEDGE_SOURCE_PATH=...`
- `OPS_AGENT_KNOWLEDGE_SOURCE_SPECS=name=path;name2=path2` for multiple repositories
- Phase 4 tool connectors are configured through:
- `OPS_AGENT_SLACK_BOT_TOKEN` and `OPS_AGENT_SLACK_DEFAULT_CHANNEL`
- `OPS_AGENT_JIRA_BASE_URL`, `OPS_AGENT_JIRA_PROJECT_KEY`, and Jira credentials
- Existing Jira issue planning accepts either an issue key such as `P69-10` or a Jira URL containing `/browse/P69-10` or `selectedIssue=P69-10`
- `OPS_AGENT_INTERNAL_API_BASE_URL` and `OPS_AGENT_INTERNAL_API_TOKEN`
- `OPS_AGENT_INTERNAL_DB_URL` for guarded read-only internal DB queries
- Effective tool permissions can be overridden with `OPS_AGENT_TOOL_PERMISSION_OVERRIDES`
- If PowerShell blocks script execution, keep using the `-ExecutionPolicy Bypass` form shown above

## Next Development Plan

Immediate next task card: `T-026 Workbench Backend Persistence and Governance Integration`.

Immediate implementation sequence:

1. Add backend knowledge import endpoints for files and zip archives, keeping browser local-path access compliant.
2. Add backend knowledge source delete or disable APIs and wire the existing UI delete action to server enforcement.
3. Add backend memory APIs for list, create, update, delete, automatic memory settings, whitelist, and blacklist topics.
4. Add backend model/provider configuration read APIs and a safe admin-only write path that does not expose raw secrets in the browser.
5. Connect frontend RBAC checks to backend governance roles and policy-rule responses where possible.
6. Re-run frontend and backend smoke tests for admin, operator, member, and viewer roles.

The broader roadmap remains in `docs/phase-5-7-enterprise-roadmap.md`, but the active next product task is now T-026.

## Tracking

Implementation task history is recorded in [docs/task-cards.md](./docs/task-cards.md).
