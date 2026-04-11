# Project Context

Last updated: 2026-04-11

This file is the long-lived project context for continuing development in a new environment.

## Purpose

Ops Agent Platform is an enterprise AI assistant and governed agent workflow platform.

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
