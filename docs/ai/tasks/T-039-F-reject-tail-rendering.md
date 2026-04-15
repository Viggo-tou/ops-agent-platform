# T-039-F — Surface Jira-rejection confirmation in chat reply

## Problem

After T-039 merged, when a user rejects the `jira.transition_issue` approval, the backend writes an appendix to `latest_result_json.message`:

```
## Jira transition rejected

Code changes passed review and are preserved. Jira status was NOT updated because the transition approval was rejected.

**Reviewer notes:** <reason>
```

But the chat UI (`apps/web/src/components/chat/MessageList.tsx` → `buildAgentReply()`) never surfaces this text for develop-scenario tasks. It falls into the `plan_json` branch at `buildAgentReply()` line ~105 and returns the plan-based "change_explanation + locations + steps" reply, so the user sees the pre-approval plan + structured diff but no confirmation that Jira was NOT transitioned.

Verified by Playwright MCP on 2026-04-15 against the already-rejected task `33294801-76d6-4624-bb4c-1291d101e839`: backend `latest_result_json.message` contains `## Jira transition rejected`, but `document.body.innerHTML` of `/chat/{id}` does not.

Evidence: `docs/ai/evidence/T-039/frontend-p69-10-rejected-full-2026-04-15.png`.

## Goal

`buildAgentReply()` must detect the `## Jira transition rejected` section in `latest_result_json.message` and prepend it (as the authoritative outcome line) to whatever reply it would otherwise return, so the user always sees the Jira-reject confirmation in the chat bubble. Structured diff panel rendering (`readDevelopDiff`) must remain unchanged.

## Files to edit

- `apps/web/src/components/chat/MessageList.tsx`

Do NOT touch:

- `apps/web/src/components/chat/DiffViewer.tsx` (diff rendering is independent and already correct)
- backend code (the message format is settled in `apps/backend/app/services/approvals.py`)

## Implementation spec

1. Add a module-private helper in `MessageList.tsx`:

   ```ts
   const JIRA_REJECT_HEADING = "## Jira transition rejected";

   function extractJiraRejectionNotice(message: string | null): string | null {
     if (!message) return null;
     const idx = message.indexOf(JIRA_REJECT_HEADING);
     if (idx === -1) return null;
     // Take from the heading to the next top-level `## ` or EOF.
     const tail = message.slice(idx);
     const nextHeading = tail.slice(JIRA_REJECT_HEADING.length).search(/\n## /);
     const section = nextHeading === -1 ? tail : tail.slice(0, JIRA_REJECT_HEADING.length + nextHeading);
     return section.trim();
   }
   ```

2. Inside `buildAgentReply()`, after `const resultMessage = readString(task.latest_result_json?.message);` (current line ~91), compute once:

   ```ts
   const jiraRejection = extractJiraRejectionNotice(resultMessage);
   ```

3. At the top of the non-failed, non-process_question branch (immediately before the `plan_json` block starting at current line ~105), short-circuit with the rejection notice prepended to the plan-based reply:

   ```ts
   if (jiraRejection) {
     const planReply = /* the same plan/review/fallback computation as below, extracted */;
     return planReply ? `${jiraRejection}\n\n${planReply}` : jiraRejection;
   }
   ```

   Acceptable concrete refactor: extract the existing plan → review → resultMessage → default-fallback chain into an inner `buildDevelopDetail()` function returning `string | null`, call it once, and compose with `jiraRejection` when present.

4. Do NOT change the `failed` / `needs_info` / `rejected` branch at current line ~101 — `resultMessage` already flows there, which already contains the rejection notice.

5. Preserve TypeScript strictness. `tsc --noEmit -p tsconfig.app.json` must pass.

## Acceptance criteria

- `cd apps/web && npm run build` succeeds (this invokes both `tsc` checks + `vite build`).
- Unit-level reasoning: for a `TaskDetail` with `scenario="jira_issue_develop"`, `status="completed"`, `latest_result_json.message` containing `## Jira transition rejected ... Reviewer notes: <x>`, and a non-null `plan_json`, `buildAgentReply(task)` MUST return a string that contains the substring `Jira transition rejected` AND contains the plan's `change_explanation`.
- For a task without the rejection section, behavior is unchanged (plan-based reply as today).
- For `status="failed"` or `review_verdict="rejected"` tasks, the existing `resultMessage ?? reviewSummary` branch is untouched.

## Workflow (for the executor — Codex)

1. Read `apps/web/src/components/chat/MessageList.tsx` end-to-end (it is <200 lines).
2. Apply the edit per the spec above.
3. Run `cd apps/web && npm run build` and paste the last ~20 lines of stdout/stderr into the run log.
4. If build passes, summarize the diff.
5. If build fails, fix iteratively — do NOT skip the `tsc` step.

Run log: write to `docs/ai/runs/T-039-F.log`.
