# Decisions

Last updated: 2026-04-11

## D-001 Single Runtime Remains The Default

Keep the backend on the current single-runtime orchestrator until governance, approval, audit, and the main workbench UI are stable.

Reason:

- The product still needs correctness and clarity in the existing path.
- Splitting into async workers or multiple services now would make debugging the current answer-chain issue harder.

## D-002 Recovery Files Are Authoritative For Session Resume

Use these files as the first recovery layer:

- `AGENTS.md`
- `PROJECT_CONTEXT.md`
- `CURRENT_STATE.md`
- `TASK_QUEUE.md`
- `DECISIONS.md`
- `SESSION_HANDOFF.md`

Reason:

- The local machine restarted and the active development context was lost.
- These files give future agents a stable, repo-local recovery entry.

## D-003 UI Must Follow Local References Strictly

The next UI pass should treat screenshots in `references/` as the visual source of truth, not just broad inspiration.

Constraints:

- pale fixed left sidebar around 240-270 px
- centered readable main content
- black user bubble and white assistant reply card
- black primary buttons
- light borders and minimal cards
- no gradient, no colorful dashboards, no technical status panels in the product surface

## D-004 Planner Output Is Not A Chat Answer

Do not expose planner objectives and step lists as the normal assistant answer in chat.

Reason:

- Users asked a repository question and received a plan/debug response.
- The assistant surface must show a final answer, a grounded no-evidence explanation, or a calm failure message.

Required follow-up:

- Fix backend `process_question` final output and frontend `MessageList` fallback behavior.

## D-005 Frontend LocalStorage Is Temporary Scaffolding

The current frontend uses localStorage for some workbench scaffolding such as login role, conversation title overrides, memory entries, and model choice.

Reason:

- T-025 focused on UI/product structure before backend persistence existed.

Constraint:

- Do not store raw provider API keys in frontend localStorage.
- T-026 must replace temporary local behavior with backend-backed APIs where sensitive or persistent.

## D-006 Browser Path Import Must Stay Compliant

The browser frontend can offer file, folder, and zip import affordances through user-granted file selection.

It must not claim it can read arbitrary local paths unless a backend, desktop shell, or explicit file access mechanism exists.

## D-007 Follow-up Turns Remain Auditable Tasks For Now

Same-chat follow-ups reuse `session_id` and render as one product conversation in the UI, but each turn is still persisted as a separate backend task.

Reason:

- Existing task/event/tool execution persistence gives an audit trail without adding a new conversation-message table yet.
- This is a pragmatic bridge until T-026 or a later persistence task introduces first-class conversation messages.

Constraint:

- Follow-up classification must use the marker-delimited user intent, not the whole context block.
- The UI must hide the context block and show only the user's actual follow-up text.

## D-008 Knowledge Delete is Hard Delete (T-026-B)

`DELETE /api/knowledge/documents/{id}` and `DELETE /api/knowledge/sources/{name}` physically remove DB rows and, for upload-owned sources, files on disk. No soft-delete / disable column exists.

Reason:

- Knowledge documents are derived artifacts: files can be re-uploaded, repos can be re-synced. A soft-delete tombstone buys no recovery value the source of truth does not already offer.
- Soft-delete adds query filters, index cost, and UI ambiguity ("is this hidden or gone?"). Not worth the complexity at the current product stage.
- Governance-level undo is still available via the task rollback path for patches that introduced the knowledge change.

Constraint:

- Delete must remain gated by the `knowledge:delete` permission (admin only in current `PERMISSION_MAP`).
- If compliance or legal later require retention/recall, switch to soft-delete by adding a `deleted_at` column and filtering in `KnowledgeService.list_documents` / `list_sources` — don't retrofit half the codebase.
