# T-Q1 — Real-Time Progress Indicator & Typewriter Text Effect

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

Improve the chat UX so users see real-time progress during task execution (like "thinking", "reading files", "generating code") and agent replies appear with a typewriter character-by-character animation. Currently the UI freezes after submission and only updates after the full pipeline completes, making users think the app is broken.

## Background

Phase Q of the multi-agent MVP roadmap. The chat currently polls task events every 5 seconds and only renders the final agent reply once `task.status` reaches `completed` or `failed`. Users see nothing happening between submission and final result.

Existing infrastructure:
- `ChatPage.tsx` polls `api.getTaskEvents()` every 5000ms via `refetchInterval`.
- `EventTimeline.tsx` renders lifecycle events (Chinese text) between user message and agent reply.
- `MessageList.tsx` renders the full agent reply text immediately via `buildAgentReply()`.
- `styles.css` has `.event-timeline-*` styles already.

## Design

### 1. TypingText component

New file: `apps/web/src/components/chat/TypingText.tsx`

A component that reveals text character by character.

```tsx
interface TypingTextProps {
  text: string;        // Full text to display
  speed?: number;      // ms per character, default 18
  enabled?: boolean;   // when false, render full text immediately (for already-seen messages)
}
```

- Uses `useState` + `useEffect` with `setInterval` to increment displayed character count.
- When `enabled` is false or animation completes, renders full text directly (no wrapper span).
- Includes a blinking cursor `<span className="typing-cursor" />` during animation.
- Resets animation when `text` changes.

### 2. ThinkingIndicator component

New file: `apps/web/src/components/chat/ThinkingIndicator.tsx`

Shows a pulsing status line when the task is still processing.

```tsx
interface ThinkingIndicatorProps {
  status: string;      // task status
  events: EventRecord[];
  latestEventType: string | null;
}
```

Logic to derive the display message from the latest event:

| Latest event_type | Display |
|---|---|
| `task_created` / `semantic_translation_started` | "正在思考…" |
| `semantic_translation_completed` / `planning_started` | "正在分析需求…" |
| `plan_generated` / `review_started` | "正在审查计划…" |
| `review_passed` / `execution_started` | "正在执行…" |
| `tool_call_requested` with tool_name containing "codegen" | "正在生成代码…" |
| `tool_call_requested` with tool_name containing "jira" | "正在读取 Jira…" |
| `tool_call_requested` with tool_name containing "sandbox" | "正在应用补丁…" |
| `tool_call_requested` with tool_name containing "test" | "正在运行测试…" |
| `tool_call_requested` with tool_name containing "diff_reviewer" | "正在审查代码…" |
| `approval_requested` | "等待审批中…" |
| Default (task still active) | "处理中…" |

Renders as:
```html
<div class="thinking-indicator">
  <span class="thinking-dot" />
  <span class="thinking-text">{message}</span>
</div>
```

The dot has a CSS pulse animation. The text updates as events arrive.

### 3. MessageList changes

In `MessageList.tsx`:

- Import `TypingText` and `ThinkingIndicator`.
- For the **last task in the thread** that is NOT completed/failed/rolled_back:
  - Show `<ThinkingIndicator>` instead of the agent reply bubble.
  - The indicator uses the latest event from `eventsMap[messageTask.id]` to determine what step is happening.
- For the **last task** when it just completed (within the last 30 seconds based on `updated_at`):
  - Wrap agent reply text in `<TypingText text={agentReply} enabled={true} />`.
- For all other tasks (older, already seen):
  - Render agent reply with `<TypingText enabled={false} />` (immediate render, no animation).

Key logic:
```tsx
const isActive = !["completed", "failed", "rolled_back"].includes(messageTask.status);
const isLastTask = index === visibleTasks.length - 1;
const justCompleted = messageTask.status === "completed" && 
  (Date.now() - new Date(messageTask.updated_at).getTime() < 30_000);
const shouldAnimate = isLastTask && justCompleted;
```

### 4. Polling interval change

In `ChatPage.tsx`, change event polling from 5000ms to 2000ms for active tasks:

```tsx
refetchInterval:
  messageTask.status === "completed" || messageTask.status === "failed" || messageTask.status === "rolled_back"
    ? false
    : 2_000,
```

Also change task query `refetchInterval` from 5000 to 3000:
```tsx
refetchInterval: taskId ? 3_000 : false,
```

### 5. CSS additions

Add to `apps/web/src/styles.css`:

```css
/* Thinking indicator */
.thinking-indicator {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem 1rem;
  margin: 0.25rem 0;
  color: #666;
  font-size: 0.85rem;
}
.thinking-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #999;
  animation: thinking-pulse 1.2s ease-in-out infinite;
}
@keyframes thinking-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.1); }
}
.thinking-text {
  animation: thinking-fade 1.2s ease-in-out infinite;
}
@keyframes thinking-fade {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* Typing cursor */
.typing-cursor {
  display: inline-block;
  width: 2px;
  height: 1em;
  background: #333;
  margin-left: 1px;
  vertical-align: text-bottom;
  animation: cursor-blink 0.8s step-end infinite;
}
@keyframes cursor-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}
```

## Files to create

1. `apps/web/src/components/chat/TypingText.tsx`
2. `apps/web/src/components/chat/ThinkingIndicator.tsx`

## Files to edit

3. `apps/web/src/components/chat/MessageList.tsx` — integrate TypingText and ThinkingIndicator.
4. `apps/web/src/pages/chat/ChatPage.tsx` — reduce polling intervals.
5. `apps/web/src/styles.css` — add thinking + typing CSS.

## Acceptance criteria

- `npx.cmd tsc --noEmit` passes in `apps/web/`.
- While a task is processing, a pulsing indicator shows the current step in Chinese.
- The indicator updates as new events arrive (every ~2 seconds).
- When the task completes, the agent reply text appears character by character.
- Older messages in the thread render instantly (no animation).
- CSS matches project style (minimal, monochrome).

## Workflow (for the executor)

<!-- Effort: medium — pure frontend with known patterns -->

1. Read `apps/web/src/components/chat/MessageList.tsx`, `apps/web/src/components/chat/EventTimeline.tsx`, `apps/web/src/pages/chat/ChatPage.tsx`, `apps/web/src/types.ts`, `apps/web/src/styles.css`.
2. Create `TypingText.tsx` and `ThinkingIndicator.tsx`.
3. Edit `MessageList.tsx` to integrate both components.
4. Edit `ChatPage.tsx` to reduce polling intervals.
5. Add CSS.
6. Run `cd apps/web && npx.cmd tsc --noEmit`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q1-streaming-progress-ux.md
```
