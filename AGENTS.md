# Ops Agent Platform Agent Instructions

This file is the repo-level recovery and working guide for AI coding agents.

## Recovery Bootstrap

At the start of a new session, read these files before editing code:

1. `AGENTS.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_STATE.md`
4. `DECISIONS.md`
5. `TASK_QUEUE.md`
6. `SESSION_HANDOFF.md`

Then output:

- your understanding of the current project state
- the next 3 most reasonable actions
- any scope that should not be touched without confirmation

Do not make broad edits outside the active handoff scope until the current blocker and queue are understood.

## Project Goal

Build an enterprise AI assistant platform with:

- a minimal white AI workbench frontend
- chat-first task creation and conversation switching
- repository-grounded knowledge answers
- knowledge import and RAG management
- memory management
- model/provider settings
- login state and RBAC-sensitive controls
- governed backend task, tool, approval, and audit flows

The UI target is strict: white background, black text, light gray borders, restrained spacing, no gradients, no decorative panels, and no raw task/debug data as the primary answer.

## Stack

- Backend: FastAPI, SQLAlchemy, local SQLite by default
- Frontend: React 18, Vite, React Router, TanStack Query
- Local scripts: PowerShell scripts in `scripts/`
- Current architecture: single-runtime orchestrator

## Project Structure

```text
apps/backend  FastAPI API, orchestrator, agents, persistence, tools
apps/web      React + Vite frontend
docs/         Task cards and roadmap
references/   UI reference screenshots
scripts/      Local setup and startup scripts
```

## Local Commands

Backend:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1
```

Backend with reload:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1 -Reload
```

Frontend static server:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1
```

Frontend Vite dev server:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-web.ps1 -Dev
```

Open:

- Frontend: `http://127.0.0.1:5173`
- Backend docs: `http://127.0.0.1:8000/docs`
- Backend health: `http://127.0.0.1:8000/health`

## Current Agent Constraints

- Prefer incremental evolution over rewrites.
- Keep the single-runtime backend until governance and audit paths are stable.
- Do not introduce async workers, queues, or multi-service agent runtimes before the roadmap calls for it.
- Do not store raw provider API keys in frontend localStorage.
- Browser UI must not claim direct arbitrary local-path access without backend, desktop, or user-granted file access.
- Sensitive frontend operations must be checked before the UI mutation and before backend mutation.
- For chat, natural-language answers are primary; raw JSON, task status, review verdict, and plan metadata are diagnostic only.
- Keep UI styling unified in the existing frontend styling approach unless there is a clear reason to change.

## UI Reference Direction

Use the screenshots in `references/` as the source of truth for the next UI pass:

- fixed pale left sidebar around 240-270 px
- simple nav labels: chat, knowledge, memory, settings
- centered main content with a narrower readable column
- chat header with assistant name and selected model
- user message on the right as a black bubble
- assistant message on the left as a plain white card with natural copy
- bottom composer with attachment affordance and one send action
- knowledge page with centered title, upload button, dashed drop zone, and compact source list
- memory page with compact stats, search, and empty state
- settings page with tabs, provider chips, model rows, and a simple selected state

Avoid dashboard/CMS styling, dense tables, colorful badges, exposed status panels, and task-debug terminology in the main product surface.
