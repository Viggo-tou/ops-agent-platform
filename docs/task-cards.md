# Task Card Log

This file is the append-only task card register for the Phase 0 / Phase 1 MVP.

## Working Rules

- Create one task card before starting each discrete implementation task.
- Keep task scope narrow and demo-oriented.
- Update task status in place: `planned`, `in_progress`, `done`, or `blocked`.
- Record verification notes when the task is closed.

## Task Card Template

```md
### T-XXX Task Name

Status: planned
Date: YYYY-MM-DD

Goal:
- ...

Scope:
- ...

Out of Scope:
- ...

Modules:
- ...

Input:
- ...

Output:
- ...

Acceptance Criteria:
- ...

Risks / Dependencies:
- ...

Verification:
- ...

Result:
- ...
```

## Cards

### T-000 Task Card Log Setup

Status: done
Date: 2026-04-10

Goal:
- Add a persistent markdown log for task cards so every implementation task can be tracked.

Scope:
- Create a dedicated task card log file in `docs/`
- Define a reusable task card template
- Record the setup task itself as the first entry

Out of Scope:
- Project implementation work
- Task automation
- Status dashboards

Modules:
- `docs/task-cards.md`

Input:
- User requirement to record every task card in a standalone markdown file

Output:
- A shared markdown log for planned, active, and completed task cards

Acceptance Criteria:
- The repository contains one markdown file dedicated to task card logging
- The file includes a standard template for future tasks
- The file includes at least one recorded task card entry

Risks / Dependencies:
- Future tasks need consistent manual updates to keep the log useful

Verification:
- Confirm `docs/task-cards.md` exists and contains the template and first task entry

Result:
- Task card logging file created and ready for future tasks

### T-001 Backend Foundation and Core Task APIs

Status: done
Date: 2026-04-10

Goal:
- Create the Phase 1 backend foundation needed to accept requests, persist tasks and events, and expose task visibility APIs.

Scope:
- Bootstrap a FastAPI backend app structure
- Define core enums, models, and schemas for `task`, `event`, and `approval`
- Implement task creation, task list/detail, and task event APIs
- Add a minimal orchestrator stub that records planning-related events

Out of Scope:
- Frontend pages
- Real Jira or other external integrations
- Multi-agent runtime separation
- Full approval workflow execution

Modules:
- `apps/backend/`
- `docs/task-cards.md`

Input:
- Phase 0 and Phase 1 scope documents
- MVP architecture constraints from `CLAUDE.md`

Output:
- A runnable backend codebase skeleton
- Persistent core data models
- Minimal REST APIs for tasks and events
- Initial orchestrator flow with structured event recording

Acceptance Criteria:
- A request can create a task
- A created task persists a lifecycle record and planning event(s)
- Task list and task detail endpoints return stable structured data
- Task events endpoint returns chronological event history

Risks / Dependencies:
- Need a minimal database configuration that works locally without overcomplicating deployment
- Need to keep the orchestrator simple enough to match Phase 1 scope

Verification:
- Start the API locally
- Call task creation and read APIs
- Confirm tasks and events are stored and returned

Result:
- FastAPI backend skeleton created under `apps/backend`
- Core `task`, `event`, and `approval` models implemented with persistent SQLite-backed storage by default
- Added task creation, task list/detail, event history, approval grant/reject, and task rollback APIs
- Added a single-runtime orchestrator with mock planning, mock knowledge retrieval, mock Jira draft creation, and approval-gated action execution
- Verified with local smoke tests:
- `POST /api/tasks` returns `201`
- Jira draft flow reaches `completed`
- Approval-required flow reaches `waiting_approval`
- `GET /api/tasks/{id}/events` returns chronological event history
- `POST /api/approvals/{id}/grant` completes the task
- `POST /api/tasks/{id}/rollback` marks the task as `rolled_back`

### T-002 Frontend MVP Task Console

Status: done
Date: 2026-04-10

Goal:
- Build the Phase 1 frontend task console so users can submit requests, browse tasks, and inspect task plans and event logs.

Scope:
- Bootstrap a React frontend app structure in `apps/web`
- Implement task submission, task list, and task detail/log pages
- Connect the pages to the backend task, event, approval, and rollback APIs
- Add minimal enterprise-console styling and clear task state visibility

Out of Scope:
- Authentication and RBAC UI
- Real-time streaming updates
- Complex design system setup
- Multi-workspace navigation

Modules:
- `apps/web/`
- `apps/backend/` if small API support changes are needed
- `docs/task-cards.md`

Input:
- Existing Phase 1 backend APIs
- Phase 0 / Phase 1 MVP scope constraints

Output:
- A runnable React frontend for the core task workflow
- Task submission flow connected to the backend
- Task dashboard views for list, plan, approvals, and event history

Acceptance Criteria:
- A user can submit a request from the frontend
- The task list page shows persisted tasks and statuses
- The task detail page shows task metadata, generated plan, latest result, approvals, and event log
- The frontend handles loading and error states without breaking navigation

Risks / Dependencies:
- Need a lightweight setup that can be verified quickly in the current environment
- May require small backend CORS or response-shape adjustments during integration

Verification:
- Install frontend dependencies
- Build the frontend
- Run a local integration smoke test against the backend APIs

Result:
- React + Vite frontend scaffold created under `apps/web`
- Added routed MVP pages for task submission, task list, and task detail/event log
- Connected the frontend to backend task, event, approval, and rollback APIs
- Added approval action controls and rollback controls on the task detail page
- Added enterprise-console styling focused on visibility, auditability, and task state clarity
- Added backend CORS support for the local frontend dev origin
- Installed frontend dependencies and generated `package-lock.json`
- Verified frontend build with `npm.cmd run build`
- Verified backend still compiles after the frontend integration support changes

### T-003 Local Runbook and Startup Scripts

Status: done
Date: 2026-04-10

Goal:
- Make the Phase 1 MVP easy to open and run locally with clear root-level instructions and reusable startup scripts.

Scope:
- Add a root `README.md` with backend/frontend setup and run steps
- Add Windows PowerShell startup scripts for backend and frontend
- Keep the scripts simple and compatible with the current local environment

Out of Scope:
- Production deployment
- Docker or container setup
- CI/CD pipelines
- Cross-platform shell support beyond the current Windows environment

Modules:
- `README.md`
- `scripts/`
- `docs/task-cards.md`

Input:
- Current backend and frontend project structure
- Existing local dependency setup from T-001 and T-002

Output:
- One root document that explains how to start and open the MVP
- Reusable local scripts to start backend and frontend dev servers

Acceptance Criteria:
- The repository root contains a readable startup guide
- Backend and frontend can each be started from dedicated scripts
- The user can identify the exact browser URLs to open

Risks / Dependencies:
- Python launcher and local interpreter paths differ by environment
- PowerShell execution policy may affect direct script execution

Verification:
- Check the startup commands used by the scripts
- Verify the referenced local URLs and commands match the implemented apps

Result:
- Added a root `README.md` with first-time setup, startup, direct commands, and local URLs
- Added `scripts/setup-local.ps1` for dependency installation
- Added `scripts/start-backend.ps1` for the FastAPI dev server
- Added `scripts/start-web.ps1` for the Vite dev server
- Added `scripts/common.ps1` to resolve the local Python and npm executables more reliably on Windows
- Verified the scripts in `-PrintOnly` mode and confirmed the generated commands and working directories
- Documented the exact browser URLs to open for the frontend app and backend docs

### T-004 Local Runtime Readiness and Startup Debugging

Status: done
Date: 2026-04-10

Goal:
- Make the current MVP directly openable by starting the local services and fixing any runtime issues that prevent access.

Scope:
- Diagnose why the app cannot currently be opened
- Start backend and frontend services locally
- Verify the backend API and frontend app are reachable on their expected localhost URLs
- Apply small runtime fixes if startup or reachability problems are found

Out of Scope:
- New product features unless they are required to make the runtime usable
- Production hosting or deployment

Modules:
- `apps/backend/`
- `apps/web/`
- `scripts/`
- `docs/task-cards.md`

Input:
- Existing local scripts and startup commands
- Current backend and frontend codebases

Output:
- Running local backend and frontend services
- Verified local URLs for browser access

Acceptance Criteria:
- Backend responds on `http://127.0.0.1:8000`
- Frontend responds on `http://127.0.0.1:5173`
- The user can open the frontend and backend docs in a browser

Risks / Dependencies:
- Long-running local processes need to be started in a way that survives this session step
- Local port conflicts may require adjustment

Verification:
- Launch both services
- Check local HTTP responses for backend and frontend URLs

Result:
- Diagnosed the original startup blockers:
- backend `--reload` triggered Windows multiprocessing permission failures in this environment
- frontend Vite local server modes were blocked by `esbuild` child-process restrictions in the sandbox
- Updated backend startup to run without reload by default, with `-Reload` kept as an explicit option
- Replaced the frontend default runtime with a lightweight Python static server for the built `dist/` output, with `-Dev` kept as an explicit option
- Added `scripts/serve-web.py` with SPA fallback support
- Launched backend and frontend outside the sandbox so the processes stay alive
- Verified:
- `http://127.0.0.1:8000/health` returns `{"status":"ok"}`
- `http://127.0.0.1:5173` returns HTTP `200`

### T-005 Dashboard Live Visibility Improvements

Status: done
Date: 2026-04-10

Goal:
- Improve the Phase 1 dashboard so task status visibility feels live and demo-ready without adding architectural complexity.

Scope:
- Add summary status cards to the task list page
- Add automatic refresh for task list and task detail/event views
- Surface last-refresh timing or live-state cues where useful

Out of Scope:
- WebSocket streaming
- Background workers
- Real-time collaborative presence

Modules:
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing task list and task detail pages
- Existing backend task and event APIs

Output:
- A more informative and self-updating dashboard experience

Acceptance Criteria:
- The task list page shows aggregate visibility of current task states
- The task list and task detail pages refresh automatically without manual reload
- The UI clearly indicates that data is live or recently refreshed

Risks / Dependencies:
- Need to avoid aggressive polling that makes the UI noisy
- Must keep the implementation inside current MVP scope

Verification:
- Build the frontend after the changes
- Check that status cards render from live task data
- Check that list/detail queries are configured to refresh automatically

Result:
- Added aggregate status metric cards to the task list dashboard
- Added automatic refresh polling to:
- task submission page recent-task panel
- task list page
- task detail page
- event timeline view through the task detail page queries
- Added last-sync visibility hints so the UI signals that task data is live
- Rebuilt the frontend bundle and confirmed the running local app serves the updated `dist/`
- Verified local URLs remain reachable after the update:
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:5173`

### T-006 LLM-Backed Primary Agent Integration

Status: done
Date: 2026-04-10

Goal:
- Upgrade the current primary agent path from purely deterministic planning to an optional real LLM-backed planning flow while preserving the existing MVP runtime.

Scope:
- Add a provider abstraction for the primary agent
- Support real LLM plan generation when environment configuration is present
- Keep a deterministic fallback when the provider is not configured or fails
- Persist the generated plan and agent metadata through the existing task/event flow

Out of Scope:
- Multi-agent runtime separation
- Streaming token responses
- Tool-calling loops beyond the current Phase 1 mock action path
- Real external Jira / Slack / Notion integrations

Modules:
- `apps/backend/`
- `docs/task-cards.md`

Input:
- Existing single-runtime orchestrator flow
- Current task/event/approval persistence model

Output:
- A configurable primary agent planning layer that can use a real LLM or fallback logic

Acceptance Criteria:
- Task creation still works without an LLM API key
- When agent configuration is present, the orchestrator can request a structured plan from the provider
- The resulting plan and provider metadata are visible through the existing task detail and event views

Risks / Dependencies:
- Need to preserve current reliability if the upstream model call fails
- Need to keep the provider interface narrow enough for Phase 1 scope

Verification:
- Run backend checks with provider fallback mode
- Verify tasks still create and plans still persist
- Verify provider metadata appears in plan/event payloads

Result:
- Added a dedicated primary agent planning layer under `app/agents/`
- Added structured plan models for normalized plan generation and persistence
- Added optional OpenAI-backed planning through the Responses API with JSON-schema output
- Added safe fallback behavior when the provider is disabled, unconfigured, or errors
- Persisted provider metadata in `plan_json` and `plan_generated` event payloads
- Added backend configuration for provider selection, model selection, API key, base URL, and timeout
- Added `apps/backend/.env.example` for primary agent configuration
- Verified fallback mode with live task creation
- Verified explicit OpenAI-provider mode without an API key falls back safely and records the reason
- Restarted the running backend so the new primary agent code is active on port `8000`

### T-007 Primary Agent Provider Visibility in Dashboard

Status: done
Date: 2026-04-10

Goal:
- Make it obvious in the dashboard whether a task plan was produced by the mock planner or a real OpenAI-backed primary agent, and whether fallback was used.

Scope:
- Add frontend types for plan provider metadata
- Surface provider state in the task list and task detail views
- Highlight fallback conditions without forcing users to inspect raw plan JSON

Out of Scope:
- Backend schema changes unless strictly required
- New planning behavior
- Streaming traces or token-level diagnostics

Modules:
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing `plan_json.provider` payload
- Existing `plan_generated` event payload metadata

Output:
- Clear provider badges and fallback visibility in the dashboard

Acceptance Criteria:
- The task list page exposes plan provider visibility per task
- The task detail page shows provider, mode, model, and fallback state when present
- The frontend still builds cleanly after the update

Risks / Dependencies:
- Need to handle missing provider metadata for older tasks gracefully

Verification:
- Rebuild the frontend
- Confirm provider badges render correctly for current tasks
- Confirm fallback state is visible for mock/openai paths

Result:
- Added backend summary fields for `plan_provider_name`, `plan_provider_mode`, `plan_model_name`, `plan_used_fallback`, and `plan_fallback_reason`
- Added frontend provider badge UI for task list and task detail views
- Added explicit provider details and fallback warning visibility to the task detail page
- Rebuilt the frontend bundle so the running local dashboard serves the updated UI
- Restarted the backend so the running API exposes the new provider summary fields
- Verified live task creation returns provider visibility fields, including:
- `plan_provider_name=mock`
- `plan_provider_mode=deterministic_planner`
- `plan_used_fallback=false`

### T-008 Explicit Session Correlation for Tasks and Events

Status: done
Date: 2026-04-10

Goal:
- Make session event tracking explicit by adding a first-class `session_id` to task creation and event persistence.

Scope:
- Add `session_id` fields to the task and event models
- Allow task creation to accept an optional `session_id` and auto-generate one when missing
- Persist the same `session_id` across all events for a task
- Expose `session_id` in API responses and dashboard views
- Add a lightweight local migration path for the existing SQLite database

Out of Scope:
- Separate session table
- Multi-task session threads
- Conversation history storage beyond current event records

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing task and event persistence model
- Current running SQLite database

Output:
- Tasks and events with explicit session correlation

Acceptance Criteria:
- A new task has a `session_id`
- The task's recorded events carry the same `session_id`
- The API returns `session_id`
- The dashboard shows `session_id` in task views

Risks / Dependencies:
- Existing local SQLite DB needs a safe additive migration path

Verification:
- Create a new task and verify a single `session_id` appears on both task and events
- Rebuild the frontend after the session visibility changes

Result:
- Added explicit `session_id` persistence to tasks and events, with auto-generation when task creation does not provide one
- Exposed `session_id` in task and event API responses so the dashboard can correlate a durable session trail
- Added a lightweight additive SQLite schema upgrade path so existing local databases gain the new columns safely
- Updated the dashboard task list, task detail view, and event timeline to display `session_id`
- Verified the live backend returns a single shared `session_id` across a newly created task and all 8 recorded events
- Rebuilt the frontend bundle after the dashboard session visibility changes

### T-009 Task Dashboard Filters for Session Tracking

Status: done
Date: 2026-04-10

Goal:
- Make the dashboard easier to operate by adding first-class task filters for `session_id`, task status, and plan provider.

Scope:
- Add optional filter params to the task list API
- Add dashboard controls for text search, `session_id`, status, and provider filtering
- Keep the list auto-refresh behavior compatible with active filters

Out of Scope:
- Dedicated session details page
- Saved filter presets
- Event stream pagination

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing task list API and task dashboard
- Existing session/provider metadata on tasks

Output:
- Filterable task list for session-oriented tracking

Acceptance Criteria:
- Users can filter tasks by `session_id`, status, and provider from the dashboard
- The task list API accepts matching optional query params
- The frontend build still passes after the filter changes

Risks / Dependencies:
- Need to keep filtering behavior stable for older tasks with missing provider metadata

Verification:
- Call the task list API with filters and confirm the result set changes as expected
- Rebuild the frontend and confirm the dashboard filter controls work with the updated API

Result:
- Added optional `search`, `session_id`, `status`, and `provider` filters to `GET /api/tasks`
- Updated the dashboard task list to use API-backed filtering with controls for text search, `session_id`, status, and plan provider
- Added a clear-filters action and empty-state copy that distinguishes between an empty system and a filtered-out view
- Rebuilt the frontend bundle so the running static dashboard serves the new filter controls
- Restarted the backend so the running API exposes the updated filter parameters
- Verified live filtering by creating a task and matching it successfully through `status`, `session_id`, `provider`, and `search` queries

### T-010 Phase 2 Planner and Reviewer Design Spec

Status: done
Date: 2026-04-10

Goal:
- Define the Phase 2 multi-role workflow for `primary -> planner -> reviewer -> execution` before implementation starts.

Scope:
- Define the structured plan schema emitted by the planner role
- Define the structured review schema emitted by the reviewer role
- Define the Phase 2 task status transitions and their meaning
- Capture the role responsibilities and orchestration flow in one design doc

Out of Scope:
- Code implementation of planner and reviewer roles
- New frontend screens
- Real policy engine or external tool integrations

Modules:
- `docs/`
- `docs/task-cards.md`

Input:
- Current Phase 1 single-runtime architecture
- User requirements for Phase 2 planner and reviewer roles

Output:
- A Phase 2 design document that can be used as the implementation baseline

Acceptance Criteria:
- The doc clearly defines `plan schema`, `review schema`, and `status transitions`
- The doc explains how `primary`, `planner`, and `reviewer` interact in Phase 2
- The design stays aligned with the current single-runtime orchestrator approach

Risks / Dependencies:
- Need to avoid over-designing beyond the current Phase 2 scope

Verification:
- Review the doc against the requested Phase 2 flow and required outputs

Result:
- Added the Phase 2 design spec at `docs/phase-2-planner-reviewer.md`
- Defined the planner output contract as a structured `plan schema` stored in `task.plan_json`
- Defined the reviewer output contract as a structured `review schema` for pre-execution and post-execution checks
- Defined the Phase 2 task status set and transition rules: `created`, `planning`, `reviewing`, `awaiting_approval`, `executing`, `completed`, `failed`
- Kept the design aligned with the current single-runtime orchestrator approach, with `planner` and `reviewer` modeled as internal role executors

### T-011 Phase 2 Planner and Reviewer Runtime

Status: done
Date: 2026-04-10

Goal:
- Implement the first runnable Phase 2 workflow with `primary -> planner -> reviewer -> execution` inside the existing single runtime.

Scope:
- Update task statuses and transitions to the Phase 2 set
- Add structured planner and reviewer documents to the backend
- Update orchestrator flow to run planner review before execution
- Update approval handling for the new `reviewing/awaiting_approval/executing` path
- Expose the latest review outcome in task responses and the dashboard

Out of Scope:
- Separate planner/reviewer services
- Real external tools
- New dashboard pages

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Phase 2 design spec
- Existing Phase 1 single-runtime implementation

Output:
- A working Phase 2 runtime with planner and reviewer stages

Acceptance Criteria:
- A task moves through the new Phase 2 statuses
- The backend returns structured `plan` and `review` data
- Approval-required tasks pause after review and resume into execution when granted
- The dashboard shows the latest review outcome and updated statuses

Risks / Dependencies:
- Existing local SQLite data may contain old status values and stale rows
- Frontend badges and filters must stay aligned with the new status set

Verification:
- Run backend compile and local API smoke tests for normal and approval paths
- Rebuild the frontend
- Verify the live app still opens and exposes the new task state/review data

Result:
- Reworked the runtime into a Phase 2 flow with `planning -> reviewing -> executing` and `awaiting_approval` as a review gate
- Added structured `phase2.plan.v1` and `phase2.review.v1` documents for planner and reviewer output
- Updated approval grant handling so approved tasks resume execution through the same orchestrator path instead of a separate Phase 1 shortcut
- Added `review_json`, `review_verdict`, `review_stage`, and `review_summary` to task responses and dashboard views
- Updated dashboard filters and status badges to match the new Phase 2 state model
- Verified backend compile, local API smoke tests, frontend rebuild, live backend restart, and live knowledge plus approval task flows

### T-012 Review Findings and Policy Visualization

Status: done
Date: 2026-04-10

Goal:
- Make reviewer output readable in the dashboard by visualizing findings, policy checks, and approval requirements instead of relying on raw JSON alone.

Scope:
- Add typed frontend parsing for the structured review document
- Add a review visualization component to the task detail page
- Keep raw review JSON available as a secondary debug view

Out of Scope:
- Backend API changes
- New standalone review pages
- Editable review actions from the UI

Modules:
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing `review_json` returned by the task detail API
- Current Phase 2 task detail page

Output:
- A readable review panel in the dashboard

Acceptance Criteria:
- The task detail page shows review findings, policy checks, and approval requirements in structured sections
- The frontend build still passes after the new review component is added
- Existing task detail behavior stays intact

Risks / Dependencies:
- Need to handle missing or partial review documents gracefully

Verification:
- Rebuild the frontend
- Verify the live task detail page still loads with current review-bearing tasks

Result:
- Added typed frontend parsing for the structured Phase 2 review document
- Added a dedicated review visualization component that renders findings, policy checks, approval requirements, and missing information
- Updated the task detail page to show structured reviewer output first and keep raw review JSON as a secondary debug view
- Kept the task list and existing detail behaviors intact while making reviewer output easier to scan
- Rebuilt the frontend bundle and verified the live dashboard and backend remain reachable

### T-013 Phase 3 Knowledge Agent and Basic Repository RAG

Status: done
Date: 2026-04-10

Goal:
- Add a basic Knowledge Agent flow backed by the local handyman codebase, with document ingestion, metadata, retrieval, citations, answer trace, and reviewer risk signals.

Scope:
- Add knowledge document persistence and ingestion from a local source path
- Add metadata capture and top-k repository retrieval
- Replace mock `knowledge.search` with repository-backed retrieval
- Include citations and answer trace in knowledge results
- Let reviewer surface higher hallucination risk when citations are weak
- Add minimal knowledge APIs and dashboard visibility for the new trace data

Out of Scope:
- Advanced chunking strategies
- Dedicated vector database
- Multi-source routing beyond the initial handyman repository
- Automatic code modification in the source repository

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 2 planner/reviewer runtime
- Local handyman repository as the first knowledge source

Output:
- A working basic repository-backed RAG path for code/debug style requests

Acceptance Criteria:
- The system can ingest the local handyman repository into the platform knowledge store
- `knowledge.search` returns metadata, top-k citations, and answer trace
- Knowledge-backed tasks expose source citations in their output
- Reviewer output reflects higher risk when citation coverage is weak

Risks / Dependencies:
- Need to keep ingestion lightweight and avoid large-file or binary noise
- The handyman source path may differ across environments and must remain configurable

Verification:
- Run backend compile and knowledge retrieval smoke tests
- Rebuild the frontend
- Verify the live app can answer a repository-backed question with citations

Result:
- Added a `knowledge_document` table and repository ingestion flow for the local handyman codebase
- Added configurable knowledge source settings with automatic fallback to the detected local handyman repository path
- Added repository sync and search APIs with metadata, top-k retrieval, citations, and answer trace
- Replaced mock `knowledge.search` with repository-backed retrieval through the tool gateway
- Updated the knowledge output contract to require `answer`, `citations`, and `answer_trace`
- Extended reviewer output checks so knowledge answers surface higher hallucination risk when citation grounding is weak
- Added structured knowledge result visibility in the dashboard, including citation snippets and answer trace
- Verified backend compile, repository sync, knowledge search, frontend rebuild, live backend restart, and a live knowledge-backed task flow against the handyman repository

### T-014 Multi-Source Knowledge Routing and Reviewer Grounding Checks

Status: done
Date: 2026-04-10

Goal:
- Strengthen the Phase 3 knowledge path by supporting multiple repository sources, clearer query routing, richer answer trace, and reviewer checks that look at retrieval relevance instead of citation count alone.

Scope:
- Add configurable multi-source knowledge repository support
- Add source-scoped sync and search APIs
- Improve repository retrieval with lightweight route-aware scoring
- Expand answer trace with source selection, route metadata, matched tokens, and relevance metrics
- Tighten reviewer checks for weakly grounded knowledge answers
- Show the richer trace data in the dashboard

Out of Scope:
- Full vector database adoption
- Advanced chunking pipelines
- Automatic code fixing inside the knowledge repositories
- Independent Knowledge Agent runtime

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 3 repository-backed knowledge flow
- Local handyman repository as the default code/debug knowledge source

Output:
- A more reliable and traceable knowledge retrieval path for code debugging questions

Acceptance Criteria:
- Knowledge sources can be configured as one or more named repositories
- Knowledge APIs can sync and search by source
- Retrieval returns route metadata and relevance metrics in the answer trace
- Reviewer raises warnings when citations exist but retrieval relevance is weak
- The dashboard shows the richer knowledge trace without breaking existing task detail flows

Risks / Dependencies:
- Repository paths remain environment-specific and must stay configurable
- Lightweight scoring must stay understandable and cheap enough for local indexing and search

Verification:
- Run backend compile
- Run local API checks for source listing, source sync, knowledge search, and knowledge-backed task execution
- Rebuild the frontend
- Restart the live backend and verify the updated knowledge routes through the running app

Result:
- Added configurable multi-source repository support through `OPS_AGENT_KNOWLEDGE_SOURCE_SPECS`, while keeping the single-source path configuration as a simple default
- Added source-scoped knowledge APIs for sync, document listing, and search, plus a new source listing endpoint
- Reworked repository retrieval with lightweight query routing for code/debug scenarios such as test failures, Android resource issues, build configuration problems, and general debugging
- Expanded the answer trace with `selected_sources`, `route_kind`, `route_reason`, `matched_tokens`, `token_coverage`, and `top_score`
- Updated citations to carry `source_name` so cross-repository provenance stays explicit
- Tightened reviewer grounding checks so weak token coverage or low retrieval score can raise relevance warnings even when citations are present
- Updated the dashboard knowledge panel to show routing, coverage, score, selected sources, and source-qualified citations
- Updated `.env.example` and the root `README.md` to document single-source and multi-source configuration
- Verified backend compile, local knowledge API checks, frontend rebuild, live backend restart, and a live knowledge-backed task flow against the handyman repository

### T-015 Natural-Language Knowledge Answers

Status: done
Date: 2026-04-10

Goal:
- Make knowledge answers readable for end users by returning natural-language guidance first, while still preserving citations and traceability for technical review.

Scope:
- Rewrite the knowledge answer summary into plain-language guidance
- Add route-aware next-step recommendations for debug-style questions
- Improve the dashboard answer presentation so the natural-language summary is easy to scan

Out of Scope:
- Replacing structured citations or answer trace
- Full conversational answer generation with a separate LLM response synthesizer
- Broad localization across the entire dashboard

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing repository-backed knowledge answers and dashboard rendering

Output:
- A clearer, user-facing knowledge answer format

Acceptance Criteria:
- Knowledge answers lead with a plain-language explanation instead of only route names and file references
- The dashboard presents the answer as readable paragraphs or sections
- Existing citations and trace data remain available for audit and reviewer checks

Risks / Dependencies:
- The answer must stay grounded in retrieved citations and avoid overstating certainty

Verification:
- Run backend compile
- Run a local knowledge search smoke test
- Rebuild the frontend
- Verify a live knowledge-backed task shows the new answer format

Result:
- Reworked the repository-backed knowledge answer so it now leads with a plain-language recommendation instead of only route names, file paths, and risk labels
- Added route-aware next-step guidance for test failures, Android resource issues, build configuration problems, and general code debugging
- Added query-language handling so Chinese requests receive Chinese natural-language answers while English requests keep English phrasing
- Updated the dashboard knowledge answer panel to render readable paragraphs and step lists instead of collapsing the answer into a single dense block
- Preserved the existing citation list, answer trace, and reviewer grounding checks so the answer remains auditable
- Verified backend compile, local English and Chinese knowledge-search smoke tests, frontend rebuild, live backend restart, live knowledge API verification, and a full Chinese task flow through `POST /api/tasks`

### T-016 User-First Knowledge Detail Layout

Status: done
Date: 2026-04-10

Goal:
- Make the knowledge task detail page easier for non-technical users by keeping the natural-language answer visible first and hiding technical retrieval detail behind collapsible sections.

Scope:
- Keep the user-readable answer at the top of the knowledge result panel
- Add a concise evidence summary near the answer
- Collapse answer trace and citation detail by default

Out of Scope:
- Changing backend schemas
- Removing citations or trace data
- Redesigning the entire task detail page

Modules:
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 3 knowledge result panel and natural-language answer format

Output:
- A cleaner knowledge result layout for business and product users

Acceptance Criteria:
- The first visible block is the natural-language answer
- Technical retrieval metrics and raw evidence are hidden until expanded
- Citation data is still available when the user wants to inspect grounding

Risks / Dependencies:
- Need to keep the collapsed sections discoverable so technical reviewers can still audit answers quickly

Verification:
- Rebuild the frontend
- Verify the live knowledge task page still loads and shows the new layout

Result:
- Kept the natural-language answer as the first visible block in the knowledge result panel
- Added a short evidence summary showing citation count, source coverage, confidence level, and the primary reference without exposing the full retrieval trace immediately
- Moved grounding metrics and answer-trace detail into a default-collapsed technical section
- Moved repository citations and code snippets into a separate default-collapsed evidence section
- Added supporting styles for the new answer-first card, evidence summary, and collapsible technical panels
- Verified the frontend rebuild and confirmed the live dashboard is still reachable after the layout change

### T-017 Phase 4 Action Agent and Real Tool Runtime

Status: done
Date: 2026-04-10

Goal:
- Start Phase 4 by replacing the mock tool gateway with a real tool runtime that can execute Slack, Jira, and internal enterprise connectors with registry-based permissions, execution logs, and failure controls.

Scope:
- Add a tool registry and configurable permission mapping
- Add a real tool gateway with retry, timeout, and failure handling
- Add real connector adapters for Slack, Jira, and internal API plus a guarded internal DB query tool
- Add tool execution logging and APIs for registry plus per-task execution logs
- Update planner, reviewer, and orchestrator to use the new tool model
- Show tool execution logs in the dashboard task detail page

Out of Scope:
- End-user credential management UI
- Full OAuth installation flows for Slack or Jira
- Background worker queues
- Bulk tool orchestration across multiple tasks

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 2 planner/reviewer runtime
- Existing Phase 3 knowledge retrieval path

Output:
- A Phase 4 tool execution foundation with real enterprise connectors and auditable runtime behavior

Acceptance Criteria:
- The system exposes a registry of supported tools with effective permission categories
- Slack, Jira, and internal connectors are available through the unified tool gateway
- Tool execution attempts are logged with request, response, status, timing, and retry metadata
- Reviewer can enforce approval requirements from the effective tool permission mapping
- Task detail pages can show per-task tool execution logs

Risks / Dependencies:
- Real external tools depend on environment configuration and valid credentials
- Retry behavior must avoid re-sending unsafe requests without clear policy boundaries

Verification:
- Run backend compile
- Run local API smoke tests for registry, task flows, and execution logs
- Rebuild the frontend
- Verify the live app still opens and shows tool execution data

Result:
- Replaced the old mock-only tool gateway with a unified Phase 4 `ToolGateway` backed by a registry, real connector adapters, retry handling, timeout handling, and per-execution audit logs
- Added a `tool_execution` table to persist tool name, provider, permission category, status, attempts, timeout, timing, request payload, response payload, and failure details
- Added a tool registry with effective permission mapping and runtime enablement for `knowledge.search`, `slack.post_message`, `jira.create_issue`, `internal_api.request`, and `internal_db.query`
- Added real connector implementations for Slack, Jira, internal API, and guarded read-only internal DB queries, while keeping repository-backed knowledge search in the same tool runtime
- Added retry and timeout event handling through `tool_retry_scheduled` and `tool_timed_out` runtime events
- Added a deterministic `ActionAgent` to build connector payloads for Slack, Jira, internal API, internal DB, and knowledge requests
- Updated request classification so Slack, Jira, internal API, internal DB, and knowledge requests route to different Phase 4 scenarios without substring misclassification such as `debug` accidentally matching `bug`
- Updated planner fallback behavior and OpenAI planning instructions so plans now reference real tool names and output contracts instead of the old mock Jira and generic approved-action tools
- Updated reviewer plan validation so it checks the actual registered tool scope, effective permission category, and whether the connector is configured in the current environment before allowing execution
- Added `GET /api/tools/registry` and `GET /api/tasks/{task_id}/tool-executions`
- Updated the task detail page to fetch and show per-task tool execution logs, including attempts, timing, request/response payloads, and failures
- Updated `.env.example` and `README.md` with Phase 4 connector configuration variables and permission override settings
- Verified backend compile, local API smoke tests for the registry plus tool-execution logging, frontend rebuild, live backend restart, live registry access, live knowledge-task execution logging, live safe Jira-plan rejection when credentials are missing, and live frontend availability

### T-018 Tool Readiness and Slack/Jira Operator UX

Status: done
Date: 2026-04-10

Goal:
- Make Phase 4 easier to operate by exposing tool readiness reasons in the dashboard and improving the Slack/Jira request experience for the first real enterprise tool flows.

Scope:
- Extend the tool registry response with readiness reasons and missing configuration details
- Show tool readiness in the frontend dashboard
- Add better starter prompts and submission guidance for Slack and Jira requests
- Improve Slack and Jira payload shaping so task requests map more cleanly to real connector calls

Out of Scope:
- Credential entry forms
- OAuth installation flows
- Full admin configuration UI

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 4 tool runtime, registry API, and task submission page

Output:
- A clearer Phase 4 operator experience for Slack and Jira readiness and request submission

Acceptance Criteria:
- The registry tells the user why a tool is disabled, not just that it is disabled
- The dashboard surfaces current connector readiness and missing configuration
- The submit page includes Slack and Jira oriented request examples
- Slack and Jira payload building stays stable for common request phrasing

Risks / Dependencies:
- Readiness messaging must stay accurate and not imply a tool is executable when critical credentials are still missing

Verification:
- Run backend compile
- Run local API checks for the enriched registry payload
- Rebuild the frontend
- Verify the live dashboard shows tool readiness state

Result:
- Extended the tool registry payload so each tool now reports `status_message` and `missing_configuration`, not just an `enabled` flag
- Updated the backend registry logic to explain exactly why Slack, Jira, internal API, or internal DB connectors are blocked in the current environment
- Improved Slack payload shaping so requests with a `#channel` and message body map more cleanly to `slack.post_message`
- Improved Jira payload shaping so requests with `bug`, `story`, `task`, and `project OPS` style wording map more cleanly to `jira.create_issue`
- Added a reusable frontend tool readiness panel and surfaced it on both the task submit page and task list dashboard
- Updated the submit page starter prompts so Slack and Jira are now first-class examples instead of only knowledge and approval prompts
- Updated the submit page guidance text so operators know how to phrase Slack and Jira requests for the current Action Agent heuristics
- Verified backend compile, local registry smoke tests, frontend rebuild, live backend restart, live registry readiness output, and live frontend availability

### T-019 Jira Issue Planning Flow

Status: done
Date: 2026-04-10

Goal:
- Let the agent plan a real existing Jira issue by loading the Jira issue context before planning and then returning an auditable implementation plan tied to that issue.

Scope:
- Add a read-only `jira.get_issue` tool
- Add a new `jira_issue_plan` scenario and request classifier
- Prefetch Jira issue context before planner execution
- Feed Jira issue summary and description into the planner input
- Return the generated agent plan alongside the Jira issue data
- Add submit-page guidance for existing Jira issue planning requests

Out of Scope:
- Multi-issue Jira planning
- Automatic Jira issue updates after planning
- Full Jira project discovery UI

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Phase 4 tool runtime and Jira connector foundation

Output:
- A task flow that can plan an existing Jira issue when Jira credentials are configured

Acceptance Criteria:
- Requests such as `Plan Jira issue OPS-123 ...` route to a dedicated planning scenario
- The system attempts to load Jira issue context before generating the plan
- The final task result contains both Jira issue context and the generated agent plan
- Missing Jira configuration causes a clear early failure instead of a silent fallback

Risks / Dependencies:
- Real Jira planning depends on valid Jira credentials and network access
- The planner quality depends on the issue description containing enough implementation context

Verification:
- Run backend compile
- Run a local stubbed Jira smoke test for the full planning flow
- Rebuild the frontend
- Restart the live backend and verify live scenario classification plus clear readiness/failure behavior

Result:
- Added `jira.get_issue` to the Phase 4 tool registry as a read-only Jira connector with the same readiness checks as Jira issue creation
- Added a `jira_issue_plan` request scenario that is triggered when a Jira issue key appears together with planning-style wording
- Added pre-planning Jira issue context loading in the orchestrator so planner input can be enriched with issue summary, status, priority, and description
- Added Action Agent support for extracting Jira issue keys from requests
- Updated planner fallback behavior and OpenAI planning instructions so Jira issue planning uses `jira.get_issue`
- Updated the execution phase so final Jira planning results include an `agent_plan` payload alongside the Jira issue metadata
- Updated the submit page starter prompts and guidance so existing Jira issue planning is a first-class example in the UI
- Verified backend compile, a local stubbed Jira issue planning flow that completed successfully with `jira_issue_plan`, frontend rebuild, live backend restart, live registry visibility for `jira.get_issue`, and live early failure messaging when Jira credentials are still missing

### T-020 Jira URL Planning and Semantic Translation Layer

Status: done
Date: 2026-04-11

Goal:
- Let the agent accept direct Jira URLs, normalize natural-language requests through a structured semantic translation step, and expose that intermediate state in the dashboard for auditability.

Scope:
- Parse Jira issue references from either issue keys or Jira URLs
- Add a semantic translation document and store it on tasks
- Support a Minimax-backed semantic translator with deterministic fallback
- Feed semantic translation into planner context and knowledge/code retrieval
- Show the semantic translation result in the task detail page

Out of Scope:
- Full prompt management UI for different translation models
- Multi-issue planning in a single task
- Automatic code patch generation from the translated request

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing Jira issue planning flow
- Existing knowledge retrieval and dashboard task detail views
- User-provided Jira URL and planned Minimax API availability

Output:
- A task pipeline that can normalize direct Jira URLs plus natural-language code/debug requests into a structured intermediate representation before planning and retrieval

Acceptance Criteria:
- Jira URLs such as `/browse/ABC-123` or `selectedIssue=ABC-123` are recognized as Jira issue references
- Each task stores an auditable semantic translation document
- Knowledge requests can use translated search queries instead of raw user text only
- The task detail page shows the translation result clearly
- The system falls back safely when Minimax is not configured or errors

Risks / Dependencies:
- Real Minimax execution depends on a valid API key and outbound network access
- Jira credentials still need to be configured separately before real Jira issue reads can succeed

Verification:
- Run backend compile
- Run local API smoke tests for Jira URL parsing, semantic translation persistence, and translated knowledge queries
- Rebuild the frontend
- Restart the live backend and frontend static server if the build changes

Result:
- Added a shared Jira reference parser that accepts direct issue keys, `/browse/KEY` style URLs, and backlog URLs with `selectedIssue=KEY`
- Added a semantic translation document stored on each task as `translation_json`
- Added semantic translation events so the primary runtime now records when translation starts, completes, or falls back
- Added a MiniMax-backed semantic translator path with deterministic fallback when MiniMax is not configured or fails
- Updated the orchestrator so Jira issue planning first captures translation, then preloads Jira issue context, then re-runs translation with that richer context before planning
- Updated the Action Agent so knowledge retrieval uses translated search queries instead of raw user text and Jira issue planning uses the translated issue key
- Updated task titles so pasted Jira URLs become readable dashboard titles like `Plan Jira issue P69-10`
- Added a semantic translation panel to task detail so operators can inspect intent, work type, issue link, candidate modules, search queries, grounding terms, and raw JSON
- Updated the submit page to advertise direct Jira URL planning requests
- Verified local smoke tests for Jira URL parsing plus translated knowledge retrieval, a provider-path check for MiniMax mode selection, frontend production build, live backend restart, live Jira URL task parsing, and live translated knowledge queries in tool execution logs

### T-021 Jira Runtime Normalization and MiniMax Output Hardening

Status: done
Date: 2026-04-11

Goal:
- Fix the first live Jira and MiniMax integration issues so a configured Jira issue can be read correctly and MiniMax semantic translation can survive minor schema drift.

Scope:
- Normalize Jira base URLs so backlog or browse links still produce the correct REST API origin
- Harden MiniMax translation parsing by trimming overlong list fields and sanitizing enum-like outputs
- Re-run live Jira issue planning against `P69-10`

Out of Scope:
- New planner prompts
- Multi-issue workflows
- Jira writeback or comment posting

Modules:
- `apps/backend/`
- `docs/task-cards.md`

Input:
- Live Jira readiness with a configured bearer token
- Live MiniMax semantic translation requests

Output:
- A stable Jira issue planning flow that can tolerate a pasted Jira backlog URL in configuration and minor MiniMax output variation

Acceptance Criteria:
- Jira connector calls the site root origin even if the configured value includes a backlog URL
- MiniMax responses no longer fail solely because list fields exceed the schema cap
- A live `P69-10` planning request reaches reviewer with non-empty Jira issue data

Risks / Dependencies:
- Real Jira content still depends on the token having issue read access for `P69-10`
- MiniMax can still fail for reasons other than schema length drift

Verification:
- Run backend compile
- Re-run the live `P69-10` task
- Inspect tool execution and events for non-empty Jira issue fields and MiniMax provider usage

Result:
- Normalized Jira base URLs so the connector now strips pasted backlog or browse URLs down to the site root before building REST API requests
- Hardened MiniMax translation parsing so overlong list fields are trimmed and sanitized before schema validation
- Added a guard so malformed Jira issue payloads fail early in the connector instead of surfacing later as reviewer output-contract failures
- Verified backend compile and live runtime reload
- Verified that `jira.get_issue` and `jira.create_issue` are now `ready` in the registry when `.env` is loaded
- Verified that semantic translation now runs with `provider.name = minimax` and no longer falls back solely because list lengths exceed schema caps
- Verified that the remaining live blocker for `P69-10` is now external authentication: Jira REST API returns `HTTP 403: {"error": "Failed to parse Connect Session Auth Token"}`

### T-022 MiniMax Planner and Human-Readable Change Plan

Status: done
Date: 2026-04-11

Goal:
- Use MiniMax as the planner model and return a plan that clearly explains what should change, where the likely source code lives, and the implementation plan in natural language.

Scope:
- Add MiniMax support to the planner provider path
- Enrich plan schema with natural-language change explanation and affected code locations
- Gather planning-time repository context for Jira issue planning before planner execution
- Upgrade the task detail page so plan output is readable without opening raw JSON

Out of Scope:
- Automatic code edits from the plan
- Multi-repo planning heuristics
- Full multi-agent decomposition beyond the existing single-runtime flow

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`

Input:
- Working Jira `P69-10` issue retrieval
- Working MiniMax semantic translation provider
- Existing Handyman code knowledge base

Output:
- A live Jira planning task whose planner is MiniMax-backed and whose plan highlights what to change, candidate source files, and implementation steps

Acceptance Criteria:
- Planner can run with `provider.name = minimax`
- Jira planning collects codebase context before plan generation
- Plan payload includes a readable change summary, a change explanation, and likely source locations
- Dashboard renders the new plan fields in a human-readable layout

Risks / Dependencies:
- MiniMax may still occasionally emit malformed plan JSON and require fallback
- Planning-time code retrieval is heuristic and may suggest candidate files rather than exact edit points

Verification:
- Run backend compile
- Rebuild the frontend
- Re-run live Jira planning for `P69-10`
- Confirm the returned plan shows MiniMax provider usage plus human-readable code-location guidance

Result:
- Added MiniMax as a first-class planner provider and kept deterministic defaults available to fill any missing machine fields without losing the model's natural-language plan
- Expanded the plan schema with `change_summary`, `change_explanation`, and `affected_code_locations` so the plan can explain what to change, where the likely source code lives, and how to proceed
- Added planning-time repository-context retrieval for Jira planning and compacted that context before sending it to MiniMax so planner requests stay grounded without bloating the prompt
- Added a structured plan breakdown UI so the dashboard now renders the change explanation, likely code files, implementation steps, assumptions, and missing information without forcing users to read raw JSON
- Rebuilt the frontend bundle and restarted the live backend runtime after the planner changes
- Verified live Jira planning for `P69-10` now completes with `plan_provider_name = minimax`, `plan_provider_mode = chatcompletion_v2`, and readable plan output that includes candidate Handyman source files and implementation steps

### T-023 Phase 5-7 Enterprise Roadmap

Status: done
Date: 2026-04-11

Goal:
- Turn the next-step ideas for governance, enterprise UI, and later scale-out into an executable development roadmap that fits the current single-runtime platform.

Scope:
- Document the rationale behind the recent planner and grounding improvements
- Define the implementation plan for:
- Phase 5 Approval + RBAC + Risk Guardrails
- Phase 6 Enterprise Demo Dashboard
- Phase 7 Async and multi-agent scale-out
- Capture recommended sequencing, deliverables, and acceptance criteria

Out of Scope:
- Implementing Phase 5 runtime changes
- Building the new dashboard modules
- Splitting the runtime into async workers in this task

Modules:
- `docs/task-cards.md`
- `docs/phase-5-7-enterprise-roadmap.md`

Input:
- Current live system state through Phase 4 plus MiniMax planner work
- Requested enterprise features for governance, auditability, and human oversight

Output:
- A concrete roadmap document for the next three phases

Acceptance Criteria:
- The roadmap explains why the recent planner changes were made
- Phase 5, Phase 6, and Phase 7 each have scoped deliverables and sequencing
- The plan is specific enough to start implementation task cards from it

Risks / Dependencies:
- The roadmap must preserve the current single-runtime simplicity until governance and UI flows are stable
- Phase 7 must not pull async or service-splitting forward before approval and audit paths are in place

Verification:
- Review the roadmap document for Phase 5-7 scope, deliverables, and ordering
- Ensure task-card logging is updated for traceability

Result:
- Added a roadmap document for Phase 5-7 covering governance, enterprise UI, and later scale-out sequencing
- Documented why the current MiniMax planner and grounded code-location improvements were worth doing before RBAC and approvals
- Defined Phase 5 around RBAC matrix, policy engine, approval workflow, high-risk guardrails, and auditable events
- Defined Phase 6 around the demo-critical enterprise console modules, including Request Console, Task List, Task Detail, Plan and Review, Tool Logs, and Approval Queue
- Defined Phase 7 as a post-governance scale phase for async runners, queueing, long-running workflows, and eventual multi-agent decomposition

### T-024 Phase 5 Governance Data Model

Status: done
Date: 2026-04-11

Goal:
- Add the foundational governance data model for Phase 5 so RBAC, policy evaluation, approval routing, and risk guardrails can build on stable backend types and tables.

Scope:
- Add actor-role, risk-category, and policy-decision enums
- Extend task and approval persistence with governance fields
- Add `rbac_role` and `policy_rule` tables with default seeded records
- Add read APIs for governance metadata and approval queue access

Out of Scope:
- Full runtime policy evaluation
- Approval escalation logic
- Dashboard UI for approval queue and admin settings

Modules:
- `apps/backend/`
- `docs/task-cards.md`

Input:
- Existing task/event/approval data model
- Phase 5 roadmap and governance requirements

Output:
- A backend governance foundation with seeded roles, policy rules, richer approval metadata, and read APIs

Acceptance Criteria:
- Backend exposes seeded RBAC roles and policy rules through API
- Task records store actor role and risk category
- Approval records store requester identity, approver role, risk data, and policy snapshot
- Existing task and approval flows still run after schema migration

Risks / Dependencies:
- SQLite local schema upgrades need additive migrations only
- Policy-rule data must stay scoped to foundation work and not prematurely replace reviewer logic

Verification:
- Run backend compile
- Restart the live backend
- Check governance role and policy-rule endpoints
- Create an approval-gated task and verify the approval metadata appears through API

Result:
- Added governance enums for actor roles, policy decisions, risk categories, and new approval and policy event types
- Extended `task` with actor identity, actor role, risk category, and governance snapshot fields
- Extended `approval` with requester identity, decision identity, risk metadata, expiry slot, and policy snapshot fields
- Added seeded `rbac_role` and `policy_rule` tables plus governance read APIs
- Added approval listing filters so a future Approval Queue page has a backend source
- Preserved existing orchestration while enriching approval creation with role, risk, and policy snapshot data
- Added local SQLite migration normalization for the new enum-backed columns so old rows and new rows can be read through the ORM consistently
- Verified backend compile, live governance endpoints, live task actor/risk filtering, live approval listing, and an isolated approval-required smoke test with policy snapshot metadata

### T-025 Minimal AI Workbench Frontend Refactor

Status: done
Date: 2026-04-11

Goal:
- Refactor the frontend into a minimal white AI assistant workbench with chat-first navigation, knowledge management, memory management, settings, login state, and RBAC-aware controls.

Scope:
- Replace the current dashboard-style shell with a fixed white sidebar and clean main workspace
- Add chat route with conversation list mapped from existing tasks and task creation as message submission
- Add knowledge, memory, settings, and login pages
- Add local frontend auth state and RBAC guards for sensitive controls
- Connect available backend APIs for tasks, knowledge, approvals, and tools while using local mock state for unsupported memory/model/upload persistence
- Rewrite shared CSS into a restrained black/white/gray design language

Out of Scope:
- Real backend authentication
- Multipart file upload backend
- Persistent backend memory store
- Full model configuration backend writes

Modules:
- `apps/web/`
- `docs/task-cards.md`

Input:
- Existing React/Vite frontend
- Existing backend task, knowledge, approval, and tool APIs

Output:
- A working enterprise AI assistant frontend with a clean sidebar, chat page, knowledge page, memory page, settings page, login state, and RBAC checks

Acceptance Criteria:
- The default route opens the chat workbench
- New task requests create a conversation and appear in the left conversation list
- Task replies are rendered in natural language first, with technical details hidden behind secondary disclosure
- Knowledge page exposes upload/import affordances and lists synced knowledge sources/documents
- Memory page supports local add/edit/delete/search with RBAC checks
- Settings page shows mainstream model provider groups and guarded configuration controls
- Login state controls sensitive navigation and actions

Risks / Dependencies:
- Some requested product capabilities do not yet have backend endpoints and must be implemented as local UI scaffolding
- The refactor must preserve existing task detail/list access for current backend workflows

Verification:
- Run frontend TypeScript build
- Open live frontend and verify route availability
- Exercise chat task creation and sidebar conversation switching
- Verify permission-denied UI for viewer/member roles

Result:
- Refactored the frontend shell into a fixed left-sidebar AI workbench with chat-first navigation and restrained black/white/gray styling
- Added local conversation title overrides so recent task conversations can be renamed from the sidebar while still mapping to existing backend tasks
- Updated chat submission so attached files are preserved as request context and agent replies prioritize natural-language summaries over raw technical payloads
- Expanded knowledge import affordances for drag/drop, file selection, folder selection, zip selection, and a compliant local-path placeholder for future backend or desktop integration
- Added knowledge source viewing, re-index controls, and RBAC-guarded delete scaffolding while preserving the existing backend `knowledge/sources`, `knowledge/documents`, and `knowledge/sync` APIs
- Added local memory persistence for automatic memory settings, whitelist/blacklist topics, search, add, edit, and delete operations with `memory:edit` permission checks
- Rebuilt the model settings panel around provider groups for OpenAI, Anthropic, Google, DeepSeek, Moonshot, Mistral, Cohere, and domestic model providers
- Kept API key inputs masked and non-persistent in the browser demo, with copy directing production usage toward backend-managed configuration or a vault
- Updated shared CSS tokens and component rules for a cleaner white UI, light borders, 8px radii, no negative letter spacing, and less visual noise
- Verified:
- `npm.cmd exec tsc -- --noEmit -p tsconfig.app.json`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.node.json`
- `npm.cmd run build`
- live backend health at `http://127.0.0.1:8000/health`
- live frontend HTTP 200 at `http://127.0.0.1:5173/`

### T-026 Workbench Backend Persistence and Governance Integration

Status: planned
Date: 2026-04-11

Goal:
- Replace the temporary frontend-only workbench scaffolding with backend-backed persistence and align the UI RBAC controls with server-side policy decisions.

Scope:
- Add backend upload/import support for knowledge files, folders, and zip archives
- Add backend delete or disable support for knowledge sources and indexed documents
- Add a backend memory store with list, create, update, delete, and memory-control endpoints
- Add backend model/provider configuration read APIs and a safe write path for admin-managed settings
- Connect frontend permission checks to backend governance roles and policy-rule responses where possible
- Add natural permission-denied responses for guarded operations before and after API calls

Out of Scope:
- Real identity provider integration
- Full secret vault implementation
- Async worker split or streaming runtime
- Multi-agent service decomposition

Modules:
- `apps/backend/`
- `apps/web/`
- `docs/task-cards.md`
- `README.md`
- `CLAUDE.md`

Input:
- Completed T-025 AI workbench frontend
- Existing governance data model from T-024
- Existing knowledge source indexing APIs

Output:
- Backend-backed workbench persistence for knowledge, memory, and settings surfaces
- Frontend controls that no longer rely only on local scaffolding for sensitive operations
- Updated documentation for the next implementation phase

Acceptance Criteria:
- Knowledge import can send files or zip content to backend-owned processing endpoints
- Knowledge source deletion or disablement is enforced server-side
- Memory entries persist through backend APIs instead of only localStorage
- Model/provider settings are loaded from a controlled backend endpoint
- RBAC-sensitive actions are checked both before user interaction and before API mutation
- README and CLAUDE identify this as the backend persistence phase after the current P0 chat-answer and reference-UI fixes

Risks / Dependencies:
- File and folder import behavior must remain browser-safe and cannot assume direct local-path access
- Secret handling must avoid storing raw provider keys in frontend localStorage
- Backend policy enforcement should be additive and must not break existing task creation flows

Verification:
- Backend compile and targeted API smoke tests
- Frontend TypeScript checks and production build
- Manual UI check for admin, operator, member, and viewer roles

Result:
- Pending

### T-027 Resumable Development State Files

Status: done
Date: 2026-04-11

Goal:
- Add repository-local recovery files so an interrupted agent session can resume without losing the project background, current blocker, task queue, decisions, and evidence.

Scope:
- Add a repo-level agent guide
- Add mutable current-state, task-queue, decision, and session-handoff files
- Record the current chat-answer blocker and local reference UI direction
- Update README, CLAUDE, and roadmap references so future sessions start from the recovery files

Out of Scope:
- Fixing the chat-answer chain itself
- Reworking the frontend to match the reference screenshots
- Adding backend persistence for memory, model settings, or knowledge upload

Modules:
- `AGENTS.md`
- `CURRENT_STATE.md`
- `TASK_QUEUE.md`
- `DECISIONS.md`
- `SESSION_HANDOFF.md`
- `README.md`
- `CLAUDE.md`
- `docs/task-cards.md`
- `docs/phase-5-7-enterprise-roadmap.md`

Input:
- User request to prioritize breakpoint/resume persistence after a machine restart
- User-provided recovery-file scheme
- Observed chat failure where planner text appeared as the assistant answer
- Local reference screenshots under `references/`

Output:
- A permanent recovery entry and mutable handoff files that document current state, decisions, queue, evidence, and next first action

Acceptance Criteria:
- Future agents can start by reading `AGENTS.md`, `CURRENT_STATE.md`, `DECISIONS.md`, `TASK_QUEUE.md`, and `SESSION_HANDOFF.md`
- The current P0 blocker is recorded as the chat knowledge-answer chain
- The strict reference UI pass is recorded as a P0 next task
- The prior T-026 backend persistence task remains tracked and was temporarily sequenced after P0 recovery, chat-answer, and reference-UI work
- Runtime evidence and the lack of Git metadata are documented

Risks / Dependencies:
- The project is not currently a Git repository, so branch, commit, and stash evidence cannot be captured
- Recovery files must be kept current after future implementation work

Verification:
- Confirm recovery files exist
- Confirm README and CLAUDE point future sessions to the recovery files
- Confirm the task queue records the post-recovery next implementation task

Result:
- Added `AGENTS.md` as the repo-level recovery and agent instruction entry
- Added `CURRENT_STATE.md`, `TASK_QUEUE.md`, `DECISIONS.md`, and `SESSION_HANDOFF.md`
- Recorded the live chat-answer failure diagnosis and the local reference UI notes
- Updated README, CLAUDE, and roadmap sequencing to prioritize the chat-answer fix and strict reference UI pass before returning to T-026

### T-028 Fix Chat Knowledge Answer Chain

Status: done
Date: 2026-04-11

Goal:
- Fix repository question behavior so the chat page returns a natural answer or a natural no-evidence message instead of exposing planner objectives and step titles.

Scope:
- Improve knowledge search packaging for no-citation results
- Improve Firebase/configuration query matching where possible
- Ensure backend failed knowledge output stores a user-facing message
- Ensure frontend chat fallback does not render planner output as the assistant answer for repository questions
- Hide normal-product exposure of task status, request type, and review state in the chat message surface

Out of Scope:
- Full reference screenshot UI pass
- New upload or memory persistence APIs
- Async runtime or multi-agent decomposition

Modules:
- `apps/backend/app/services/knowledge.py`
- `apps/backend/app/orchestrator/service.py`
- `apps/backend/app/agents/service.py`
- `apps/web/src/components/chat/MessageList.tsx`
- `docs/task-cards.md`
- `TASK_QUEUE.md`
- `SESSION_HANDOFF.md`

Input:
- User report that a frontend repository question returned planner text with `Status failed`, `Request type process_question`, and `Review state needs_info`
- Existing knowledge search, reviewer, orchestrator, and chat rendering behavior

Output:
- Repository questions no longer show planner step lists as the normal assistant response

Acceptance Criteria:
- `Locate Firebase configuration file(s) in the codebase` returns a natural-language answer or natural no-evidence message
- Frontend chat does not use `plan.change_explanation` as the normal answer for `process_question`
- Chat page does not surface task status/review state as primary product content
- Backend still compiles and frontend TypeScript/build checks pass

Risks / Dependencies:
- Actual grounded Firebase evidence depends on the configured knowledge source and indexed repository content
- If the local knowledge source points to a different repository, the correct behavior is a natural no-evidence response, not a planner/debug response

Verification:
- `& "$env:LOCALAPPDATA\Python\bin\python.exe" -m compileall app`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.app.json`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.node.json`
- `npm.cmd run build` passed after rerunning outside the sandbox because Vite/esbuild spawn hit sandbox `EPERM`
- `/api/knowledge/search` for `Locate Firebase configuration file(s) in the codebase` returned `app/google-services.json`
- `POST /api/tasks` for the same query returned `status=completed`, `scenario=process_question`, `review_verdict=approved`, and citations including `app/google-services.json`

Result:
- Updated knowledge search query expansion and route preferences so Firebase/configuration queries prioritize `google-services`, JSON, Gradle, manifest, and related configuration evidence
- Ensured no-citation knowledge results still include non-empty packaged context, allowing the frontend to render the natural no-evidence answer instead of falling back to a plan
- Updated planner fallback wording from `mock knowledge` to configured repository knowledge
- Sanitized `process_question` planner payloads so model-provided `missing_information` does not block a repository lookup before execution
- Added a backend user-facing failed-output message path for rejected knowledge answers
- Updated chat rendering so `process_question` does not use `plan.change_explanation` as its normal assistant reply
- Removed normal chat exposure of task status, request type, and review state
- Restarted the local backend so the frontend calls the updated code

### T-029 Strict Reference UI Pass

Status: done
Date: 2026-04-11

Goal:
- Rework the frontend workbench to closely match the local reference screenshots while preserving existing backend integration points.

Scope:
- Tighten the fixed left sidebar layout, conversation list, search field, navigation, feature toggles, and user entry
- Rework chat header, model selector, message layout, and composer to match the reference chat screenshot
- Rework knowledge page title, upload button, upload drop zone, embedding/status card, and source list to match the reference knowledge screenshot
- Rework memory page stats, search, settings action, add-memory action, and empty/list states to match the reference memory screenshot
- Rework settings page tabs, provider chips, model rows, and selected state to match the reference settings screenshot
- Add or align a simple home/entry surface if needed to match the reference home screenshot
- Keep the current white/black/gray styling and avoid debug/status panels in the main product surface

Out of Scope:
- Backend knowledge upload persistence
- Backend memory persistence
- Backend model/provider secret storage
- New async runtime or multi-agent architecture

Modules:
- `apps/web/src/styles.css`
- `apps/web/src/components/layout/`
- `apps/web/src/components/chat/`
- `apps/web/src/components/knowledge/`
- `apps/web/src/components/memory/`
- `apps/web/src/components/settings/`
- `apps/web/src/pages/`

Input:
- Local screenshots in `references/`
- Completed T-025 frontend refactor
- Completed T-028 chat-answer chain fix

Output:
- A stricter reference-matched enterprise AI assistant workbench

Acceptance Criteria:
- Chat page matches the reference hierarchy: pale sidebar, recent conversations, centered readable chat column, assistant header, model selector, black user bubble, white assistant card, and bottom composer
- Knowledge page matches the reference hierarchy: centered title, black upload action, dashed upload zone, status card, and compact file/source list
- Memory page matches the reference hierarchy: compact stats, search, settings/add actions, and clean empty/list state
- Settings page matches the reference hierarchy: tabs, provider chips, model rows, and simple selected state
- Main surfaces do not expose planner/debug/task metadata
- Frontend TypeScript and production build pass

Risks / Dependencies:
- The reference screenshots show product-level layout, not exact component API contracts, so implementation must preserve current routes, state, and API calls while changing the visual arrangement

Verification:
- `npm.cmd exec tsc -- --noEmit -p tsconfig.app.json`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.node.json`
- `npm.cmd run build` passed after rerunning outside the sandbox because Vite/esbuild spawn hit sandbox `EPERM`
- Frontend returned HTTP `200`
- Backend health returned `{"status":"ok"}`

Result:
- Added a `/home` entry surface with centered Knowledge Assistant content and three compact cards
- Reworked the fixed sidebar with a pale background, start-chat action, search field, navigation, feature toggles, recent conversations, and account entry
- Reworked chat header, model pill, starter copy, composer placeholder, and message surface to stay product-first and free of task-debug metadata
- Reworked knowledge page header, upload action, upload icon, dashed import panel, source status, and source/document presentation
- Reworked memory page header actions, stat cards, control anchor, and add-memory anchor
- Reworked settings into model/API tabs, provider chips, full-width model rows, and masked API configuration fields
- Replaced the corrupted domestic-provider label in `ModelSelector.tsx` with `Aliyun / Zhipu / Domestic`
- Updated shared CSS for a stricter white/black/gray reference layout with a narrower centered content column, lighter borders, and flatter cards
- Residual risk: this pass was verified by TypeScript/build and local HTTP checks, not by screenshot-level visual diffing

### T-032 Same-Conversation Follow-up Turns

Status: done
Date: 2026-04-11

Goal:
- Allow users to continue asking follow-up questions inside the same chat thread, with enough prior context for the backend to answer short follow-ups such as `Give me the exact path.`

Scope:
- Reuse the active task `session_id` for messages sent from an existing chat
- Include the previous user request and assistant answer as hidden follow-up context in the backend request payload
- Render only the visible follow-up text in the chat UI
- Group the sidebar conversation list by `session_id`
- Make backend task classification use the actual follow-up request rather than the whole context block

Out of Scope:
- Dedicated `conversation_message` persistence table
- Streaming assistant responses
- Full conversation summarization memory
- Removing per-turn task records used for auditability

Modules:
- `apps/backend/app/services/tasks.py`
- `apps/web/src/types.ts`
- `apps/web/src/pages/chat/ChatPage.tsx`
- `apps/web/src/components/chat/MessageList.tsx`
- `apps/web/src/components/layout/ConversationList.tsx`
- `apps/web/src/styles.css`
- `docs/task-cards.md`
- `TASK_QUEUE.md`
- `CURRENT_STATE.md`
- `SESSION_HANDOFF.md`

Input:
- User request to ask follow-up questions in the same chat instead of opening a new isolated request
- Current backend task/session model
- Current frontend chat page and sidebar conversation list

Output:
- Same-session follow-up behavior in the chat UI, while preserving per-turn backend task records for audit

Acceptance Criteria:
- Sending from an existing chat reuses the active `session_id`
- Follow-up requests include prior context for backend grounding
- Chat shows only the user's follow-up text, not the hidden context payload
- Sidebar groups turns under one conversation title
- Backend classification of `Give me the exact path.` remains `process_question`, not Jira creation

Risks / Dependencies:
- This is still a task-per-turn implementation rather than a dedicated conversation message table
- Long threads will eventually need summarization or server-side conversation storage

Verification:
- `& "$env:LOCALAPPDATA\Python\bin\python.exe" -m compileall app`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.app.json`
- `npm.cmd exec tsc -- --noEmit -p tsconfig.node.json`
- `npm.cmd run build` passed after rerunning outside the sandbox because Vite/esbuild spawn hit sandbox `EPERM`
- Follow-up smoke created a first turn for `Locate Firebase configuration file(s) in the codebase` and a second turn `Give me the exact path.`
- Smoke result: both turns used the same `session_id`, second turn was `scenario=process_question`, `status=completed`, `review_verdict=approved`, and citations included `app/google-services.json`

Result:
- Added follow-up context packaging in `ChatPage`
- Added display-text extraction in `MessageList` so hidden context is not shown as the user's message
- Added multi-turn rendering for all task details in the same session
- Grouped sidebar conversations by `session_id` with one title and turn count per conversation
- Added frontend `session_id` support to task creation input
- Updated backend task creation to classify/risk/title follow-ups using the marker-delimited user intent instead of the full context block
- Restarted the backend after the classification fix so live verification used the updated code

### T-033 Environment Handoff Documentation

Status: done
Date: 2026-04-11

Goal:
- Prepare the repository for continuing development in a new environment by making current progress, context, decisions, evidence, and next work recoverable from local documentation.

Scope:
- Add a long-lived project context file
- Update recovery bootstrap lists to include the new context file
- Make handoff docs encoding-safe by avoiding non-ASCII absolute local paths
- Record current completed tasks, next task, known gaps, verification evidence, Firebase findings, and module scope for the next implementation step

Out of Scope:
- Implementing T-026 backend persistence
- Changing runtime behavior
- Reworking UI beyond documentation updates

Modules:
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

Input:
- User request to prepare for switching development environments
- Existing recovery files and task-card history
- Latest completed work through T-032

Output:
- A complete handoff package that future agents can read before continuing development

Acceptance Criteria:
- A new session can read a fixed list of recovery files and understand current progress
- Current next task is clearly identified as T-026
- Known gaps and non-goals are explicit
- Verification evidence is captured
- Firebase findings are documented without relying on mojibake-prone absolute paths

Risks / Dependencies:
- The project folder is not currently a Git repository, so branch/commit/stash evidence cannot be captured
- Future sessions must keep these files current after new implementation work

Verification:
- Confirm `PROJECT_CONTEXT.md` exists
- Confirm `AGENTS.md`, README, and CLAUDE recovery lists include `PROJECT_CONTEXT.md`
- Confirm recovery docs avoid non-ASCII absolute D-drive paths
- Confirm T-026 remains the next implementation task

Result:
- Added `PROJECT_CONTEXT.md` as the long-lived project context entry
- Updated `AGENTS.md`, README, and CLAUDE recovery bootstrap lists
- Updated `CURRENT_STATE.md`, `TASK_QUEUE.md`, `DECISIONS.md`, and `SESSION_HANDOFF.md` with environment handoff details
- Updated the roadmap sequence so T-032 and T-033 are done, and T-026 remains the next implementation task
