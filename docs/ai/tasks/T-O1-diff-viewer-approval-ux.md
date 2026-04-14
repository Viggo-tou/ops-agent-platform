# T-O1 — Inline Diff Viewer & Approval Buttons

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Frontend root: `apps/web/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Frontend type check: `cd apps/web && npx.cmd tsc --noEmit`.

## Goal

Add inline diff viewing and approval action buttons to the chat timeline, so users can review code changes and approve/reject tasks entirely from the chat panel.

## Background

Phase O of the multi-agent MVP roadmap. Phase H added the event timeline with Chinese status messages. Phase N wired the full pipeline. Now the chat needs two interactive elements:
1. **Diff viewer** — when a `patch.applied` or `execution_completed` event appears, show a collapsible diff.
2. **Approval buttons** — when an `approval_requested` event appears, show [批准] / [拒绝] buttons.

Existing infrastructure:
- `EventTimeline.tsx` renders events as text lines.
- `api.getTaskEvents()` returns `EventRecord[]` with `payload_json`.
- `api` has no approval action methods yet — need to add `grantApproval()` and `rejectApproval()` (backend endpoints exist: `POST /approvals/{id}/grant` and `POST /approvals/{id}/reject`).
- `useAuth()` provides `can("approval:decide")` for RBAC gating.

## Design

### 1. DiffBlock component

New file: `apps/web/src/components/chat/DiffBlock.tsx`

```tsx
interface DiffBlockProps {
  diff: string;
  summary: string;
}

export function DiffBlock({ diff, summary }: DiffBlockProps) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="diff-block">
      <button className="diff-toggle" onClick={() => setExpanded(!expanded)}>
        {expanded ? "▼" : "▶"} {summary}
      </button>
      {expanded && <pre className="diff-content">{diff}</pre>}
    </div>
  );
}
```

### 2. ApprovalActions component

New file: `apps/web/src/components/chat/ApprovalActions.tsx`

```tsx
interface ApprovalActionsProps {
  approvalId: string;
  actionName: string;
  onDecision?: () => void;
}

export function ApprovalActions({ approvalId, actionName, onDecision }: ApprovalActionsProps) {
  const { user, backendActorRole, can } = useAuth();
  const queryClient = useQueryClient();
  const [decided, setDecided] = useState(false);

  const grantMutation = useMutation({
    mutationFn: () => api.grantApproval(approvalId, { actor_name: user?.name ?? "unknown", actor_role: backendActorRole }),
    onSuccess: () => { setDecided(true); queryClient.invalidateQueries({ queryKey: ["tasks"] }); onDecision?.(); },
  });
  const rejectMutation = useMutation({
    mutationFn: () => api.rejectApproval(approvalId, { actor_name: user?.name ?? "unknown", actor_role: backendActorRole }),
    onSuccess: () => { setDecided(true); queryClient.invalidateQueries({ queryKey: ["tasks"] }); onDecision?.(); },
  });

  if (!can("approval:decide") || decided) return null;

  return (
    <div className="approval-actions">
      <span className="approval-label">审批：{actionName}</span>
      <button className="approval-btn approve" onClick={() => grantMutation.mutate()} disabled={grantMutation.isPending}>
        批准
      </button>
      <button className="approval-btn reject" onClick={() => rejectMutation.mutate()} disabled={rejectMutation.isPending}>
        拒绝
      </button>
    </div>
  );
}
```

### 3. API methods

Add to `apps/web/src/lib/api.ts`:

```typescript
grantApproval: (approvalId: string, payload: { actor_name: string; actor_role: string }) =>
  request<Approval>(`/approvals/${approvalId}/grant`, { method: "POST", body: JSON.stringify(payload) }),
rejectApproval: (approvalId: string, payload: { actor_name: string; actor_role: string }) =>
  request<Approval>(`/approvals/${approvalId}/reject`, { method: "POST", body: JSON.stringify(payload) }),
```

### 4. Integration in EventTimeline

In `EventTimeline.tsx`, enhance the rendering for specific event types:

- When `event_type === "execution_completed"` or event payload contains a `diff` field:
  → Render `<DiffBlock>` with the diff from `payload_json.diff` or `payload_json.patch_stats`.
  
- When `event_type === "approval_requested"`:
  → Render `<ApprovalActions>` with `approvalId` and `actionName` from `payload_json`.

### 5. CSS

Add to `apps/web/src/styles.css`:

```css
/* Diff viewer */
.diff-block { margin: 0.25rem 0; }
.diff-toggle {
  background: none; border: 1px solid #ddd; border-radius: 4px;
  padding: 0.25rem 0.5rem; cursor: pointer; font-size: 0.8rem; color: #333;
}
.diff-toggle:hover { background: #f5f5f5; }
.diff-content {
  margin: 0.25rem 0 0; padding: 0.5rem; background: #fafafa;
  border: 1px solid #eee; border-radius: 4px; font-size: 0.75rem;
  overflow-x: auto; white-space: pre; max-height: 400px; overflow-y: auto;
}

/* Approval buttons */
.approval-actions {
  display: flex; align-items: center; gap: 0.5rem;
  margin: 0.25rem 0; padding: 0.25rem 0;
}
.approval-label { font-size: 0.8rem; color: #666; }
.approval-btn {
  padding: 0.2rem 0.75rem; border-radius: 4px; font-size: 0.8rem;
  cursor: pointer; border: 1px solid #ddd;
}
.approval-btn.approve { color: #166534; border-color: #86efac; }
.approval-btn.approve:hover { background: #dcfce7; }
.approval-btn.reject { color: #991b1b; border-color: #fca5a5; }
.approval-btn.reject:hover { background: #fee2e2; }
.approval-btn:disabled { opacity: 0.5; cursor: not-allowed; }
```

Minimal, monochrome with subtle color hints for approve (green) / reject (red).

## Files to create

1. `apps/web/src/components/chat/DiffBlock.tsx`
2. `apps/web/src/components/chat/ApprovalActions.tsx`

## Files to edit

3. `apps/web/src/components/chat/EventTimeline.tsx` — render DiffBlock and ApprovalActions for relevant events.
4. `apps/web/src/lib/api.ts` — add `grantApproval`, `rejectApproval`.
5. `apps/web/src/styles.css` — add diff + approval CSS.

## Acceptance criteria

- `npx.cmd tsc --noEmit` passes in `apps/web/`.
- Diff block renders as collapsible for events with diff payload.
- Approval buttons render for `approval_requested` events.
- Buttons are hidden if user lacks `approval:decide` permission.
- Buttons disappear after a decision is made.
- CSS matches project style.

## Workflow (for the executor)

<!-- Effort: medium — pure frontend wiring with known patterns -->

1. Read `apps/web/src/components/chat/EventTimeline.tsx`, `apps/web/src/lib/api.ts`, `apps/web/src/lib/auth.tsx`, `apps/web/src/types.ts`, `apps/web/src/styles.css`.
2. Create `DiffBlock.tsx` and `ApprovalActions.tsx`.
3. Edit `EventTimeline.tsx` to render them.
4. Add API methods to `api.ts`.
5. Add CSS.
6. Run `cd apps/web && npx.cmd tsc --noEmit`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-O1-diff-viewer-approval-ux.md
```
