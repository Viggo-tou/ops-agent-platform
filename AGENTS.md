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

## Session Boundary Discipline (MANDATORY)

The repo has an accumulated-uncommitted-work problem (see T-037 in `TASK_QUEUE.md`). To stop this recurring, every session MUST follow this ritual:

### At session start

1. Run `git status --short` and `git log --oneline -5`. If HEAD is not a clean checkpoint of prior session work, flag this to the user before editing anything.
2. Create a session-start baseline tag: `git tag session-start/YYYY-MM-DD-HHMM` on current HEAD. This gives an unambiguous diff baseline for the session.
3. Note in the first turn which tag was created.

### During the session

- Keep a running list of files touched, in memory or in a scratch note. At the end you must be able to answer "which files did this session modify" without guessing.

### At session end

Exactly one of the following must happen before the session closes:

- **Option A (preferred):** Commit session work. Even a rough `wip:` commit is better than leaving the working tree dirty. The user can rebase/reword later.
- **Option B (docs-only sessions):** Update `SESSION_HANDOFF.md` with a manifest section listing:
  - Session-start tag name
  - Exact file paths touched
  - `git diff --stat <session-start-tag>..HEAD -- <file>` output for each file (or a note that the file is still in working tree)
  - Whether the session's code changes are committed or still dirty

If neither A nor B happens, the session has failed its contract.

### Rationale

Without a baseline tag, no git operation can separate "this session's work" from "all prior uncommitted work". Once multiple sessions stack dirty changes on top of each other, the only way out is a big rebaseline commit that attributes everything to a single author — losing real history. The ritual above costs ~10 seconds per session and prevents that failure mode.

## Project Goal

Build a single-tenant agent runtime that **looks like** an enterprise
AI assistant platform from the audit / approval / governance side
(those parts are real), while being honest that several "enterprise"
ingredients (process-level sandbox, multi-tenant DB, durable job
queue, secret management) are out of scope for the current
implementation. See `PROJECT_CONTEXT.md` "Honest scope" for the
precise claim/caveat split before reusing the "enterprise" word
externally.

Concretely:

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

## Execution Workflow (Claude / codex / MiniMax)

This project uses a three-way split between AI coding agents. Roles are not interchangeable.

- **Claude** — director, planner, reviewer. Reads code, writes specs, reviews diffs, runs verification (`python -m compileall app`, `tsc --noEmit`, `npm run build`, targeted smoke). Does **not** use Edit/Write on product source files.
- **codex CLI** — primary executor for non-trivial code work across `apps/backend/**` and `apps/web/**`. Invoked via `codex exec` (see `Bash(codex *)` in `.claude/settings.json`).
- **MiniMax** — cheaper executor for low-difficulty, well-scoped edits (simple renames, string/text tweaks, mechanical refactors, docstring/comment changes). Config via `OPS_AGENT_MINIMAX_*` env vars in `apps/backend/.env`. MiniMax also runs at runtime as the backend semantic translator; the "easy-lane executor" role is a dev-time convention.

### What Claude may edit directly

Meta, recovery, and config files only:

- `.claude/settings.json`, `.claude/**`
- Recovery/handoff docs: `AGENTS.md`, `PROJECT_CONTEXT.md`, `CURRENT_STATE.md`, `TASK_QUEUE.md`, `DECISIONS.md`, `SESSION_HANDOFF.md`, `CLAUDE.md`, `README.md`, `docs/task-cards.md`
- Task specs in `docs/ai/tasks/*.md`, `docs/ai/runs/*.log`
- `.gitignore`, `requirements.txt` (borderline config, Claude-authored is fine)
- Personal Claude memory under `~/.claude/projects/.../memory/**`

Everything else — backend services, API handlers, schemas, React components, styles — goes through codex or MiniMax.

### Spec and run convention

- Task spec: `docs/ai/tasks/<task-id>.md`. Canonical example: `docs/ai/tasks/T-UI-01-reference-alignment.md`.
- Each spec must end with: **files to edit**, **acceptance criteria**, and a **Workflow (for the executor)** line naming the executor (codex or MiniMax).
- Run log: `docs/ai/runs/<task-id>.log` (plus `-rerun.log`, `-stdin.log` variants).

### Codex dispatch pattern

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/<spec>.md
```

After any codex or MiniMax run, Claude **must** re-read the changed files and run verification before reporting success. Never trust "done" without reviewing the diff.

If unsure whether a task is simple enough for MiniMax, ask the user.

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
