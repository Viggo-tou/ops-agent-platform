# Task Queue

Last updated: 2026-04-14

Status values: `todo`, `doing`, `blocked`, `done`.

## P0

### T-034 Jira Develop Pipeline Stability Fixes

Status: done (code), docs-only (no git commit — see T-037)

Scope: six pipeline fixes for the `jira_issue_develop` flow, verified end-to-end on Jira project `TEST` (TEST-1 transitioned To Do → Done).

Fixes:

- Fix #1: MiniMax planner empty-steps fallback — add `jira_issue_develop` guidance + post-sanitize default refill in `apps/backend/app/agents/service.py`.
- Fix #3: `codegen.files_changed` aggregated into task `result` (was already correct; false alarm).
- Fix #4: Pure new-file tasks skip grounding-file batches; `_new_file_task` pipeline flag with three detection paths (plan/disk/request-text filename extraction) in `apps/backend/app/orchestrator/service.py`.
- Fix #5: Removed auto Jira comment after task completion (transition to Done still kept); cleaned up "Jira: commented and transitioned" message wording to "Jira: transitioned".
- Fix #6: Planner no longer copies knowledge-retrieval citations into `affected_code_locations` for develop scenarios — citations are grounding, not edit targets. Prompt in `_build_planning_instructions` rewritten.
- Test sync: `test_develop_pipeline.py` updated (12/12 green), `test_jira_writeback_scenario.py` `requires_approval` assertion aligned with current auto-approve policy.

Verification:

- TEST-1 end-to-end: `status=completed`, `affected_code_locations=[]`, `_new_file_task=true`, single codegen batch, `files_changed=["config/retry.json"]`, Jira transitioned to Done, no auto-comment.
- Develop pipeline tests: 12/12 pass.

Files touched this session:

- `apps/backend/app/agents/service.py`
- `apps/backend/app/orchestrator/service.py`
- `apps/backend/tests/orchestrator/test_develop_pipeline.py`
- `apps/backend/tests/orchestrator/test_jira_writeback_scenario.py`

### T-035 Residual Test Debt (auto-approve + provider resolver)

Status: done (2026-04-14)

Pre-existing failures unrelated to T-034, fixed by aligning assertions with current policy:

- `tests/services/test_codegen.py` — 4 failures fixed. `_resolve_provider()` renamed to `_resolve_provider_chain()` (returns a list); `codegen.generate_patch` permission now WRITE (auto-approve policy), not APPROVAL_REQUIRED.
- `tests/tools/test_tool_approval_gate.py` — 3 failures fixed. Swapped sample tool `sandbox.run_command` (now WRITE) → `internal_api.request` (still APPROVAL_REQUIRED under current registry).

Verification: `pytest tests/` = 124 passed, 0 failed.

Files touched this session:

- `apps/backend/tests/services/test_codegen.py`
- `apps/backend/tests/tools/test_tool_approval_gate.py`

### T-036 Completeness Check Third Safety-Net Path (Fix #2)

Status: todo (demoted)

After Fix #4 the original symptom (grounding-file pollution in completeness check) is no longer the main failure mode. This safety-net path remains a hedge. Low priority.

### T-037 Session Boundary Discipline and Commit Hygiene

Status: todo

Root cause: only one commit (`940b232 chore: initial commit`) exists. Every session since T-024 has left its work in the working tree; there is no git-level way to tell which lines belong to which session. This forces every "commit only this session" request to fail.

Acceptance:

- `AGENTS.md` records a mandatory session-start and session-end commit ritual.
- Each session ends with either a real commit OR a `SESSION_HANDOFF.md` manifest listing the exact files + line-stat touched this session.
- A `session-start/<timestamp>` git tag is created at the beginning of each working session so the diff baseline is unambiguous.
- Policy is written so that a future session cannot produce the "can't separate my work" situation.



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

Status: in progress (audit + decisions + tests done; zip + E2E pending)

Audit (2026-04-14) found that most scope was already implemented. Refined sub-tasks with explicit ownership:

| ID | Scope | Owner | Status |
|---|---|---|---|
| T-026-S1 | Current-state audit | Claude | done |
| T-026-B | Delete semantics decision | Claude | done → see DECISIONS.md D-008 (keep hard delete) |
| T-026-C | Model-config key-leak defensive test | Claude | done → `tests/api/test_model_config_no_secret_leak.py` |
| T-026-D | Frontend↔backend PERMISSION_MAP parity test | Claude | done → `tests/core/test_permission_map_frontend_parity.py` |
| T-026-M1 | RBAC expected-response matrix fixture | MiniMax | done → `apps/backend/tests/fixtures/rbac_expected_matrix.json` (22 endpoints) |
| T-026-M2 | API schema docstrings | MiniMax | done → `Field(description=...)` added in memory.py, model_config.py, knowledge.py |
| T-026-M3 | ADR 0001 zip import security policy | MiniMax | done → `docs/adr/0001-zip-import-security.md` (9 MUST controls) |
| T-026-M4 | HTTPException detail text normalization | MiniMax | done → 17 sites normalized across `apps/backend/app/api/` |
| T-026-A | Zip archive import endpoint | Codex (after 2026-04-17) | unblocked on ADR; still waiting codex availability |
| T-026-E | 4-role E2E RBAC smoke (`scripts/verify-rbac.ps1`) | Claude | done → 88/88 role×endpoint cells pass against live backend |

Acceptance (original, unchanged):

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
