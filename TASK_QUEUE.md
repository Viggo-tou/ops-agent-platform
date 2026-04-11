# Task Queue

Last updated: 2026-04-11

Status values: `todo`, `doing`, `blocked`, `done`.

## P0

### T-027 Resumable Development State Files

Status: done

Acceptance:

- Repo has a permanent `AGENTS.md` recovery guide.
- Repo has mutable current-state files: `CURRENT_STATE.md`, `TASK_QUEUE.md`, `DECISIONS.md`, `SESSION_HANDOFF.md`.
- The recovery prompt is recorded.
- Current blocker, reference UI findings, runtime evidence, and next actions are recoverable from files.

### T-028 Fix Chat Knowledge Answer Chain

Status: done

Acceptance:

- A repository question such as `Locate Firebase configuration file(s) in the codebase` returns a natural-language answer or a natural no-evidence explanation, not the planner step list.
- Backend `process_question` either produces a valid `knowledge_answer` with citations or stores a user-facing failure response.
- Frontend chat does not show `Status failed`, `Request type`, or `Review state` as normal product content.
- Failed reviewer output is phrased as a calm assistant response.
- Regression smoke test covers at least one repository-grounded query.

### T-029 Strict Reference UI Pass

Status: done

Acceptance:

- Chat, knowledge, memory, settings, and home/entry surfaces follow the screenshots in `references/`.
- Sidebar, main content width, composer, cards, provider chips, upload zone, memory stats, and empty states match the reference hierarchy.
- UI remains white/black/gray, uncluttered, and free of dashboard/debug terminology.
- Chat answer area prioritizes readable Chinese or English natural-language output.

### T-032 Same-Conversation Follow-up Turns

Status: done

Acceptance:

- Sending a message while viewing an existing chat reuses the current `session_id`.
- Follow-up questions include the previous user request and assistant answer as backend context.
- The chat surface displays only the user's follow-up text, not the hidden context payload.
- The sidebar groups tasks by `session_id` so follow-up turns stay under one conversation.
- Backend classification uses the real follow-up request instead of the whole context block.

### T-033 Environment Handoff Documentation

Status: done

Acceptance:

- `PROJECT_CONTEXT.md` exists as a long-lived project context entry.
- Recovery bootstrap lists include `PROJECT_CONTEXT.md`.
- Handoff files avoid non-ASCII absolute paths that render poorly in PowerShell.
- Current progress, next task, verification evidence, Firebase findings, and known gaps are documented.

## P1

### T-026 Workbench Backend Persistence and Governance Integration

Status: todo

Acceptance:

- Knowledge import for files/zip has backend-owned endpoints.
- Knowledge source delete/disable is enforced server-side.
- Memory entries and memory settings persist through backend APIs.
- Model/provider settings load from backend-controlled APIs.
- Sensitive actions are checked by frontend and backend RBAC/governance paths.

## P2

### T-030 Approval APIs and Queue Polish

Status: todo

Acceptance:

- Approval queue UI has backend-backed list/detail actions.
- Policy decisions are visible in natural product language.
- Audit/tool logs remain available without dominating the main workbench.

### T-031 Async Execution Design Spike

Status: todo

Acceptance:

- Documents when and how to introduce queue-backed long-running workflows.
- Keeps the current single-runtime path as the default until governance and product UI are stable.
