# T-R3 — Diff Viewer Component for Code Changes

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Frontend root: `apps/web/`. Run from there.

## Goal

Add a diff viewer component to the chat UI that renders unified diffs as a readable side-by-side or inline code comparison. When the develop pipeline completes, the user should see the actual code changes visually — not raw diff text.

## Background

After T-R2, the develop pipeline response includes the diff string in a markdown code fence. But raw diff text is hard to read. The user specifically requested "Vercel前后对比" — a visual before/after comparison of code changes.

## Design

### 1. DiffViewer component

Create `apps/web/src/components/chat/DiffViewer.tsx`:

A minimal inline diff viewer that parses unified diff text and renders it with:
- Green background for added lines (`+`)
- Red background for removed lines (`-`)
- Gray background for context lines (` `)
- File path header for each file section
- Line numbers on both sides (old and new)

Do NOT use any external diff library — parse the unified diff format manually. The component should be simple and self-contained.

```tsx
interface DiffViewerProps {
  diff: string;  // unified diff string
}

// Parse the diff into file sections, each with hunks
// Render each file section with a header and colored lines
```

### 2. Styling

Use the existing project CSS conventions (white background, black text, light borders). Add to `apps/web/src/styles.css`:

```css
.diff-viewer { font-family: monospace; font-size: 13px; border: 1px solid #e0e0e0; border-radius: 4px; overflow-x: auto; }
.diff-file-header { background: #f5f5f5; padding: 8px 12px; font-weight: 600; border-bottom: 1px solid #e0e0e0; }
.diff-line { display: flex; white-space: pre; }
.diff-line-number { width: 40px; text-align: right; padding: 0 8px; color: #999; user-select: none; flex-shrink: 0; }
.diff-line-content { flex: 1; padding: 0 8px; }
.diff-line-add { background: #e6ffec; }
.diff-line-remove { background: #ffebe9; }
.diff-line-context { background: white; }
.diff-hunk-header { background: #f0f0ff; color: #666; padding: 4px 12px; }
```

### 3. Integration in MessageList

In `apps/web/src/components/chat/MessageList.tsx`, detect when a message contains a diff (from the develop pipeline result) and render it with DiffViewer instead of plain text.

Detection: if the message's task result has `scenario === "jira_issue_develop"` and contains a `diff` field, render it with DiffViewer.

Alternatively, if the message content contains a markdown code fence with language `diff`, extract the diff content and render with DiffViewer.

### 4. Collapsible

Wrap the DiffViewer in a collapsible section so users can expand/collapse the diff view. Default: expanded.

```tsx
<details open>
  <summary>Code Changes ({filesChanged} files)</summary>
  <DiffViewer diff={diffString} />
</details>
```

## Files to create

1. `apps/web/src/components/chat/DiffViewer.tsx`

## Files to edit

2. `apps/web/src/styles.css` — add diff viewer styles
3. `apps/web/src/components/chat/MessageList.tsx` — render DiffViewer for develop pipeline results

## Acceptance criteria

- DiffViewer renders added lines in green, removed lines in red, context in white.
- File headers show the file path.
- Line numbers shown on left.
- Hunk headers (`@@`) shown in blue/gray.
- Integrated into chat message display for develop pipeline results.
- Collapsible via `<details>` element.
- No external diff parsing libraries added.

## Workflow (for the executor)

1. Read `apps/web/src/components/chat/MessageList.tsx` — understand message rendering.
2. Read `apps/web/src/styles.css` — understand existing style conventions.
3. Create `DiffViewer.tsx` with diff parsing and rendering.
4. Add CSS styles.
5. Integrate in MessageList.
6. Test by opening browser and viewing a completed develop task.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-R3-diff-viewer-component.md
```
