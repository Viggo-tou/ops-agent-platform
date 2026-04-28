# Enterprise Ops Agent Platform

This file is the Claude-specific companion to the repo-level recovery guide in `AGENTS.md`.

Before changing code in a new session, read:

1. `AGENTS.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_STATE.md`
4. `DECISIONS.md`
5. `TASK_QUEUE.md`
6. `SESSION_HANDOFF.md`
7. **`docs/ai/STAGE_LOG.md`** — stage-级活动流水，最新进度在底部。session 启动时**必读**最后 5-10 个 entry。

## Session Boundary Discipline (MANDATORY — read before any edit)

See `AGENTS.md` → "Session Boundary Discipline" for the full rules. Summary:

1. **At session start:** run `git status --short` + `git log --oneline -5`, then create `git tag session-start/YYYY-MM-DD-HHMM` on current HEAD. Announce the tag in your first response.
2. **At session end:** either commit (preferred) OR append a manifest to `SESSION_HANDOFF.md` listing files touched + `git diff --stat <tag>..HEAD -- <file>` per file. No exceptions.
3. **Why:** repo currently has only one commit (`940b232`). Multiple prior sessions left work uncommitted, so no git operation can separate "this session's work" from "everything prior". The tag + manifest ritual makes future sessions recoverable even without committing.

## Stage Log Discipline (MANDATORY — 2026-04-28 added)

`docs/ai/STAGE_LOG.md` is **append-only** and **the only reliable source-of-truth for "what's actually in flight right now"**. Session-handoff is too coarse; phase-summary is too coarse. Stage = a focused unit of work between them.

**Rules**:

1. **Open a stage** before starting any non-trivial unit of work. Append `Stage <N>` entry with status `OPEN`, layer (L1-L4), trigger, timestamp. Use the template at the top of `STAGE_LOG.md`.
2. **Update the stage** every time you:
   - dispatch a codex / minimax task
   - finish a sub-step (commit, file written, gate ran, benchmark scored)
   - hit a blocker
   Append a sub-bullet under that stage's "步骤" with timestamp + fact (not plan).
3. **Close the stage** with `CLOSED-DONE` / `CLOSED-DROPPED` / `BLOCKED` and fill the "Close 摘要" block: 结果 / 产出文件 / 没做的 / Lesson.
4. **Never edit old entries**. If a closed stage was wrong, open a new stage with `Trigger: revisit Stage <N> because ...`.
5. **Read on session-start**: read at least the last 5-10 entries of `STAGE_LOG.md` before announcing your session-start tag. The log tells you what's open / dispatched / blocked right now.

**Why**: today's evidence — Claude (me) wrote 5 fresh specs without realizing 3 of them duplicated work already done in worktrees, because there was no flat append-only log to scan. STAGE_LOG.md fixes that. **Without this discipline, every new session re-discovers the same things and re-proposes the same tickets.**

## Current Project State

The project has moved beyond the original Phase 0 / Phase 1 MVP notes.

Current baseline:

- FastAPI backend with task, event, approval, governance, knowledge, tool, and orchestrator modules.
- React + Vite frontend in `apps/web`.
- Single-runtime orchestrator remains the default architecture.
- MiniMax-backed semantic translation and planning are available when configured, with safe fallback behavior.
- Governance foundation exists: actor roles, policy rules, risk categories, approval metadata, and read APIs.
- Frontend has been refactored into a minimal AI workbench with chat, knowledge, memory, settings, login state, and frontend RBAC controls.
- The local screenshots in `references/` are now the visual source of truth for the next UI pass.
- T-028 fixed the P0 blocker where chat repository questions could expose planner text when the backend knowledge-answer chain failed review.
- T-029 completed the first strict reference UI pass across sidebar, chat, home, knowledge, memory, settings, and shared CSS.
- T-032 completed same-conversation follow-up turns by reusing `session_id`, carrying prior context, and grouping sidebar conversations by session.
- T-033 added `PROJECT_CONTEXT.md` and tightened handoff docs for continuing in a new environment.

Authoritative task history lives in `docs/task-cards.md`.

## Active Next Task

Immediate next task card:

- `T-026 Workbench Backend Persistence and Governance Integration`

T-026 should replace current frontend-only scaffolding with backend-backed behavior where needed.

Priority order:

1. Add backend knowledge import APIs for files and zip archives.
2. Add backend knowledge source delete or disable action.
3. Add backend memory store and memory-control APIs.
4. Add backend model/provider configuration read endpoint and safe admin write path.
5. Connect frontend RBAC decisions to backend governance roles and policy-rule responses.
6. Verify admin, operator, member, and viewer behavior end to end.

## Development Constraints

- Preserve the single-runtime orchestrator until governance and audit paths are stable.
- Do not split into async workers, queues, or multi-service agent runtimes before the roadmap calls for it.
- Keep all high-risk actions policy-checked and auditable.
- Do not store raw provider API keys in frontend localStorage.
- Browser UI must not pretend it can read arbitrary local paths without backend, desktop, or user-granted file access.
- Prefer small, explicit backend APIs over broad generic mutation endpoints.
- Keep the frontend visual language minimal: white background, black text, light borders, restrained gray copy, no decorative gradients.
- Keep agent replies human-readable first. Raw JSON should remain secondary and hidden behind disclosure UI where it is still needed for diagnostics.
- Do not render planner output as the chat answer. If the backend cannot answer, show a clear natural-language failure or no-evidence message.
- Match the local reference screenshots before adding decorative or dashboard-style UI elements.

## Local Run Commands

Backend:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-backend.ps1
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

## Documentation

- Task log: `docs/task-cards.md`
- Roadmap: `docs/phase-5-7-enterprise-roadmap.md`
- Startup guide: `README.md`
- Recovery guide: `AGENTS.md`
- Project context: `PROJECT_CONTEXT.md`
- Current state: `CURRENT_STATE.md`
- Task queue: `TASK_QUEUE.md`
- Decisions: `DECISIONS.md`
- Session handoff: `SESSION_HANDOFF.md`
