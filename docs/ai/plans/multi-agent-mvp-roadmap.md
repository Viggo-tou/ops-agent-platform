# Multi-Agent MVP Roadmap

Target demo: a user opens the chat page, says "把 Jira 工单 OPS-123 做了", and the platform runs a multi-agent pipeline that reads the Jira ticket, plans changes, executes them inside a sandboxed clone of the target repo, runs the test pipeline, gets a Reviewer verdict, routes through an approval gate, writes status back to Jira, and — crucially — streams natural-language progress updates into the same chat (current phase, what changed, what tests said, whether it needs approval, how to roll back). No JSON blobs, no separate dashboard.

This file is the single source of truth for how we get there. Update it as phases land.

## Required capabilities (user-stated acceptance list)

1. **Jira integration** — read a ticket by key, write status / comments back.
2. **Code repo sandbox execution** — clone a target repo into an isolated workdir, run shell commands and patches there, never touch the host checkout.
3. **Reproducible test pipeline** — deterministic test commands per-repo (`tests.yaml` or similar), captured stdout/stderr/exit code, reported as structured results.
4. **Reviewer / rule checker** — an LLM or rule-based stage that reads the diff + test output and produces a pass / block verdict with reasons. Blocks bad diffs before approval.
5. **Approval and permission control** — high-risk actions (merge, push, Jira transition, destructive shell) require an explicit approval from a role with the permission. Already partially in place via `Approval` + `PolicyRule` + `RbacRole`.
6. **Task evaluation and rollback** — every run is a Task; outcomes are recorded; any mutation has a recorded inverse so the operator can roll the task back (revert commit, undo Jira transition, restore file state).
7. **Chat-first UX** — the chat panel shows natural-language status: "正在克隆仓库…", "已生成补丁，修改了 `apps/backend/app/api/memory.py` 第 42–57 行", "测试 3/3 通过", "等待 operator 审批", "已写回 Jira OPS-123 为 In Review". Diagnostic JSON stays collapsed.

## Current state (2026-04-12)

The repo already has more of this than it looks.

- **Tasks & events:** `Task`, `Event`, `ToolExecution` models exist. Orchestrator is single-runtime and already emits lifecycle events. Good enough foundation — no queue/worker split yet, per `DECISIONS.md`.
- **Governance:** `RbacRole`, `PolicyRule`, `Approval` models exist, `DEFAULT_RBAC_ROLES` + `DEFAULT_POLICY_RULES` seeded. Frontend RBAC guards (`can(permission)`) are in place but **not yet enforced at the backend** — that's T-026-04.
- **Knowledge:** upload / delete / list / sync APIs done in T-026-01.
- **Memory:** CRUD + settings persistence done in T-026-02.
- **Model config:** still hardcoded on the frontend (`ModelSelector.tsx`, `modelGroups`). That's T-026-03.
- **Chat UX:** plain chat exists, but does not yet render task lifecycle events as natural language. Does not know about Jira, sandbox, or reviewer.
- **Jira:** already partially built. `apps/backend/app/core/jira.py` extracts issue keys / URLs from free text. `jira.get_issue` and `jira.create_issue` tools exist in the tool registry and gateway (`apps/backend/app/tools/registry.py`, `apps/backend/app/tools/gateway.py`). `apps/backend/app/agents/service.py` has `jira_issue_plan` and `jira_issue_create` scenarios wired. **Missing:** `jira.transition_issue` (status writeback) and `jira.add_comment` (progress notes back to the ticket) — that's the Phase B delta.
- **Sandbox:** none. Today the orchestrator edits in-process.
- **Test pipeline:** none (there is no `tests.yaml`, no runner).
- **Reviewer stage:** none as a pipeline step. The knowledge-answer chain has a review step (the T-028 fix), but there is no "review a diff" stage.
- **Rollback:** `POST /tasks/{id}/rollback` endpoint exists but is not wired to concrete inverse operations for filesystem / git / Jira mutations.

So the gap is: **Jira client, sandbox runner, test pipeline, diff-reviewer stage, rollback inverses, chat lifecycle rendering** — plus finishing the governance wiring that T-026-04/05 covers.

## Phased plan

Each phase ends in a demo-able increment. Every phase produces a spec under `docs/ai/tasks/` and runs via codex (or MiniMax for the trivial pieces). Claude reviews and verifies.

### Phase A — Finish T-026 (workbench persistence + RBAC) — **IN PROGRESS**

- [x] T-026-01 Knowledge upload/delete backend + frontend wiring.
- [x] T-026-02 Memory backend persistence.
- [ ] T-026-03 Model/provider config read API + ModelSelector wired to backend.
- [ ] T-026-04 Frontend RBAC ↔ backend `require_actor_role` enforcement.
- [ ] T-026-05 4-role end-to-end RBAC smoke.

**Exit criteria:** Workbench pages all talk to the backend. Permission gates are enforced both sides. No `localStorage` left for product data.

### Phase B — Jira integration (writeback)

Most of the read side is already in place (see "Current state" above). Phase B adds the writeback tools and a new scenario so the pipeline can transition ticket status and post progress comments.

- **B1 — `jira.transition_issue` tool.** Add to `app/tools/registry.py` as an `approval_required` tool. Implement in `app/tools/gateway.py` using the same auth/URL-resolution pattern as the existing Jira tools. Endpoint: `POST /rest/api/3/issue/{key}/transitions` (Jira first lists available transitions, then applies the matching one by ID — handle the two-step lookup inside the executor). Tags: `("jira", "workflow", "state-change")`.
- **B2 — `jira.add_comment` tool.** Add to `app/tools/registry.py` as `approval_required` (Jira comments are visible to the watchers, so we gate them). Endpoint: `POST /rest/api/3/issue/{key}/comment`. Body uses ADF (Atlassian Document Format); wrap plain text in the minimal `{type:"doc",version:1,content:[{type:"paragraph",content:[{type:"text",text:...}]}]}` envelope.
- **B3 — Scenario wiring.** Extend `apps/backend/app/agents/service.py`: `jira_issue_plan` already exists for reads. Add `jira_issue_writeback` that runs `jira.add_comment` followed by `jira.transition_issue`. The orchestrator passes the issue key + target transition name + comment body. Wrap both in a single policy evaluation so a single approval covers the pair.
- **B4 — Policy seed.** Add a `DEFAULT_POLICY_RULES` entry (`apps/backend/app/services/governance.py`) for `jira.transition_issue` and `jira.add_comment` at the `employee` / `team_lead` levels → `REQUIRE_APPROVAL` with `required_approver_role=team_lead`, escalating to `manager` for production-tagged transitions if we want finer control later.
- **B5 — Natural-language chat echo.** When either tool runs, emit a `final_response_emitted` or new lifecycle event whose template is: "已在 Jira {KEY} 添加进度评论" / "已将 Jira {KEY} 状态从 {from} 推进到 {to}". Render via Phase H once H lands; for now, just make sure the event payload has `from_status`, `to_status`, `comment_excerpt` fields so H can format them.

**Exit criteria:** With a valid Jira ticket key, the orchestrator can be asked "把 OPS-123 标记为 in progress 并加上一条评论", the request routes to the new scenario, goes through the approval gate, executes both tool calls via the gateway, and persists `ToolExecution` rows with the transition/comment side effects.

### Phase C — Sandbox execution environment

- **C1 — `ExecutionSandbox` service.** New `app/services/sandbox.py`. Given a `repo_url` (or local path) + `task_id`, clones into `data/sandboxes/<task_id>/` (gitignored), exposes `run(command, cwd=…, timeout=…)` → structured `{exit_code, stdout, stderr, duration_ms}`. Hard timeout, captured output truncated to N KB, working directory pinned under the sandbox root. No network egress beyond the initial clone (enforced by convention first, hardening later).
- **C2 — Tool: `sandbox.run_command`.** Exposed via the tool registry. High-risk. Every invocation records a `ToolExecution` row with full stdout/stderr and exit code. The chat echoes "正在执行 `npm test`…" / "✗ 测试失败：…" in natural language.
- **C3 — Tool: `sandbox.apply_patch`.** Takes a unified diff, applies via `git apply` inside the sandbox. Records the before-state (commit SHA) for rollback.
- **C4 — Teardown.** On task completion or `POST /tasks/{id}/rollback`, the sandbox directory is wiped (or kept read-only for audit — decide during implementation).

**Exit criteria:** Orchestrator can run `sandbox.run_command` from within a task and the chat shows the command, the exit code, and a human summary.

### Phase D — Reproducible test pipeline

- **D1 — `tests.yaml` schema.** Repo-level file declaring ordered test steps: `{name, command, cwd, timeout_seconds, required: bool}`. Shipped in the target repo, not this one.
- **D2 — `TestPipeline` runner.** New `app/services/test_pipeline.py`. Reads `tests.yaml` from the sandbox root, runs each step via `sandbox.run_command`, aggregates into a `TestRunResult` model with per-step status and overall verdict. Persisted as a `ToolExecution` child rows for audit.
- **D3 — Chat echo.** "运行测试流水线：3 步，全部通过 (lint ✓, unit ✓, integration ✓)". Failures expand in a collapsed diagnostic block.

**Exit criteria:** Given a sandbox with a `tests.yaml`, the platform runs the full pipeline, stores results, and reports pass/fail in chat.

### Phase E — Reviewer / rule checker

- **E1 — `DiffReviewer` stage.** New `app/services/reviewer.py`. Input: the applied diff + the test run result + the original Jira task description. Output: `{verdict: "pass"|"block", reasons: [...]}`. Implementation starts with rule-based checks (file allow-list, no secret patterns, forbidden paths, test verdict must be `pass`), then optionally layers an LLM second opinion behind a feature flag.
- **E2 — Pipeline integration.** The orchestrator calls `DiffReviewer.review()` after the test pipeline and before requesting approval. A `block` verdict fails the task with recorded reasons — no approval requested, no writeback.
- **E3 — Chat echo.** "✓ Reviewer 通过：3 条规则校验，0 条阻断" or "✗ Reviewer 阻断：修改触及 `apps/backend/app/core/` 目录（规则 `protected-paths`）".

**Exit criteria:** A diff that violates a reviewer rule is blocked before approval. A clean diff passes the reviewer and proceeds to approval.

### Phase F — Approval & enforcement at sensitive steps

- **F1** (depends on T-026-04). Ensure `require_actor_role` guards all mutating backend endpoints and — new in this phase — high-risk tool executions.
- **F2** Tool-execution approval gate: when a tool tagged high-risk is requested inside a task, the orchestrator pauses, creates an `Approval` row, emits an `approval_requested` event. Chat echoes "等待 operator 审批：Jira writeback / 代码合并 / 沙箱外执行".
- **F3** Once approved, the orchestrator resumes and runs the tool. Once rejected, the task fails with a recorded reason.

**Exit criteria:** No high-risk tool can run without a recorded, role-authorized approval. The audit log shows who approved what and when.

### Phase G — Task evaluation & rollback inverses

- **G1** Every mutating tool execution stores an `inverse_action` descriptor in the `ToolExecution` row: for `git apply` → the base SHA; for `jira.post_status` → the previous status; for `sandbox.run_command` with side effects → a declared undo command.
- **G2** Wire `POST /tasks/{task_id}/rollback` to replay inverse actions in reverse order. Each inverse is itself tool-governed and audit-logged.
- **G3** Chat echoes "已回滚任务 T-001：恢复 OPS-123 状态为 To Do，撤销沙箱提交 abc1234，清理沙箱 /data/sandboxes/T-001/".

**Exit criteria:** Operator can roll back a task end-to-end from the chat, and the audit trail records both the original run and the inverse run.

### Phase H — Chat lifecycle rendering (the visible payoff)

- **H1 — Event stream subscription.** Frontend chat subscribes (SSE or polling — start with polling on a short interval) to `GET /api/tasks/{task_id}/events`. Each new event is rendered as an assistant message in the chat thread.
- **H2 — Event → natural language mapping.** Backend emits lifecycle events with human-friendly templates: `task.started`, `jira.fetched`, `sandbox.cloned`, `patch.applied` (includes file paths + line ranges), `tests.passed` / `tests.failed`, `reviewer.passed` / `reviewer.blocked`, `approval.requested`, `approval.granted`, `jira.updated`, `task.completed`, `task.rolled_back`. Rendering lives in `apps/web/src/components/chat/MessageList.tsx` — one template per event type, Chinese first.
- **H3 — Diff surface.** When a `patch.applied` event fires, the chat message includes a tight summary: "修改了 `apps/backend/app/api/memory.py` (L42-L57，新增 15 行，删除 4 行)". Clicking it expands the full diff inline. No external viewer needed.
- **H4 — Approval inline.** An `approval.requested` event renders as a chat card with `[批准] [拒绝]` buttons (RBAC-gated). The approval path is already in place; this is frontend wiring.

**Exit criteria:** A full Phase B–G run is fully observable from the chat panel alone, in natural language, with diffs and approvals inline. No separate dashboard needed for the happy path.

### Phase I — Structured Logging (structlog)

- **I1 — structlog integration.** Add `structlog` to requirements. Configure JSON output to stdout in `app/core/logging.py`. Replace ad-hoc print/logging calls with structured logger.
- **I2 — Event bridge.** Every `record_event()` call simultaneously emits a structlog entry with the same fields (task_id, event_type, source, stage, role, tool_name). This gives both DB persistence and stdout stream for external log aggregation (ELK/Loki/CloudWatch).
- **I3 — Request logging middleware.** FastAPI middleware that logs every HTTP request: method, path, status code, duration_ms, actor_role. Structured JSON format.

**Exit criteria:** `docker logs` or stdout shows structured JSON for every task lifecycle event and HTTP request. No changes to existing Event model — structlog is a parallel output channel.

### Phase J — OpenTelemetry Tracing

- **J1 — OTel SDK setup.** Add `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi` to requirements. Configure in `app/core/telemetry.py`. Exporter: OTLP (configurable endpoint) or console for local dev.
- **J2 — Orchestrator spans.** Each `bootstrap_task()` call creates a root span. Child spans for: semantic translation, planning, review, execution, approval wait. Tool gateway calls are leaf spans with tool_name, duration, status attributes.
- **J3 — Context propagation.** Pass `trace_id` through the orchestrator → gateway → sandbox chain. Store `trace_id` on the Task model for cross-referencing with external tracing backends (Jaeger/Tempo).

**Exit criteria:** A single task produces a complete trace with nested spans visible in Jaeger or console exporter. Trace ID is queryable from the Task.

### Phase K — Metrics & Cost Tracking

- **K1 — Prometheus metrics.** Add `prometheus-fastapi-instrumentator` or manual `prometheus_client` counters/histograms. Key metrics:
  - `ops_task_total` (counter, labels: scenario, status)
  - `ops_task_duration_seconds` (histogram, labels: scenario)
  - `ops_tool_execution_total` (counter, labels: tool_name, status)
  - `ops_tool_duration_seconds` (histogram, labels: tool_name)
  - `ops_approval_wait_seconds` (histogram, labels: approver_role)
  - `ops_reviewer_verdict_total` (counter, labels: verdict)
- **K2 — `/metrics` endpoint.** Prometheus-compatible scrape endpoint at `/metrics`.
- **K3 — LLM cost tracking.** Every LLM call (planner, translator, reviewer) records: model_name, input_tokens, output_tokens, estimated_cost_usd. Stored in a new `LlmUsage` model or as Event payload. Aggregatable per task, per user, per day.
- **K4 — Cost dashboard API.** `GET /api/admin/costs?group_by=task|user|day` returns aggregated LLM spend.

**Exit criteria:** `/metrics` returns Prometheus-format data. LLM token usage is tracked per task. Admin can query cost breakdown.

### Phase L — Alerting & Health

- **L1 — Health endpoint enhancement.** Extend `GET /health` to include: DB connectivity, last successful task timestamp, pending approval count, tool failure rate (last 1h).
- **L2 — Alert rules (config-driven).** New `app/services/alerts.py` with configurable rules:
  - Tool failure rate > N% in last M minutes
  - Approval pending > N minutes without decision
  - Task queue depth > N
  - LLM cost exceeds daily budget
- **L3 — Alert dispatch.** Webhook-based: POST alert payload to a configurable URL (Slack incoming webhook, PagerDuty, generic). No built-in Slack SDK — just HTTP POST with JSON body.

**Exit criteria:** Health endpoint reports operational metrics. Alert rules fire when thresholds are breached. Webhook delivers alerts to configured endpoint.

### Phase M — Code Generation Tool (keystone for end-to-end demo)

The missing piece that connects "plan" to "sandbox execution". Without this, the orchestrator can plan what to do but cannot produce actual code changes.

- **M1 — `codegen.generate_patch` tool.** New `app/services/codegen.py`. Input: plan document (steps, affected files, objective) + repository context (file contents from knowledge or sandbox). Output: unified diff string ready for `sandbox.apply_patch`. Implementation: calls the configured LLM provider (MiniMax or OpenAI) with a code-generation prompt. Falls back to a structured error if the provider is unavailable.
- **M2 — Tool registration.** Register `codegen.generate_patch` in the tool registry as `APPROVAL_REQUIRED` (generated code must be reviewed before application). Gateway executor: accepts `plan_json`, `context_files: dict[str, str]` (filename → content), `task_description`. Returns `{diff: str, summary: str, files_changed: list[str]}`.
- **M3 — LLM cost integration.** Every codegen call records a `LlmUsage` row (Phase K) with `purpose="codegen"`, tracking token consumption for code generation specifically.
- **M4 — Prompt engineering.** The codegen prompt must: produce valid unified diff format, respect the plan's affected_code_locations, include only necessary changes (no unrelated refactors), handle multi-file diffs. Include a system prompt that enforces diff format discipline.

**Exit criteria:** Given a plan document and file context, `codegen.generate_patch` returns a valid unified diff. The diff can be applied via `sandbox.apply_patch` and passes `DiffReviewer.review()`.

### Phase N — End-to-End Pipeline Orchestration

Wire the full automated pipeline: user request → Jira fetch → plan → codegen → sandbox apply → test → review → approve → writeback. This is the "it all works together" phase.

- **N1 — Pipeline executor in orchestrator.** Extend `_execute_plan()` to run the full sequence for code-change scenarios:
  1. `codegen.generate_patch` (from plan + repo context)
  2. `sandbox.apply_patch` (apply the generated diff)
  3. `test_pipeline.run` (run tests in sandbox)
  4. `diff_reviewer.review` (check the diff)
  5. If reviewer passes → request approval
  6. If approved → `jira.transition_issue` + `jira.add_comment`
  7. Record all inverse actions for rollback
- **N2 — Context gathering.** Before codegen, the orchestrator must gather file contents for the affected paths. Source: knowledge index (if synced) or sandbox clone (read files from the cloned repo). New helper: `_gather_codegen_context(plan, sandbox) -> dict[str, str]`.
- **N3 — New scenario: `jira_issue_develop`.** classify_request detects "做了", "implement", "fix", "develop" + Jira key → routes to this scenario which runs the full N1 pipeline instead of just planning.
- **N4 — Failure handling.** If codegen produces invalid diff → fail with clear message. If tests fail → fail before review. If reviewer blocks → fail with reasons. Each failure records events for the chat timeline (Phase H).

**Exit criteria:** User says "把 OPS-123 做了" in the chat → system fetches the ticket, generates a plan, produces code, applies it in sandbox, runs tests, reviews the diff, requests approval, and writes back to Jira. The entire flow is visible in the chat timeline.

### Phase O — Diff Viewer & Approval UX (completing H3/H4)

Frontend enhancements to make the pipeline output actionable in the chat.

- **O1 — Inline diff viewer.** When a `patch.applied` event fires, the chat message includes a summary ("修改了 `app/api/memory.py` L42-57, +15/-4 行"). Clicking expands the full unified diff with syntax highlighting (use a lightweight diff renderer, no external viewer).
- **O2 — Approval buttons.** When `approval.requested` event renders, show `[批准]` / `[拒绝]` buttons (RBAC-gated). Clicking calls the existing approval API endpoints.
- **O3 — Before/after preview.** For sandbox-based changes, show a split view of the file before and after the patch. Source: `sandbox.apply_patch` result includes before_sha; read file at both SHAs.

**Exit criteria:** The full Phase N pipeline is observable and actionable entirely from the chat panel — user sees the diff, can approve/reject inline, and sees the final Jira writeback confirmation.

## Ordering and dependencies

```
功能层：
Phase A ─► B ─► C ─► D ─► E ─► F ─► G ─► H ─► M ─► N ─► O
                                                 │
可观测层：                                        ├─► I ─► J ─► K ─► L
```

- Phase M (codegen) depends on: E (reviewer), C (sandbox), D (test pipeline), K (cost tracking). Can start after L.
- Phase N (e2e orchestration) depends on M. This is the integration phase.
- Phase O (diff UI + approval UX) depends on N. Frontend polish after the pipeline works.
- I–L (observability) are independent of M–O and are already complete or in progress.

## Where we are right now (2026-04-13)

- ✅ Phase A (partial): A-1, A-2 done. A-3, A-4, A-5 not yet complete.
- ✅ Phase B: Jira writeback tools + scenario wiring done.
- ✅ Phase C: Sandbox execution environment done.
- ✅ Phase D: Test pipeline runner done.
- ✅ Phase E: DiffReviewer service done (8 tests).
- ✅ Phase F: Tool-execution approval gate done (6 tests).
- ✅ Phase G: Rollback inverses done (8 tests).
- ✅ Phase H: Chat lifecycle rendering done (tsc clean).
- ✅ Phase I: structlog done (5 tests).
- ✅ Phase J: OpenTelemetry tracing done (5 tests).
- ✅ Phase K: Metrics + cost tracking done (7 tests).
- ⏳ Phase L: Alerting + health — dispatched to codex.
- ⏸ Phase M: Code generation tool — next after L.
- ⏸ Phase N: End-to-end pipeline orchestration.
- ⏸ Phase O: Diff viewer + approval UX.

Total tests: 65 (all green).

## Execution notes

- **Workflow:** Claude drafts specs in `docs/ai/tasks/` and reviews diffs. Codex executes non-trivial phases. MiniMax handles trivial renames / string tweaks. See `AGENTS.md → Execution Workflow`.
- **Do not** start Phase B before Phase A is closed — governance enforcement has to be real before Jira writeback and sandbox execution go live, otherwise the first demo can bypass the audit surface we're building.
- **Do not** introduce a queue, worker pool, or new process model. Single-runtime orchestrator stays until Phase H is green, per `DECISIONS.md` and `CLAUDE.md` constraints.
- Every phase's spec must end with a codex / MiniMax "Workflow (for the executor)" section and list concrete files to edit.
- **Effort tiering:** Use xhigh for new services (E, F, G), medium for wiring/integration (I, parts of K), low for config/docs. See `feedback_effort_tiering.md`.
- **Observability phases (I–L)** are additive — they don't change existing behavior, only add parallel output channels. Safe to land incrementally.
