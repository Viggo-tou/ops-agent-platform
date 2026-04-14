# T-H1 — Chat Lifecycle Event Rendering

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: xhigh -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Frontend root: `apps/web/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v` (backend).
Compile check (backend): `python -m compileall app`.
Frontend type check: `cd apps/web && npx tsc --noEmit`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Render task lifecycle events as natural-language Chinese status messages in the chat panel, between the user's message and the final agent reply. The user should see real-time progress like "正在规划…", "测试 3/3 通过", "等待审批", "已写回 Jira" — not raw JSON.

## Background

Phase H of the multi-agent MVP roadmap. The infrastructure is ready:
- Backend: `GET /api/tasks/{task_id}/events` returns `EventRecord[]` (already implemented).
- Frontend: `api.getTaskEvents()` exists in `apps/web/src/lib/api.ts:112`. `EventRecord` type exists in `apps/web/src/types.ts:210`.
- `ChatPage.tsx` already polls the task detail every 5 seconds (`refetchInterval: 5_000`).
- `MessageList.tsx` renders user message → agent reply, but has no event timeline between them.

What's missing: fetching events per task, mapping event_type to Chinese text, rendering them as a timeline between the user bubble and the agent reply bubble.

## Design

### 1. Event polling in ChatPage.tsx

For each task in the thread, add an event query:

```tsx
const eventQueries = useQueries({
  queries: visibleTasks.map((t) => ({
    queryKey: ["task-events", t.id],
    queryFn: () => api.getTaskEvents(t.id),
    refetchInterval: t.status === "completed" || t.status === "failed" || t.status === "rolled_back" ? false : 5_000,
  })),
});
```

Pass events to `MessageList` as a map: `Record<string, EventRecord[]>`.

### 2. Event → Chinese text mapping

New file: `apps/web/src/components/chat/EventTimeline.tsx`

A pure function `formatEventMessage(event: EventRecord): string | null` that maps `event_type` → Chinese text. Return `null` for events that should be hidden (internal bookkeeping).

| event_type | Chinese text |
|-----------|-------------|
| `task_created` | "任务已创建" |
| `semantic_translation_started` | "正在理解请求…" |
| `semantic_translation_completed` | "请求解析完成" |
| `planning_started` | "正在生成执行计划…" |
| `plan_generated` | "执行计划已生成" |
| `review_started` | "正在审查计划…" |
| `review_passed` | "审查通过 ✓" |
| `review_failed` | "审查未通过 ✗" |
| `tool_call_requested` | "正在调用工具：{tool_name}…" (from payload or event.tool_name) |
| `tool_succeeded` | "工具调用成功：{tool_name}" |
| `tool_failed` | "工具调用失败：{tool_name}" |
| `tool_timed_out` | "工具调用超时：{tool_name}" |
| `approval_requested` | "等待审批：{action_name}" (from payload) |
| `approval_granted` | "审批已通过 ✓" |
| `approval_rejected` | "审批已拒绝 ✗" |
| `execution_started` | "正在执行…" |
| `execution_completed` | "执行完成 ✓" |
| `execution_failed` | "执行失败 ✗" |
| `rollback_requested` | "正在回滚…" |
| `rollback_completed` | "回滚完成" |
| `knowledge_retrieved` | "知识检索完成" |

Hidden (return null): `task_status_changed`, `policy_evaluation_started`, `policy_evaluation_completed`, `final_response_emitted`, `semantic_translation_failed`, `approval_assigned`, `approval_expired`, `approval_cancelled`, `guardrail_triggered`.

Unknown event types → return `null` (don't crash on new event types).

### 3. EventTimeline component

```tsx
interface EventTimelineProps {
  events: EventRecord[];
}

export function EventTimeline({ events }: EventTimelineProps) {
  const visibleEvents = events
    .map((e) => ({ event: e, text: formatEventMessage(e) }))
    .filter((item) => item.text !== null);

  if (visibleEvents.length === 0) return null;

  return (
    <div className="event-timeline">
      {visibleEvents.map(({ event, text }) => (
        <div className="event-timeline-item" key={event.id}>
          <span className="event-timeline-text">{text}</span>
          <span className="event-timeline-time">
            {new Date(event.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </span>
        </div>
      ))}
    </div>
  );
}
```

### 4. Integration in MessageList.tsx

Add `eventsMap?: Record<string, EventRecord[]>` to `MessageListProps`. Between the user bubble and the agent reply bubble, render:

```tsx
{eventsMap?.[messageTask.id]?.length ? (
  <EventTimeline events={eventsMap[messageTask.id]} />
) : null}
```

### 5. CSS

Add to `apps/web/src/styles.css`:

```css
.event-timeline {
  margin: 0 3.5rem;
  padding: 0.5rem 0;
}
.event-timeline-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.2rem 0;
  font-size: 0.8rem;
  color: #666;
}
.event-timeline-text {
  flex: 1;
}
.event-timeline-time {
  font-size: 0.7rem;
  color: #999;
  white-space: nowrap;
}
```

Minimal, matches the project's design language: white background, no decorative elements, restrained gray text.

## Files to create

1. `apps/web/src/components/chat/EventTimeline.tsx`

## Files to edit

2. `apps/web/src/pages/chat/ChatPage.tsx` — add event queries, pass eventsMap to MessageList.
3. `apps/web/src/components/chat/MessageList.tsx` — accept eventsMap prop, render EventTimeline.
4. `apps/web/src/styles.css` — add timeline CSS.

## Acceptance criteria

- `npx tsc --noEmit` passes in `apps/web/`.
- Events render as Chinese status messages between user message and agent reply.
- Completed/failed/rolled_back tasks stop polling events.
- Unknown event types are silently hidden (no crash).
- Timeline disappears if there are no visible events.
- CSS matches project style: minimal, monochrome, no gradients or decorative elements.

## Workflow (for the executor)

<!-- Effort: xhigh -->

1. Read `apps/web/src/pages/chat/ChatPage.tsx`, `apps/web/src/components/chat/MessageList.tsx`, `apps/web/src/types.ts`, `apps/web/src/lib/api.ts`, `apps/web/src/styles.css`.
2. Create `apps/web/src/components/chat/EventTimeline.tsx` with `formatEventMessage()` and `EventTimeline` component.
3. Edit `ChatPage.tsx` to add event queries and pass eventsMap.
4. Edit `MessageList.tsx` to accept eventsMap and render EventTimeline.
5. Add CSS to `styles.css`.
6. Run `cd apps/web && npx tsc --noEmit`.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-H1-chat-lifecycle-rendering.md
```
