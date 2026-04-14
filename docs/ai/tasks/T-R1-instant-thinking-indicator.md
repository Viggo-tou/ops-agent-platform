# T-R1 — Instant Thinking Indicator on Message Send

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Frontend root: `apps/web/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.

## Goal

Show the thinking indicator IMMEDIATELY when the user sends a message, before the API response arrives. Currently the user types a message, hits send, and sees nothing for 10-30 seconds while the backend processes. This makes it look like the app is frozen.

## Background

The current flow in ChatPage.tsx:
1. User clicks Send
2. `handleSendMessage()` calls `POST /api/tasks` 
3. Waits for API response (~10-30 seconds for MiniMax translation + planning)
4. Only then adds the message to the message list and starts polling

The fix: immediately add a "thinking" message to the UI on send, before the API call.

## Design

In `apps/web/src/pages/chat/ChatPage.tsx`, modify `handleSendMessage()`:

```typescript
async function handleSendMessage(text: string) {
  // 1. IMMEDIATELY add user message + thinking indicator to UI
  const tempId = `temp-${Date.now()}`;
  setMessages(prev => [
    ...prev,
    { id: tempId, role: 'user', content: text, timestamp: new Date().toISOString() },
    { id: `${tempId}-thinking`, role: 'assistant', content: '', timestamp: new Date().toISOString(), isThinking: true }
  ]);
  
  // 2. Scroll to bottom
  // (existing scroll logic)
  
  // 3. THEN make the API call
  try {
    const result = await api.createTask({ request: text, session_id: sessionId });
    // 4. Replace thinking message with real response once available
    // ... existing polling logic
  } catch (err) {
    // Replace thinking message with error
    setMessages(prev => prev.filter(m => m.id !== `${tempId}-thinking`));
    // show error
  }
}
```

The key change: step 1 happens SYNCHRONOUSLY before the await, so the UI updates instantly.

## Files to edit

1. `apps/web/src/pages/chat/ChatPage.tsx` — modify `handleSendMessage()` to immediately show user message + thinking indicator before API call.

## Acceptance criteria

- User clicks Send → thinking indicator appears within 100ms (before API responds).
- User's message appears in the chat immediately.
- Once the API responds and task processing begins, the thinking indicator transitions to real status updates.
- If the API call fails, the thinking indicator is replaced with an error message.

## Workflow (for the executor)

1. Read `apps/web/src/pages/chat/ChatPage.tsx` — focus on `handleSendMessage()` and the message state management.
2. Read `apps/web/src/components/chat/ThinkingIndicator.tsx` — understand the existing thinking component.
3. Modify `handleSendMessage()` to add messages immediately.
4. Test: open browser, type message, verify indicator appears instantly.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-R1-instant-thinking-indicator.md
```
