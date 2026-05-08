# SSE Event Contract — Task Live Stream

## Endpoint

```
GET /api/tasks/{task_id}/events/stream
Accept: text/event-stream
```

Returns: `text/event-stream` (Server-Sent Events).
Auth: same as `/api/tasks/{id}` (X-Actor-Name / X-Actor-Role headers carried via query if needed).

## Stream lifecycle

1. Client opens EventSource on task detail page mount.
2. Server sends a `snapshot` event first (current task state + all events so far).
3. Server pushes new events as they happen (lifecycle / gate / approval / completion).
4. Server sends `done` when task reaches a terminal state (completed / failed / awaiting_approval).
5. Client closes EventSource on `done` OR on page unmount.

## Event types

All events are JSON-serialized in `data:` field.

### `event: snapshot`

Sent once at stream open. Full task state plus all historical events for replay.

```json
{
  "task": {
    "id": "...",
    "status": "executing",
    "workflow_stage": "action",
    "title": "...",
    "scenario": "jira_issue_develop"
  },
  "events": [ /* array of past events, oldest first */ ],
  "pipeline_state": {
    "diff_shape_done": true,
    "compile_gate_done": false,
    "..."
  }
}
```

### `event: status`

Task status / stage transition.

```json
{
  "status": "awaiting_approval",
  "workflow_stage": "review",
  "previous_status": "executing",
  "timestamp": "2026-05-08T..."
}
```

### `event: gate`

Gate fired, succeeded, or failed.

```json
{
  "gate": "compile_gate",
  "verdict": "PASS" | "FAIL" | "SKIPPED",
  "duration_ms": 35420,
  "details": {
    "errors": [],
    "skipped_reason": "no_strict_tokens"
  },
  "timestamp": "2026-05-08T..."
}
```

Gate names enumerated:
- `diff_shape_check`
- `diff_symbol_verifier` (Leg 2)
- `compile_gate`
- `compile_repair_round` (with `round_index` in details)
- `feature_presence_check`
- `symbol_graph`
- `runtime_validation`
- `semantic_review` (with `completeness_pct`, `high_severity_count` in details)
- `diff_reviewer`
- `spec_conformance`
- `goal_attestation`
- `goal_decomposition`
- `artifact_existence`
- `evidence_chain`
- `reservations_review`

### `event: codegen_progress`

Codegen call started / completed (for showing "Claude is generating code...").

```json
{
  "phase": "started" | "completed",
  "provider": "claude_code",
  "files_target": ["app/src/main/.../File.kt"],
  "duration_ms": 45000,
  "diff_chars": 7200
}
```

### `event: repair`

Repair attempt (compile_repair, semantic_review_repair, intent_drop_retry).

```json
{
  "repair_type": "compile_repair" | "semantic_review_repair" | "intent_drop_retry",
  "round": 1,
  "trigger": "Unresolved reference 'getHomeAddress'",
  "outcome": "started" | "succeeded" | "exhausted" | "intent_dropped",
  "details": { "intent_preservation_ratio": 0.242 }
}
```

### `event: approval_requested`

Task entered AWAITING_APPROVAL.

```json
{
  "approval_id": "...",
  "action_name": "jira.transition_issue",
  "approver_role": "team_lead",
  "reason": "...",
  "payload_summary": "..."
}
```

### `event: log`

Arbitrary lifecycle log message (mirrors `record_event` calls).

```json
{
  "level": "info" | "warning" | "error",
  "source": "orchestrator",
  "message": "Compile repair round 1 starting (1 file(s) queued)."
}
```

### `event: heartbeat`

Sent every 30s when no other events. Keeps connection alive against proxies.

```
event: heartbeat
data: {"ts": 1715164800}
```

### `event: done`

Terminal event. After this, server closes the stream.

```json
{
  "final_status": "completed" | "failed" | "awaiting_approval",
  "final_stage": "done"
}
```

### `event: error`

Server-side stream error (rare). Client should reconnect with last-event-id.

```json
{
  "code": "internal" | "timeout",
  "message": "..."
}
```

## Reconnection

Client SHOULD send `Last-Event-ID` header on reconnect; server replays missed events from that ID. Each event sent has a monotonic `id:` field.

## Backend dispatch model

- Single in-process queue per task (asyncio.Queue keyed on task_id).
- All `record_event(...)` callers ALSO push to the per-task queue (small wrapper).
- Stream endpoint reads from queue + heartbeat timer.
- On task terminal status, push `done` and remove queue.

## Frontend consumer skeleton

```ts
const ev = new EventSource(`/api/tasks/${taskId}/events/stream`);
ev.addEventListener('snapshot', (e) => setInitialState(JSON.parse(e.data)));
ev.addEventListener('status', (e) => updateStatus(JSON.parse(e.data)));
ev.addEventListener('gate', (e) => appendGateEvent(JSON.parse(e.data)));
ev.addEventListener('repair', (e) => showRepairAnimation(JSON.parse(e.data)));
ev.addEventListener('done', () => { ev.close(); refetchTask(); });
ev.onerror = () => { /* EventSource auto-reconnects */ };
```

## Out of scope for 1.0

- WebSocket bidirectional (SSE one-way is enough for live view)
- Multi-tenant filtering (single-workspace assumption)
- Cursor-based replay beyond Last-Event-ID
- Compression
