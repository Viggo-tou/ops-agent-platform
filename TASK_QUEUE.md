# Task Queue

Last updated: 2026-04-14 (body) — **but see SESSION_HANDOFF.md 2026-05-12 section for the active v16 roadmap**. This file's body below is from the T-026/T-029-era backlog and does not reflect the v15 governance closure or v16 chat UX / repair-intent work.

> **🚨 New-agent pointer**: the live in-flight backlog is in **SESSION_HANDOFF.md → "Open / pending work — roadmap"**. The named items (v16.0 / v16.1 / v16.2 / v16.3 / v16.4 + the v16 P0 backlog T-DEPENDENCY-FINGERPRINT / T-IMPORT-DEPENDENCY-GATE / T-COMPILE-ERROR-MEMORY) supersede everything below for new work. The 2026-04-14 items below are kept for historical traceability — many are done, some folded into v15/v16, none should be picked up without first reading the handoff.

Status values: `todo`, `doing`, `blocked`, `done`.

## P0

### T-LEARNING-LOOP-V3 Cold-start playbook promotion (opened 2026-05-15)

Status: proposed

Spec: `docs/ai/tasks/T-LEARNING-LOOP-V3-COLD-START-PLAYBOOK-PROMOTION.md`

Goal:

- Unknown or first-time task types must run through a cold-start workflow, not
  a broad deterministic recipe.
- Successful/failed runs may synthesize draft playbooks, but drafts are
  advisory only until promoted by repeated verified approvals.
- Promoted playbooks carry source/dependency fingerprints and demote
  automatically on drift, repeated failures, or neighboring-subtype collision.

Why now:

- P69-19 became stable after domain recipe + gates.
- P69-17 proved that stability alone is not enough: a broad map/location
  playbook can be semantically wrong for a nearby job-default-address subtype.
- The loop needs a principled path from "no history" to "trusted playbook"
  without turning one run into a permanent template.

Acceptance summary:

- No exact promoted subtype -> cold-start mode.
- Draft playbook -> prompt warning/checklist only, no deterministic fast path.
- Promotion requires repeated normal approvals and clean deterministic gates.
- Fingerprint or failure drift -> automatic demotion.

---

### T-LEARNING-LOOP-V1 Failure-observation memory closes the learning loop (opened 2026-05-12)

Status: in progress (Phase 1 — schema + classifier + first 3 hook scopes)

Diagnosis (evidence from this session, see Stage 28 + recent runs):

- 49 `agent_memory` rows total; last write 2026-05-10; **0 rows from rounds 6/7/7b/8 (May 11-12)**.
- Write discipline `recompile + symbol_graph + quote_verified` is correct for success memory but **starves on failure memory**: terminal-fail runs never satisfy the gate.
- All 49 rows are `gate_failure_resolution` scoped to old gates (`gate:semantic_review:*`, `gate:failure_diagnosis`); **0 rows for `gate:contract_coverage`, `gate:compile_repair`, or `gate:must_touch`** — the exact failure classes hitting recent runs.
- 25,965 events in DB; **0 events with tool_name containing 'history' / 'previous' / 'past_plan' / 'few_shot'** — no path retrieves prior runs as planner/codegen context.
- Net: planner and codegen start from scratch on every task even when an identical task_family failed an identical gate one day prior.

Goal:

Create a failure-observation memory loop that records **verified failure facts**, not speculative fixes, separated from the existing success-memory write discipline.

Phase 1 (this iteration):

1. Extend `agent_memory` schema with `memory_kind` (`success_fact` | `failure_observation` | `verifier_diagnostic` | `provider_liveness`), `failure_class`, `task_family`, `trust_level` (`verified` | `human_confirmed` | `auto_classified`), `prompt_eligible` (JSON whitelist), `evidence_refs` (JSON).
2. Add `apps/backend/app/services/failure_classifier.py` with rule-based classifiers for:
   - `must_touch_incomplete_diff` (gate:must_touch)
   - `contract_coverage_unverified | contradicted | lie` (gate:contract_coverage)
   - `compile_repair_timeout | liveness_gap` (gate:compile_repair)
   - `provider_partial_diff | provider_empty_response` (codegen provider)
3. Hook `pipeline.failed` and explicit `manual_abort_with_reason` in `orchestrator/service.py` to call classifier → `memory_service.write_failure_observation(...)`.
4. Backfill round 8 (task `6803021c`) as the first `must_touch_incomplete_diff` row to validate the write path.
5. Tests:
   - `test_must_touch_failure_writes_observation_row`
   - `test_contract_coverage_lie_does_not_write_to_success_pool`
   - `test_planner_retrieves_failure_observation_for_similar_task`
   - `test_prompt_injection_excludes_speculative_fixes`

Phase 2 (next iteration):

- Planner retrieval injection: query failure_observation by `task_family` + `scope`, top-3 with `prompt_eligible` containing `planner_warning`, render as prior-failure warning section before plan generation.

Phase 3 (later):

- Codegen retrieval (gate/family-specific, 1 row max, `prompt_eligible: codegen_warning`).

Phase 4 (later):

- Historical few-shot from past successful plans only (`memory_kind=success_fact`, verdict=approval_requested).

Phase 5 (concurrent):

- Metrics dashboard. Core indicator: `same_failure_recurrence_rate` (% of runs whose `failure_class` matches a row already in failure_observation for the same `task_family`).

Hard rules:

- success memory write discipline (recompile + symbol_graph + quote_verified) **unchanged**.
- failure_observation rows record observed facts only — never "the correct fix is X".
- `prompt_eligible` is a whitelist; planner/codegen consume only the labels they're authorized for.
- New rows tagged `trust_level=auto_classified` unless human confirms; planner retrieval may surface but must label as unconfirmed.

Acceptance:

- Round 8 backfill row reads correctly through `memory_service.query(kind='failure_observation', scope='gate:must_touch', task_family='android_map_location')`.
- New `failure_observation` rows accumulate on every subsequent terminal pipeline failure without further code changes.
- Existing success-memory query path returns unchanged result set (`kind='success_fact'` filter applied transparently).

---

### T-C7-COMPILE-REPAIR-STALL Investigate compile_repair stall behavior (opened 2026-05-12)

Status: open

Trigger: surfaced during v16.2.1 live verification (Stage 27, task `cf2b0a81-b855-4ece-91a9-3a6def8e1b1e`, round 7b). Target gate (`contract_coverage`) passed cleanly with 6/6 contracts verified, then pipeline entered `compile_repair.round_started` and produced no further progress events for ~8 min before manual abort.

Scope (investigate, do NOT modify v16.2.1 verifier):

- Does the generated Kotlin actually fail compile, or is it the repair loop itself stuck before invoking Gradle?
- Is the repair loop lacking progress events (per-batch deadline / cancel telemetry missing) so we cannot tell stuck-vs-running from outside?
- Is the per-batch deadline (720s) correctly counted from `round_started`, or does it inherit from earlier stages?
- Check whether v16.2.1 verifier changes accidentally corrupted the repair prompt (compare repair prompt before/after the contract_coverage refactor).

Evidence pack: [`docs/incidents/2026-05-12-v16.2.1-live-verification/`](docs/incidents/2026-05-12-v16.2.1-live-verification/) — has audit.jsonl, plan, codegen diff, and the contract_coverage verdict. The codegen diff in there (112 lines) is what the compile gate received.

Hard rule: this ticket does NOT block committing v16.2.1. v16.2.1 already shipped at `72242f4`. C7 is a separate failure class on a different gate.

---

### T-034 Jira Develop Pipeline Stability Fixes

Status: done (code), docs-only (no git commit — see T-037)

Scope: six pipeline fixes for the `jira_issue_develop` flow, verified end-to-end on Jira project `TEST` (TEST-1 transitioned To Do → Done).

Fixes:

- Fix #1: MiniMax planner empty-steps fallback — add `jira_issue_develop` guidance + post-sanitize default refill in `apps/backend/app/agents/service.py`.
- Fix #3: `codegen.files_changed` aggregated into task `result` (was already correct; false alarm).
- Fix #4: Pure new-file tasks skip grounding-file batches; `_new_file_task` pipeline flag with three detection paths (plan/disk/request-text filename extraction) in `apps/backend/app/orchestrator/service.py`.
- Fix #5: Removed auto Jira comment after task completion (transition to Done still kept); cleaned up "Jira: commented and transitioned" message wording to "Jira: transitioned".
- Fix #6: Planner no longer copies knowledge-retrieval citations into `affected_code_locations` for develop scenarios — citations are grounding, not edit targets. Prompt in `_build_planning_instructions` rewritten.
- Test sync: `test_develop_pipeline.py` updated (12/12 green), `test_jira_writeback_scenario.py` `requires_approval` assertion aligned with current auto-approve policy.

Verification:

- TEST-1 end-to-end: `status=completed`, `affected_code_locations=[]`, `_new_file_task=true`, single codegen batch, `files_changed=["config/retry.json"]`, Jira transitioned to Done, no auto-comment.
- Develop pipeline tests: 12/12 pass.

Files touched this session:

- `apps/backend/app/agents/service.py`
- `apps/backend/app/orchestrator/service.py`
- `apps/backend/tests/orchestrator/test_develop_pipeline.py`
- `apps/backend/tests/orchestrator/test_jira_writeback_scenario.py`

### T-035 Residual Test Debt (auto-approve + provider resolver)

Status: done (2026-04-14)

Pre-existing failures unrelated to T-034, fixed by aligning assertions with current policy:

- `tests/services/test_codegen.py` — 4 failures fixed. `_resolve_provider()` renamed to `_resolve_provider_chain()` (returns a list); `codegen.generate_patch` permission now WRITE (auto-approve policy), not APPROVAL_REQUIRED.
- `tests/tools/test_tool_approval_gate.py` — 3 failures fixed. Swapped sample tool `sandbox.run_command` (now WRITE) → `internal_api.request` (still APPROVAL_REQUIRED under current registry).

Verification: `pytest tests/` = 124 passed, 0 failed.

Files touched this session:

- `apps/backend/tests/services/test_codegen.py`
- `apps/backend/tests/tools/test_tool_approval_gate.py`

### T-036 Completeness Check Third Safety-Net Path (Fix #2)

Status: todo (demoted)

After Fix #4 the original symptom (grounding-file pollution in completeness check) is no longer the main failure mode. This safety-net path remains a hedge. Low priority.

### T-038 Spec Conformance Gate (honesty harness, P0)

Status: in progress (P0 partial done 2026-04-14; A/B deferred)

Motivation: P69-10 surfaced the "shadow implementation" failure mode —
diff_reviewer and Jira transition both passed while the patch created a
parallel clean architecture and left the dirty existing code completely
untouched. Root cause: no gate binds the diff shape to the request's
semantic intent. The fix is general-purpose (applies to every modify
task, not just P69-10): enforce three invariants between apply_patch and
jira.transition_issue.

Sub-tasks:

| ID | Scope | Owner | Status |
|---|---|---|---|
| T-038-S1 | spec_conformance service: shadow_implementation, hit_delta, must_touch | Claude | done → `apps/backend/app/services/spec_conformance.py` + 13 tests |
| T-038-S2 | Wire hard gate into orchestrator (after diff_reviewer, before jira.transition) | Claude | done → `apps/backend/app/orchestrator/service.py` |
| T-038-A | Retry-with-feedback loop (max 1 retry, feed block messages into codegen prompt) | Claude | done → `MAX_CONFORMANCE_ATTEMPTS=2`, `_reset_for_conformance_retry`, recursive re-entry, `_build_codegen_task_description` injects directives |
| T-038-B | Evidence bundle schema in planner output (must_touch_files + justification) | Claude | done → `GeneratedPlanPayload.must_touch_files` field + planner instruction text + rule-based fallback in develop scenario + `planner_must_touch` 4th gate rule |
| T-038-C | Replay harness: record P69-8/P69-14/P69-10 as fixtures for regression scoring | Claude | done → `scripts/replay_conformance.py` + `docs/ai/fixtures/conformance/*.json` (3/3 fixtures match expected verdicts) |
| T-038-D | Goal-evidence attestation: per-anchor proof that each destructive sub-goal landed | Claude | done → `build_goal_attestation` in `spec_conformance.py`, emitted as `spec_conformance.attest` event + included in task result JSON + 3 unit tests |

Verification (2026-04-14):

- `pytest tests/` → 167 passed, 0 failed (was 145 before; +13 conformance + +3 attestation + +1 retry-loop + +5 planner-fallback tests).
- Retry path proven by `test_conformance_retry.py`: first codegen returns a shadow diff → gate blocks → sandbox reset → second codegen receives `RETRY FEEDBACK` directive → modifying diff → gate passes → Jira transitions; `conformance_attempts=1`, attestation shows `Minij` status=achieved with count 2→0.
- Planner fallback proven by `test_plan_must_touch_fallback.py` (5 cases covering destructive+citations, non-destructive, empty knowledge, non-develop scenario, 6-entry cap).
- On the exact P69-10 shape (all-new-files patch with request mentioning
  `'Minij'`), the gate now blocks with `shadow_implementation`,
  `hit_delta`, `must_touch`, and `planner_must_touch` findings (asserted
  in both unit tests and the replay harness).
- Replay harness: `python scripts/replay_conformance.py` → 3/3 fixtures
  match expected verdicts (P69-8 pass, P69-14 pass, P69-10 block-with-4-rules).
- Goal attestation: `build_goal_attestation` emits per-anchor before/after
  counts and the files that were modified; event `spec_conformance.attest`
  carries the attestation, and the final `task.result.goal_attestation`
  records it for UI/audit consumption.

Files touched this session:

- `apps/backend/app/services/spec_conformance.py` (3 rules → 4 rules + `build_goal_attestation`)
- `apps/backend/tests/services/test_spec_conformance.py` (+3 attestation tests, 16 total)
- `apps/backend/tests/orchestrator/test_conformance_retry.py` **NEW** (retry loop end-to-end)
- `apps/backend/tests/agents/test_plan_must_touch_fallback.py` **NEW** (5 cases)
- `apps/backend/app/agents/schemas.py` (`must_touch_files` field on `GeneratedPlanPayload`)
- `apps/backend/app/agents/service.py` (planner instructions + rule-based `must_touch_paths` fallback wired into develop payload)
- `apps/backend/app/orchestrator/service.py` (retry loop, sandbox reset, codegen directives, attestation event + result embed)
- `scripts/replay_conformance.py` **NEW**
- `docs/ai/fixtures/conformance/{README.md,p69-8_pass.json,p69-14_pass.json,p69-10_block.json}` **NEW**
- `TASK_QUEUE.md`

### T-039 Jira Transition Approval Gate

Status: done (2026-04-15)

Scope: insert a human-approval gate between the spec-conformance `attest` pass and `jira.transition_issue`. Pre-T-039, develop tasks auto-transitioned Jira the moment the gate passed — forcing the user to keep flipping Jira back to "To Do" during test runs. Post-T-039, the task parks in `AWAITING_APPROVAL` with the diff + summary + goal attestation surfaced; only a grant proceeds to Jira, and reject keeps the code while leaving Jira untouched.

Sub-tasks:

| ID | Description | Owner | Notes |
|---|---|---|---|
| T-039-A | Gate in orchestrator before jira writeback (setting-gated) | Claude | done → `app/core/config.py:develop_require_jira_approval`, `app/orchestrator/service.py` gate block + `_request_jira_transition_approval` |
| T-039-B | `resume_after_approval` branches on develop scenario | Claude | done → sets `pipeline_state.jira_approval_granted`, re-enters `_execute_develop_pipeline` (cached stages short-circuit) |
| T-039-C | Reject special-cases `jira.transition_issue` → task COMPLETED (not FAILED) | Claude | done → `app/services/approvals.py::reject` preserves diff + annotates `jira_transition_rejected=true` |
| T-039-D | Unit tests (gate, grant routing, reject-on-develop vs reject-other) | Claude | done → `tests/orchestrator/test_jira_approval_gate.py` (4 tests) |
| T-039-E | Curl-driven E2E driver + recorded evidence | Claude | done → `scripts/e2e_develop_approval.py`; reject path passed end-to-end, evidence in `docs/ai/evidence/T-039/reject-path-2026-04-15.txt` |

Persistence bug caught during this work: `ConformanceReport` dataclass was landing in `task.latest_result_json` via two paths (not one as first diagnosed), triggering SA `json_serializer` failures and the frontend "Failed to fetch" / PendingRollbackError. Fix: store `conformance_report.to_payload()` (dict) at the event-record site so `pipeline_state` never holds the dataclass. Lesson: any invariant "everything in pipeline_state must be JSON-safe" deserves an `assert_json_safe` guard; tests that mock `self.db` will not catch this.

Verification:

- `pytest tests/` → **171 passed, 0 failed** (167 pre-T-039 + 4 gate/grant/reject tests).
- E2E reject path: task posted to /api/tasks → parked at AWAITING_APPROVAL → Approval row has `action_name=jira.transition_issue`, `approver_role=team_lead` → `/reject` → task COMPLETED, `jira_transitioned=false`, `jira_transition_rejected=true`, 5 files preserved in `files_changed`. No Jira API call made.
- Grant path not exercised E2E (needs a real Jira project wired up); unit-covered by `test_resume_after_approval_flips_granted_and_reenters_pipeline`.

Files touched this session:

- `apps/backend/app/core/config.py` (new `develop_require_jira_approval` setting)
- `apps/backend/app/orchestrator/service.py` (approval gate + `_request_jira_transition_approval` + develop-aware `resume_after_approval` + persistence fix)
- `apps/backend/app/services/approvals.py` (reject special-case)
- `apps/backend/tests/orchestrator/test_jira_approval_gate.py` **NEW**
- `apps/backend/tests/orchestrator/test_conformance_retry.py` (opt out of gate for that test's scope)
- `scripts/e2e_develop_approval.py` **NEW**
- `docs/ai/evidence/T-039/reject-path-2026-04-15.txt` **NEW**
- `docs/ai/mcp/playwright-setup.md` **NEW** (unblocks future browser-mode self-testing)
- `SESSION_HANDOFF.md`, `TASK_QUEUE.md`

### T-037 Session Boundary Discipline and Commit Hygiene

Status: done (2026-04-14)

Outcome:

- Policy: `AGENTS.md` + `CLAUDE.md` now mandate session-start tag + commit-or-manifest-at-close.
- Backlog cleared: 153-file catch-up commit `c6c3101` consolidates every prior session's working tree into a real baseline.
- Baseline tag `session-baseline/2026-04-14-T037` points at `c6c3101` so future sessions have an unambiguous diff origin.
- `.gitignore` expanded for runtime artifacts (`*.db-shm/wal`, `data/sandboxes/`, `manual-*/`, `.claude/`, one-off MiniMax output).
- Next session can genuinely answer "what did I change?" via `git diff session-baseline/2026-04-14-T037..HEAD`.

Root cause: only one commit (`940b232 chore: initial commit`) exists. Every session since T-024 has left its work in the working tree; there is no git-level way to tell which lines belong to which session. This forces every "commit only this session" request to fail.

Acceptance:

- `AGENTS.md` records a mandatory session-start and session-end commit ritual.
- Each session ends with either a real commit OR a `SESSION_HANDOFF.md` manifest listing the exact files + line-stat touched this session.
- A `session-start/<timestamp>` git tag is created at the beginning of each working session so the diff baseline is unambiguous.
- Policy is written so that a future session cannot produce the "can't separate my work" situation.



### T-027 Resumable Development State Files

Status: done

Acceptance:

- Repo has a permanent `AGENTS.md` recovery guide.
- Repo has mutable current-state files: `CURRENT_STATE.md`, `TASK_QUEUE.md`, `DECISIONS.md`, `SESSION_HANDOFF.md`.
- The recovery prompt is recorded.
- Current blocker, reference UI findings, runtime evidence, and next actions are recoverable from files.

### T-028 Fix Chat Knowledge Answer Chain

Status: done

Acceptance:

- A repository question such as `Locate Firebase configuration file(s) in the codebase` returns a natural-language answer or a natural no-evidence explanation, not the planner step list.
- Backend `process_question` either produces a valid `knowledge_answer` with citations or stores a user-facing failure response.
- Frontend chat does not show `Status failed`, `Request type`, or `Review state` as normal product content.
- Failed reviewer output is phrased as a calm assistant response.
- Regression smoke test covers at least one repository-grounded query.

### T-029 Strict Reference UI Pass

Status: done

Acceptance:

- Chat, knowledge, memory, settings, and home/entry surfaces follow the screenshots in `references/`.
- Sidebar, main content width, composer, cards, provider chips, upload zone, memory stats, and empty states match the reference hierarchy.
- UI remains white/black/gray, uncluttered, and free of dashboard/debug terminology.
- Chat answer area prioritizes readable Chinese or English natural-language output.

### T-032 Same-Conversation Follow-up Turns

Status: done

Acceptance:

- Sending a message while viewing an existing chat reuses the current `session_id`.
- Follow-up questions include the previous user request and assistant answer as backend context.
- The chat surface displays only the user's follow-up text, not the hidden context payload.
- The sidebar groups tasks by `session_id` so follow-up turns stay under one conversation.
- Backend classification uses the real follow-up request instead of the whole context block.

### T-033 Environment Handoff Documentation

Status: done

Acceptance:

- `PROJECT_CONTEXT.md` exists as a long-lived project context entry.
- Recovery bootstrap lists include `PROJECT_CONTEXT.md`.
- Handoff files avoid non-ASCII absolute paths that render poorly in PowerShell.
- Current progress, next task, verification evidence, Firebase findings, and known gaps are documented.

## P1

### T-026 Workbench Backend Persistence and Governance Integration

Status: in progress (audit + decisions + tests done; zip + E2E pending)

Audit (2026-04-14) found that most scope was already implemented. Refined sub-tasks with explicit ownership:

| ID | Scope | Owner | Status |
|---|---|---|---|
| T-026-S1 | Current-state audit | Claude | done |
| T-026-B | Delete semantics decision | Claude | done → see DECISIONS.md D-008 (keep hard delete) |
| T-026-C | Model-config key-leak defensive test | Claude | done → `tests/api/test_model_config_no_secret_leak.py` |
| T-026-D | Frontend↔backend PERMISSION_MAP parity test | Claude | done → `tests/core/test_permission_map_frontend_parity.py` |
| T-026-M1 | RBAC expected-response matrix fixture | MiniMax | done → `apps/backend/tests/fixtures/rbac_expected_matrix.json` (22 endpoints) |
| T-026-M2 | API schema docstrings | MiniMax | done → `Field(description=...)` added in memory.py, model_config.py, knowledge.py |
| T-026-M3 | ADR 0001 zip import security policy | MiniMax | done → `docs/adr/0001-zip-import-security.md` (9 MUST controls) |
| T-026-M4 | HTTPException detail text normalization | MiniMax | done → 17 sites normalized across `apps/backend/app/api/` |
| T-026-A | Zip archive import endpoint | Claude (codex unavailable) | **done** → `apps/backend/app/services/knowledge_zip.py` + `/api/knowledge/upload-zip` route + 16 tests. All 9 ADR 0001 controls covered. 145 passed overall. |
| T-026-M5 | Zip test fixture builder | MiniMax | spec ready → `docs/ai/tasks/T-026-M5-zip-test-fixtures.md`; unblocks T-026-A tests |
| T-026-E | 4-role E2E RBAC smoke (`scripts/verify-rbac.ps1`) | Claude | done → 88/88 role×endpoint cells pass against live backend |

Acceptance (original, unchanged):

- Knowledge import for files/zip has backend-owned endpoints.
- Knowledge source delete/disable is enforced server-side.
- Memory entries and memory settings persist through backend APIs.
- Model/provider settings load from backend-controlled APIs.
- Sensitive actions are checked by frontend and backend RBAC/governance paths.

### T-040 Validator & Harness Hardening: no-found > fabrication

Status: done (2026-04-16, codegen deferred to codex)

Scope: harden the 5 defense lines against LLM fabrication. Three MiniMax codegen failures proved the validator correctly blocks hallucinated patches.

Bug fixes:
- Unified diff format parsing (Strategy 2 for standard `--- a/` / `+++ b/` without git headers)
- hit_delta aggregate logic (at least one anchor decreased → rest are warn, not block)
- Alphanum identifier extraction (`master1`-type: `_IDENT_ALPHANUM_RE`)
- Unified diff regex for `/dev/null` paths (create/delete detection)

New defense lines:
- 防线2: `_anchor_precheck_fails()` in orchestrator — all anchors missing from knowledge source → fail fast before codegen
- 防线5: `compile_gate.py` — `node --check` (JS/JSX) + `py_compile` (Python) syntax validation after apply_patch

New codegen hardening:
- System prompt rules 7-9 (file creation constraints, target file enforcement, `targets_not_in_context` error)
- Constraint injection from translation → codegen (`_build_prompt`)

Tests: 226/226 passed (including 31 adversarial defense-line tests covering all 5 gates + diff parser edge cases).

Evidence: `docs/ai/evidence/T-040/` — 3 blocked hallucinated tasks + 1 correct-patch-conformance pass.

Files:
- `apps/backend/app/services/spec_conformance.py` (3 bug fixes + anchors_missing_from_tree rule)
- `apps/backend/app/services/compile_gate.py` **NEW**
- `apps/backend/app/services/codegen.py` (prompt hardening + constraint injection)
- `apps/backend/app/orchestrator/service.py` (防线2 + 防线5 wiring + translation→codegen constraints)
- `apps/backend/tests/services/test_spec_conformance.py` (18→30 tests)
- `apps/backend/tests/services/test_compile_gate.py` **NEW** (10 tests)
- `apps/backend/tests/services/test_defense_lines_adversarial.py` **NEW** (31 tests)

### T-041 Anti-Hallucination Defense Matrix (12-mechanism)

Status: in progress (2026-04-16)

Motivation: T-040 proved the existing 5 defense lines work, but gap analysis against a comprehensive 12-point anti-hallucination framework revealed 5 fully missing and 3 partially missing mechanisms. T-041 implements all 8 gaps to complete the defense matrix.

Pipeline evolution: `plan → evidence_build → target_validation → codegen → patch_guard → apply_patch → spec_conformance → runtime_test → review → approve → writeback`

Sub-tasks:

| ID | Mechanism | Priority | Status |
|---|---|---|---|
| T-041-01 | Evidence bundle — codegen 前定位证据包 (搜索命中 + 符号 + must-touch)，不通过不允许 codegen | P0 | todo |
| T-041-02 | Intent-vs-diff shape checker — diff 规模/新增比例/文件数 vs 任务类型不匹配则 block | P0 | todo |
| T-041-03 | Existing-file-first policy — 新增文件比例硬性门槛，超限 block 或强制 review | P0 | todo |
| T-041-04 | Approval 强制校验证据 — approve 前检查 attestation/must-touch/hit-delta 全闭合 | P0 | todo |
| T-041-05 | Symbol + reference gate — import/调用链分析，改定义必须改引用 | P1 | todo |
| T-041-06 | Failing test first — 行为 bug 类任务要求先生成失败测试 | P1 | todo |
| T-041-07 | Runtime path validation — browser smoke / integration test 验收行为 | P1 | todo |
| T-041-08 | Goal-by-goal conformance + per-file justification — 子目标逐项验证 + 每文件改动理由 | P1 | todo |

Acceptance:
- All 12 anti-hallucination mechanisms have code + tests
- Pipeline has evidence_build and target_validation stages before codegen
- Approval gate requires evidence chain closure, not just green status
- 行为 bug 类任务有 failing-test-first 门控
- diff shape 与任务意图匹配检查
- 全部 tests pass

## P2

### T-030 Approval APIs and Queue Polish

Status: todo

Acceptance:

- Approval queue UI has backend-backed list/detail actions.
- Policy decisions are visible in natural product language.
- Audit/tool logs remain available without dominating the main workbench.

### T-031 Async Execution Design Spike

Status: done (2026-04-14) → `docs/adr/0002-async-execution-trigger.md`

Outcome: ADR 0002 locks in four hard trigger conditions (latency p95 > 60 s over 7 d, governance stability, frontend async UX ready, multi-tenant demand) before we leave the single-runtime path. Migration target when triggers fire is Option B (in-process asyncio + persisted state machine + SSE stream), staged across 3 PRs behind `ASYNC_EXECUTION_ENABLED`. Option C (Celery/RQ) explicitly deferred to a future ADR.

Acceptance:

- Documents when and how to introduce queue-backed long-running workflows. ✅
- Keeps the current single-runtime path as the default until governance and product UI are stable. ✅ (encoded as trigger conditions 2 + 3)
