# Project Context

Last updated: 2026-04-11

This file is the long-lived project context for continuing development in a new environment.

## Purpose

Ops Agent Platform is a **single-tenant agent runtime** for one team's
Jira-tied development and Q&A workflows. The aspirational framing is
"enterprise agent platform," but the current implementation is honest
about what's MVP-grade vs what's production-grade — see "Honest scope"
below before quoting the headline.

Current product target:

- minimal white/black/gray AI workbench UI
- fixed left sidebar with navigation and conversation switching
- chat-first task and knowledge assistant flow
- repository-grounded answers with citations
- knowledge import and RAG management
- memory management
- model/provider settings
- login state and RBAC-aware controls
- governed backend task, tool, approval, policy, and audit paths

## Honest scope (what to claim vs what to caveat)

What is genuinely production-grade:
- Per-task audit log (event_log, payload_json, trace_id) — every step
  persisted with structured payload. Defensible "auditability" claim.
- Approval queue + policy snapshot on `Approval` rows — defensible
  "human-in-the-loop governance" claim.
- Provider fallback chain across codex/claude_code/anthropic/minimax —
  defensible "provider-agnostic" claim.
- Cross-family judge (Claude Code CLI judging MiniMax synthesis) — gives
  benchmark numbers without self-evaluation bias.

What needs explicit caveats:
- "Sandbox isolation": the develop pipeline copies the source repo
  into `data/sandboxes/<task_id>/` and runs `git apply` there. This is
  **filesystem-level isolation only** — the LLM running in the sandbox
  can still touch global git config, write to the user's home dir
  (`~/.ssh/`, `~/.gitconfig`), spawn arbitrary subprocesses, and reach
  the network. There is no process-level / capability-level sandbox.
  Claim "scoped working directory for code changes" — never claim
  "secure sandbox" or "enterprise isolation."
- "Multi-tenant": there is none. Single SQLite DB, single user model,
  no row-level security. The Approval / Governance rows have an
  `actor_role` field but it's enforced by application code, not the
  database.
- "Production scale": single SQLite + single ThreadPoolExecutor for
  background pipeline work. No durable job queue, no PostgreSQL, no
  Alembic migrations. Crash recovery is a startup orphan-sweep, not a
  resumable job runner.
- "Enterprise security": API keys in plaintext `.env`, codegen output
  not scanned for malicious patterns (eval, network calls), Q&A
  endpoint may not enforce governance roles consistently.

The right framing for an external audience: "MVP-quality single-tenant
agent runtime with production-grade auditability and governance hooks,
running against a small fixture repository. The components needed to
become a true multi-tenant enterprise platform (process sandboxing,
durable job queue, multi-tenant DB, secret management) are out of
scope for the current implementation."

## Current Architecture

- Backend: FastAPI, SQLAlchemy, SQLite by default.
- Frontend: React 18, Vite, React Router, TanStack Query.
- Runtime: single-runtime orchestrator.
- Persistence: tasks, events, approvals, governance metadata, tool executions, and indexed knowledge documents are backend persisted.
- Frontend scaffolding still uses localStorage for some login, conversation-title, memory, and model-choice behavior.

Keep the single-runtime architecture until backend persistence, RBAC enforcement, approval flows, and audit UI are stable.

## Important Paths

Repo-relative paths:

- `apps/backend`: FastAPI API, agents, orchestrator, persistence, tools.
- `apps/web`: React + Vite frontend.
- `docs/task-cards.md`: task card history.
- `docs/phase-5-7-enterprise-roadmap.md`: broader governance/UI/scale roadmap.
- `references/`: local UI screenshots used as visual reference.
- `scripts/`: Windows startup scripts.

External local reference repo:

- Handyman source is in the user's D-drive project folder under `HandymanApp-master`.
- Avoid storing the non-ASCII absolute D-drive path in recovery docs because some PowerShell output renders it as mojibake.

## Recovery Entry

For a new session, read in this order:

1. `AGENTS.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_STATE.md`
4. `DECISIONS.md`
5. `TASK_QUEUE.md`
6. `SESSION_HANDOFF.md`

Then output:

- current state summary
- next 3 concrete actions
- exact module scope you plan to touch

## Current Completed Work

- `T-024 Phase 5 Governance Data Model`: done.
- `T-025 Minimal AI Workbench Frontend Refactor`: done.
- `T-027 Resumable Development State Files`: done.
- `T-028 Fix Chat Knowledge Answer Chain`: done.
- `T-029 Strict Reference UI Pass`: done.
- `T-032 Same-Conversation Follow-up Turns`: done.
- `T-033 Environment Handoff Documentation`: done.

## Current Next Task

Next implementation task:

- `T-026 Workbench Backend Persistence and Governance Integration`

Primary scope:

- backend knowledge import endpoints for files and zip archives
- backend knowledge source delete/disable action
- backend memory store and memory-control APIs
- backend model/provider configuration read endpoint and safe admin write path
- connect frontend RBAC decisions to backend governance roles and policy-rule responses

## Known Gaps

- Knowledge upload UI is still a scaffold for backend import endpoints.
- Knowledge source delete currently hides in UI unless backend delete/disable is implemented.
- Memory management still relies on localStorage.
- Model/provider settings still rely on local state and masked fields; backend-managed settings are needed.
- RBAC checks exist in the frontend, but sensitive operations still need backend enforcement.
- Reference UI pass was build-verified, not screenshot-diff verified.
- Project folder is not currently a Git repository, so there is no branch/commit/stash evidence.

## Firebase Findings From Handyman Repo

Found in the Handyman source repo:

- Android Firebase config: `app/google-services.json`.
- Root Google services plugin classpath: `build.gradle`.
- App Google services plugin and Firebase dependencies: `app/build.gradle`.
- Realtime Database usage appears in Android source files including chatbox, job/customer fragments, KYC pages, `SupportForm.kt`, and `utils/FirebaseMetrics.kt`.

Not found:

- `database.rules.json`
- `firestore.rules`
- `storage.rules`
- `firebase.json`
- `.firebaserc`
- any `.rules` file

Inference:

- Firebase Realtime Database rules are not present in the local Handyman repo. They likely live in Firebase Console unless they are exported later.

## Verification Snapshot

Latest known verification:

- backend compile: passed via local Python interpreter
- frontend TypeScript app config: passed
- frontend TypeScript node config: passed
- frontend production build: passed outside sandbox because Vite/esbuild child-process spawn is blocked inside sandbox
- backend health: `http://127.0.0.1:8000/health` returned ok
- frontend root: `http://127.0.0.1:5173/` returned HTTP 200
- follow-up smoke: second turn reused the same `session_id`, remained `process_question`, completed, and cited `app/google-services.json`
