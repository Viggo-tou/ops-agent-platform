# T-MERGE-CC-AGENTIC-INTO-MAIN — Integrate three feature branches back into checkpoint/main

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: medium -->
<!-- Executor: claude (this is a merge / integration task, not a code-gen task — Claude reviews + merges, no codex needed) -->

**Status:** todo (P0 — Phase 3.0 results live in worktree branches; not on main)
**Priority:** P0 (every passing day = higher chance of conflict drift; QA baseline 49.65 reference is on `feat/kb-cc-agentic` not on main, so future tickets cannot easily inherit it)
**Created:** 2026-04-28

## Goal

Integrate three feature branches landed on 2026-04-28 (and one earlier) back into a single line of work on `checkpoint/pre-reclassify`, so:

1. The QA accuracy baseline (`docs/ai/benchmarks/qa-baseline-2026-04-28.md` mean=49.65) lives on `checkpoint/pre-reclassify` and acts as the project's reference
2. Future tickets can branch from `checkpoint/pre-reclassify` and inherit the working CC agent + benchmark + diagnosis stack
3. `feat/kb-cc-agentic`, `feat/failure-diagnosis`, `feat/qa-benchmark-integration` (and the older `feat/qa-accuracy-benchmark`) can be deleted as work products

## Background

After Phase AF closed (mean 27.06 → 49.65), the working code lives across:

| Branch | Where | Critical content |
|---|---|---|
| `feat/kb-cc-agentic` | worktree `D:/项目/ops-worktrees/cc-agentic` | CC agentic implementation + qa-benchmark merged in + new baseline 49.65 + runner --question-timeout flag |
| `feat/failure-diagnosis` | worktree `D:/项目/ops-worktrees/failure-diagnosis` | T-FAILURE-DIAGNOSIS LLM post-mortem step (Phase 6 +1) |
| `feat/qa-benchmark-integration` | worktree `D:/项目/ops-worktrees/qa-bench-integration` | already cherry-picked into `feat/kb-cc-agentic` via merge `2576033` |
| `feat/qa-accuracy-benchmark` | older worktree, not used directly | original qa-bench source (cherry-picked into integration branch already) |
| `docs/ops-strategic-specs-2026-04-28` | main working tree | all today's STAGE_LOG, roadmap, spec, phase-summary docs |

Right now `checkpoint/pre-reclassify` HEAD does NOT have any of these. Anyone branching from `checkpoint` to start a new ticket will not see the CC code, will not see the new baseline, and will likely re-discover the same problems.

## Design

### A. Order of merges (matters)

1. **First**: `docs/ops-strategic-specs-2026-04-28` → `checkpoint/pre-reclassify`. Lowest risk (docs only, +500 lines roadmap / spec / STAGE_LOG / phase summary). Establishes baseline references in checkpoint history.
2. **Second**: `feat/qa-benchmark-integration` → `checkpoint/pre-reclassify`. (If already implicit via cc-agentic merge, skip; else cherry-pick the 8 benchmark commits.)
3. **Third**: `feat/failure-diagnosis` → `checkpoint/pre-reclassify`. Independent module, low conflict risk.
4. **Fourth**: `feat/kb-cc-agentic` → `checkpoint/pre-reclassify`. Largest change (CC agent + knowledge.py rewrite + baseline). After this, `checkpoint` has the full Phase AF state.

### B. Conflict expectations

- **Docs branch ↔ checkpoint**: should clean — docs are append-only.
- **failure-diagnosis ↔ checkpoint**: orchestrator/service.py +20 lines; check for conflict with existing `_mark_awaiting_approval` / `_mark_task_failed` (those are from T-PIPELINE-REPAIR-CAP-IMPL already on checkpoint).
- **failure-diagnosis ↔ cc-agentic**: both add `apps/web/src/components/chat/AwaitingApprovalBlock.tsx` (one from T-CHAT-APPROVAL-UX merged in cc-agentic, one new from diagnosis spec). **Real conflict expected here**. Decide which version wins, possibly merge them.
- **cc-agentic ↔ checkpoint**: large diff in `apps/backend/app/services/knowledge.py` (+235 lines for CC retrieval); conflict only if checkpoint touched this file recently (it didn't per current git log).

### C. Verification after each merge

- `python -m compileall apps/backend/app` clean
- run focused tests for the merged feature (e.g. `test_failure_diagnosis.py` or `test_cc_agent.py`)
- spot-check `git log --first-parent` shows the merge commit clearly attributed

### D. What NOT to merge

- `worktree-agent-a3648b5f` / `a5eec7ab` / `ac2e0e58` — these are temp Claude worktree branches with 0 unique commits. Safe to delete after main work merged.
- 10 already-zero-ahead worktrees (per Stage 1 audit). Already trackable for cleanup; not blockers for THIS ticket.

## Files / actions

This is **not a code-gen task**. Claude (or whoever picks this up) does:

1. Verify clean working tree on each source worktree (`git status --short`)
2. Switch to `checkpoint/pre-reclassify` in main worktree (or create a new integration worktree)
3. Merge in order A above; resolve conflicts (mostly docs adjacency); verify tests
4. Delete merged feature branches: `git branch -d feat/kb-cc-agentic feat/failure-diagnosis feat/qa-benchmark-integration` etc.
5. Remove their worktrees: `git worktree remove ...`

## Acceptance criteria

- `checkpoint/pre-reclassify` HEAD contains:
  - CC agent implementation (cc_agent.py + cc_agent_loop.py + tests)
  - qa benchmark dataset + runner + baseline 49.65 report
  - failure_diagnosis service + tests
  - All today's STAGE_LOG / phase-summary / roadmap / spec changes
- `pytest apps/backend/tests/services/test_cc_agent.py` and `test_failure_diagnosis.py` pass on `checkpoint/pre-reclassify` HEAD
- `cat docs/ai/benchmarks/qa-baseline-2026-04-28.md` is on checkpoint
- `feat/kb-cc-agentic`, `feat/failure-diagnosis`, `feat/qa-benchmark-integration`, `feat/qa-accuracy-benchmark` branches and worktrees are deleted (or recorded as archived)
- No spurious unstaged changes in `checkpoint/pre-reclassify` after the merges

## Out of scope (explicitly NOT in this card)

- Merging `checkpoint/pre-reclassify` to `main` — separate decision; the checkpoint branch is the project's "latest stable integration point"; main can lag
- Cleaning up the 10 already-merged stale worktrees (see Stage 1 audit) — separate hygiene ticket
- Re-running the QA benchmark on the merged checkpoint to confirm 49.65 reproduces — recommended but not blocking; the artifact JSONL is already committed
- Tag creation (`session-end` / `phase-AF-merge`) — optional polish

## Risks

| Risk | Mitigation |
|---|---|
| `AwaitingApprovalBlock.tsx` conflict between failure-diagnosis and chat-approval-ux | Manual merge; the diagnosis-rendering version is more functional, the chat-approval-ux version is more compact — pick chat-approval-ux as base, layer the diagnosis rendering on top |
| `orchestrator/service.py` huge file conflicts | Re-baseline cc-agentic on checkpoint (rebase, not merge) if straight merge produces too many conflicts |
| Lost commits during conflict resolution | Tag everything before starting: `git tag pre-merge/cc-agentic-2026-04-28-<HHMM>` etc. |

## Workflow

This is a Claude-driven task (no codex dispatch). Steps:

1. Tag before starting:
```
git tag pre-merge/T-MERGE-CC-AGENTIC-2026-04-28
git push origin pre-merge/T-MERGE-CC-AGENTIC-2026-04-28  # optional, but a safety net
```

2. Switch to checkpoint and merge in order:
```
git checkout checkpoint/pre-reclassify
git merge --no-ff docs/ops-strategic-specs-2026-04-28
git merge --no-ff feat/failure-diagnosis    # resolve AwaitingApprovalBlock conflict if any
git merge --no-ff feat/kb-cc-agentic        # already includes qa-benchmark-integration via 2576033
```

3. Verify:
```
python -m compileall apps/backend/app
pytest apps/backend/tests/services/test_cc_agent.py apps/backend/tests/services/test_failure_diagnosis.py
ls docs/ai/benchmarks/qa-baseline-2026-04-28.md docs/ai/STAGE_LOG.md
```

4. Cleanup:
```
git branch -d feat/kb-cc-agentic feat/failure-diagnosis feat/qa-benchmark-integration feat/qa-accuracy-benchmark docs/ops-strategic-specs-2026-04-28
git worktree remove D:/项目/ops-worktrees/cc-agentic
git worktree remove D:/项目/ops-worktrees/failure-diagnosis
git worktree remove D:/项目/ops-worktrees/qa-bench-integration
git worktree remove D:/项目/ops-worktrees/qa-benchmark
```

5. Record in STAGE_LOG: open + close a stage entry for this merge.

**Get user approval before each merge step** — these touch shared branches.
