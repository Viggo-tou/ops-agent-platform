# Current State

Last updated: 2026-04-14

## 2026-04-14 Addendum: Pipeline Stability (T-034, docs-only commit)

- Six fixes landed in working tree for the `jira_issue_develop` flow (Fixes #1, #3, #4, #5, #6 + test sync). See `TASK_QUEUE.md` → T-034 and `SESSION_HANDOFF.md` → "Session 2026-04-14" for full manifest.
- End-to-end verified on real Jira project `TEST` (issue TEST-1, To Do → Done).
- Develop pipeline tests: 12/12 green.
- Residual debt: T-035 (7 pre-existing test failures in codegen provider resolver + tool approval gate), T-036 (Fix #2 safety net, demoted), T-037 (commit hygiene — only 1 commit in repo history).
- `AGENTS.md` and `CLAUDE.md` now enforce a mandatory session-start tag + session-end commit/manifest ritual to prevent session-boundary ambiguity from recurring.
- Baseline tag created: `session-start/2026-04-14-pipeline-fixes` → `940b232`.

## Baseline

- Repo path: current workspace root `Ops_agent_platform` under the user's `D:` project folder.
- Git evidence: this folder is currently not a Git repository; `git status --short` returns `fatal: not a git repository`.
- Backend: FastAPI app in `apps/backend`.
- Frontend: React + Vite app in `apps/web`.
- Architecture: single-runtime orchestrator with persisted tasks, events, approvals, governance metadata, knowledge tools, and frontend workbench scaffolding.
- Active UI reference folder: `references/`.

## Completed Recently

- `T-024 Phase 5 Governance Data Model`: done.
- `T-025 Minimal AI Workbench Frontend Refactor`: done.
- `T-027 Resumable Development State Files`: done.
- `T-028 Fix Chat Knowledge Answer Chain`: done.
- `T-029 Strict Reference UI Pass`: done.
- `T-032 Same-Conversation Follow-up Turns`: done.
- `T-033 Environment Handoff Documentation`: done.

Frontend files changed in T-025:

- `apps/web/src/styles.css`
- `apps/web/src/components/layout/AppShell.tsx`
- `apps/web/src/components/layout/ConversationList.tsx`
- `apps/web/src/pages/chat/ChatPage.tsx`
- `apps/web/src/components/chat/MessageList.tsx`
- `apps/web/src/pages/knowledge/KnowledgePage.tsx`
- `apps/web/src/components/knowledge/KnowledgeUploadPanel.tsx`
- `apps/web/src/components/knowledge/KnowledgeSourceList.tsx`
- `apps/web/src/components/memory/MemoryPanel.tsx`
- `apps/web/src/components/settings/ModelSelector.tsx`

Documentation files changed recently:

- `AGENTS.md`
- `PROJECT_CONTEXT.md`
- `CURRENT_STATE.md`
- `TASK_QUEUE.md`
- `DECISIONS.md`
- `SESSION_HANDOFF.md`
- `README.md`
- `CLAUDE.md`
- `docs/task-cards.md`
- `docs/phase-5-7-enterprise-roadmap.md`

## Current Runtime Evidence

Latest known checks:

- Backend health: `http://127.0.0.1:8000/health` returned `{"status":"ok"}`.
- Frontend: `http://127.0.0.1:5173/` returned HTTP `200`.
- T-025 frontend verification passed:
- `npm.cmd exec tsc -- --noEmit -p tsconfig.app.json`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.node.json`
- `npm.cmd run build`

## Resolved: Chat Answer Chain

User symptom:

- Asking from the frontend for `Locate Firebase configuration file(s) in the codebase` returned a plan-like response:
- `Answer the question with grounded evidence from the repository...`
- `Suggested next steps: Interpret the process question / Search and package mock knowledge / Review the knowledge answer`
- Conversation details showed `Status failed`, `Request type process_question`, `Review state needs_info`.

Current diagnosis:

- This is not only a frontend styling issue.
- Backend `process_question` generates a knowledge plan in `apps/backend/app/agents/service.py`.
- That plan's final output contract expects a `knowledge_answer` with `answer`, `citations`, and `answer_trace`.
- The reviewer can reject or mark the task failed when citations or required fields are missing.
- The orchestrator stores the failed result while still leaving the generated plan available.
- Frontend `apps/web/src/components/chat/MessageList.tsx` currently falls back to rendering `plan.change_explanation` and plan steps when no approved `KnowledgeSearchResult.answer` is available.
- Result: the user sees backend planning text instead of a final assistant answer.

Fix applied in T-028:

- Backend no-citation knowledge results now keep a non-empty packaged context and a user-facing answer.
- Firebase/configuration queries now better prioritize `google-services`, Firebase, JSON, Gradle, manifest, and properties evidence.
- Planner wording no longer says `mock knowledge`.
- `process_question` plans no longer stay blocked only because planner-provided `missing_information` was present.
- Backend failed knowledge output stores a user-facing message where possible.
- Frontend chat no longer renders `plan.change_explanation` as the normal answer for `process_question`.
- Chat no longer shows task status, request type, and review state as normal product content.

Verification:

- Backend compile passed with the local Python interpreter.
- Frontend TypeScript app and node checks passed.
- Frontend production build passed after running outside the sandbox because Vite/esbuild spawn was blocked in the sandbox.
- Direct knowledge search for `Locate Firebase configuration file(s) in the codebase` returned `app/google-services.json`.
- Full `POST /api/tasks` smoke for the same query completed with `status=completed`, `review_verdict=approved`, and answer citations including `app/google-services.json`.

## Reference UI Pass

The T-029 UI pass reworked the current frontend to more closely match the local screenshots in `references/`.

Reference notes:

- `2481bade51d3e7d707373f6be03a7cf.jpg`: chat layout with fixed left sidebar, search, recent conversations, RAG/memory toggles, centered chat width, black user bubble, white assistant card, bottom composer, and model selector in the header.
- `1867fbe3bfbafa7330059ab816c0b16.jpg`: knowledge page with centered title, top-right black upload button, embedding status card, dashed upload drop zone, and compact uploaded file list.
- `41bdeaeb682756b47d71c863b1a2cc5.jpg`: settings page with model/API tabs, provider chips, simple model rows, and a compact selected check state.
- `7b34a4116070faeb200f7f266b6ca87.jpg`: landing/home page with three simple product cards and a single black start button.
- `bfe834370c059d0b93de83d7098bc3f.jpg`: memory page with compact stats, search, and empty memory state.

T-029 changes:

- Added a `/home` entry surface with centered Knowledge Assistant content and three compact cards.
- Reworked the sidebar to a pale fixed column with start-chat action, search, nav, feature toggles, recent conversations, and account entry.
- Reworked chat header, model pill, message copy, composer placeholder, and debug-free message surface.
- Reworked knowledge page header, upload action, dashed upload panel, upload icon, and source status presentation.
- Reworked memory page header actions, stats cards, and editor anchor.
- Reworked settings into tabs, provider chips, and full-width model rows.
- Replaced the corrupted domestic-provider label in `ModelSelector.tsx` with ASCII provider text.

Residual risk:

- This pass was verified by TypeScript/build checks and local HTTP reachability, not by screenshot-level browser visual diffing.

## Current Active Priority

Next implementation task: `T-026 Workbench Backend Persistence and Governance Integration`.

## New Environment Handoff

T-033 changes:

- Added `PROJECT_CONTEXT.md` as the long-lived project context file.
- Updated recovery bootstrap lists to include `PROJECT_CONTEXT.md`.
- Rewrote local Handyman paths in recovery files as repo-relative or ASCII-only descriptions to avoid PowerShell mojibake.
- Captured completed tasks, active next task, verification evidence, Firebase findings, known gaps, and next module scope.

New-session read order:

1. `AGENTS.md`
2. `PROJECT_CONTEXT.md`
3. `CURRENT_STATE.md`
4. `DECISIONS.md`
5. `TASK_QUEUE.md`
6. `SESSION_HANDOFF.md`

## Same-Conversation Follow-up

T-032 changes:

- Frontend chat now reuses the current `session_id` when sending from an existing chat.
- Follow-up payloads include the previous user request and assistant answer as hidden context for the backend.
- `MessageList` renders only the visible follow-up request after the `Follow-up request:` marker.
- Sidebar conversations are grouped by `session_id`, so multiple turns do not appear as separate conversations.
- Backend task classification extracts the real follow-up request before classifying scenario/risk/title, preventing context words such as `issue` from routing the follow-up to Jira.

Verification:

- Frontend TypeScript app and node checks passed.
- Frontend production build passed outside the sandbox after Vite/esbuild spawn hit sandbox `EPERM`.
- Backend compile passed.
- Follow-up smoke created two tasks with the same `session_id`; the second turn `Give me the exact path.` was classified as `process_question`, completed, and returned citations including `app/google-services.json`.

## Firebase Source Findings

Local Handyman source path checked: the user's D-drive project folder under `HandymanApp-master`.

Found:

- Firebase Android config: `app/google-services.json`.
- Google services plugin classpath: `build.gradle`.
- Google services plugin and Firebase dependencies: `app/build.gradle`.
- Realtime Database usages are spread across Android source files, including `chatbox/MainActivity.kt`, job/customer fragments, KYC pages, `SupportForm.kt`, and `utils/FirebaseMetrics.kt`.

Not found:

- No `database.rules.json`, `firestore.rules`, `storage.rules`, `firebase.json`, `.firebaserc`, or `.rules` file was found under the local Handyman repo.
- The Firebase Realtime Database rules therefore appear to live outside this repo, most likely in Firebase Console, unless they have not been exported yet.
