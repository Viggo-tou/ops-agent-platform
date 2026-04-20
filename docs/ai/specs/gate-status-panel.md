# Spec: GateStatusPanel (T-GATEUI)

## Goal

Add a compact, always-visible panel to `TaskDetailPage` that shows at a glance
whether each of the 6 develop-pipeline gates passed, failed, or was skipped for
the current task. Today the user has to scroll through the event timeline and
squint at `TOOL_SUCCEEDED` / `TOOL_FAILED` lines to figure out gate status.

This is **frontend only** — no backend changes.

## The 6 gates (in pipeline order)

Display in this exact order:

| # | Label (display)       | Derived from tool_name prefix |
|---|-----------------------|-------------------------------|
| 1 | Compile Gate          | `compile_gate.*`              |
| 2 | Runtime Validation    | `runtime_validation.*`        |
| 3 | Semantic Review       | `semantic_review.*`           |
| 4 | Diff Reviewer         | `diff_reviewer.*`             |
| 5 | Spec Conformance      | `spec_conformance.*`          |
| 6 | Goal Attestation      | `goal_decomposition.*`        |

Semantic Review is not yet wired into the live pipeline; for current tasks it
will always resolve to **skipped**. That is correct — design the panel assuming
this is the common case for now.

## Data source

Use the existing **events stream** (`eventsQuery` on TaskDetailPage, endpoint
`GET /api/tasks/{task_id}/events`). Each event already has:
- `event_type`: `tool_call_requested` | `tool_succeeded` | `tool_failed` | others
- `payload_json.tool_name`: e.g. `"compile_gate.check"`, `"spec_conformance.attest"`
- `created_at`, `message`

**Do NOT** pull from `review.findings` — those are planner-review findings, not
develop-pipeline gate findings. The event stream is the authoritative source.

## Status resolution per gate

For each gate prefix, scan events in chronological order:

1. Find all events whose `payload_json.tool_name` starts with the gate's prefix
   (e.g. `compile_gate.`).
2. Determine final status from the **last terminal event** (last
   `tool_succeeded` or `tool_failed` for that prefix):
   - Last terminal = `tool_succeeded` → **`pass`**
   - Last terminal = `tool_failed` → **`fail`**
   - No events at all for this prefix → **`skipped`**
   - Only `tool_call_requested` with no terminal yet → **`running`**
3. Count total invocations (useful to show "2 attempts" when a repair loop
   retried). Count = number of `tool_succeeded` + `tool_failed` events for the
   prefix.
4. Grab the last event's `message` as the "latest detail" line.

Edge cases:
- `spec_conformance.*` has multiple sub-calls: `.check`, `.attest`, `.retry`.
  Resolution rule above still works — take the last terminal across all
  sub-calls.
- `runtime_validation.*` similarly has `.check` and `.repair`. Same rule.
- `compile_gate.*` similarly has repair invocations.

## Component contract

### New files

1. `apps/web/src/components/tasks/GateStatusPanel.tsx` *(new)*
   - Exports `GateStatusPanel` React component and a pure helper
     `resolveGateStatuses(events: EventRead[]): GateStatus[]` for testability.
   - Props:
     ```ts
     interface GateStatusPanelProps {
       events: EventRead[];
     }
     ```
   - Derived type:
     ```ts
     type GateVerdict = "pass" | "fail" | "skipped" | "running";
     interface GateStatus {
       id: "compile_gate" | "runtime_validation" | "semantic_review"
         | "diff_reviewer" | "spec_conformance" | "goal_attestation";
       label: string;           // display label from the table above
       verdict: GateVerdict;
       attempts: number;        // count of terminal events
       latestMessage: string | null;
       latestAt: string | null; // ISO timestamp from event
     }
     ```

   - Render:
     - A header `<h3>Pipeline Gates</h3>` + subtitle
     - A horizontal strip of 6 pill-shaped cards (or a responsive grid that
       wraps to 2-column on narrow viewports). Each card shows:
       - Gate number `1..6` and label
       - Colored dot/pill: green (pass) / red (fail) / gray (skipped) / amber
         (running)
       - Attempts indicator (e.g. `×2`) if attempts > 1
     - Below the strip, a small list with **only failing gates** expanded:
       - Gate label
       - `latestAt` (formatted with existing `formatDateTime` helper)
       - `latestMessage` (truncated to 200 chars with title attr for full)
     - If all gates are `skipped` or `running` (no develop pipeline ran yet),
       render a single muted line: `Develop pipeline has not produced gate
       results yet.` and skip the strip entirely.

2. `apps/web/src/components/tasks/GateStatusPanel.test.tsx` *(new)*
   - Use vitest + `@testing-library/react` (already configured — check
     `apps/web/package.json` and existing `.test.tsx` files for conventions;
     do **not** introduce new test infra).
   - Test: `resolveGateStatuses` with an empty events array returns 6 statuses
     all `skipped`.
   - Test: when events include `compile_gate.check` success then
     `compile_gate.check` failure then `compile_gate.check` success →
     verdict=`pass`, attempts=3.
   - Test: when a gate has `tool_call_requested` but no terminal event →
     verdict=`running`.
   - Test: `spec_conformance.check` failure followed by `spec_conformance.attest`
     success → verdict=`pass` (last terminal wins).
   - One snapshot test of the rendered panel with mixed gates (pass/fail/skipped).

### Modified files

3. `apps/web/src/pages/tasks/TaskDetailPage.tsx`
   - Import `GateStatusPanel`.
   - Mount it **once**, right after the existing task metadata header and
     **before** `PlanBreakdown`/`ReviewBreakdown`/`ToolExecutionPanel`. Use
     the existing `eventsQuery.data ?? []` as the `events` prop.
   - Do not conditionally hide it — the panel handles its own empty state.

4. `apps/web/src/styles/*.css` (whichever stylesheet defines
   `review-section`, `mini-pill`, etc. — find it first, do NOT create a new
   stylesheet)
   - Add minimal CSS classes: `gate-panel`, `gate-strip`, `gate-card`,
     `gate-card--pass`, `gate-card--fail`, `gate-card--skipped`,
     `gate-card--running`, `gate-failures`. Match the existing minimal
     black-on-white visual language (`CLAUDE.md` constraint: "Keep the
     frontend visual language minimal: white background, black text, light
     borders, restrained gray copy, no decorative gradients.").
   - Colors: green = `#166534` text + `#dcfce7` background; red = `#991b1b` /
     `#fee2e2`; gray = `#6b7280` / `#f3f4f6`; amber = `#92400e` / `#fef3c7`.

## Non-requirements (do NOT implement)

- No DiffViewer syntax highlighting (separate task).
- No new backend endpoints.
- No changes to `EventRead` / `TaskReviewDocument` schemas.
- No changes to the polling cadence.
- No storybook story, no design tokens, no theming system.
- Do not modify `ReviewBreakdown` to group its findings by gate — leave it
  alone.

## Acceptance criteria

- `GateStatusPanel` renders in `TaskDetailPage` above the existing sections.
- For the committed P0 E2E test task (P69-8), all 5 wired gates show `pass`,
  `semantic_review` shows `skipped`. Verify manually via Playwright or
  screenshot.
- For a task with 0 events, the panel shows the "Develop pipeline has not
  produced gate results yet." copy.
- For a failing gate (e.g. a manually-fabricated events array in the unit
  test), the fail pill + a single expanded failure row are rendered.
- `pnpm --filter web test` (or whatever the existing test command is — check
  `package.json` scripts) passes including the new file.
- `pnpm --filter web build` succeeds.
- No lint warnings introduced.

## Out of scope for this task

- DiffViewer highlight.js integration → next task.
- Wiring semantic_review into the orchestrator → backend task, not this.
- Drilling from a gate card into filtered findings → can be a follow-up.
