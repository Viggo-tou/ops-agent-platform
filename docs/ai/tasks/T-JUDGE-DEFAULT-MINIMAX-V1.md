# T-JUDGE-DEFAULT-MINIMAX-V1 — Stage 20A V1: pin official judge to MiniMax, remove `auto`

<!-- Effort: small -->
<!-- Executor: codex (or direct per user judgment, ~10-line change) -->

**Status:** todo (P1 — Stage 20A V1 final)
**Priority:** P1 (replaces T-JUDGE-HYBRID-V1 as Stage 20A V1; D-010 amendment)
**Created:** 2026-05-01
**Branch:** `fix/judge-v1-default-minimax` based on `checkpoint/pre-reclassify` AFTER hybrid + timeout merges (see Merge Sequence)
**Linked decision:** `DECISIONS.md` D-010 (amended after this lands)
**Linked spec:** `docs/ai/specs/stage20-judge-verdict.md`
**Supersedes:** `docs/ai/tasks/T-JUDGE-HYBRID-V1.md` (retired) and `T-JUDGE-HYBRID-V1-FIX.md` (retired)

## Background

Stage 20A originally planned hybrid judge (rule + cited-evidence + LLM rungs). Implementation landed and was smoked; manual inspection of the rule-vs-LLM disagreement cases (10 cases across both datasets) found:

- TP (rule legitimately catches MM false negative): 2
- FP (rule fires but answer doesn't cover keypoint): 3
- Ambiguous (partial coverage, judgment-dependent): 5

Net delta TP − FP = −1 with 5 ambiguous. Hybrid `ev=0` shows the smallest cross-stack gap on aggregate (+4.84 vs MM's +8.46 vs rule's +25.12), but the lift is partly composed of rule false positives. **Hybrid is not yet trustworthy as the primary judge for V1.**

The simpler, more defensible V1: **pin official benchmark default to MiniMax semantic judge.** Rule judge remains as a lexical-conformance diagnostic; hybrid stays in code as an experimental flag for V2 calibration; `auto` is removed entirely (it silently falls back to rule on infrastructure failure, producing artifacts that look like official runs but use a different judge).

## Goal

1. Change `run_qa_benchmark.py` argparse `--judge-mode` default from `auto` to `minimax`.
2. **Remove** `auto` from the argparse `choices` list. Auto's silent fallback is a benchmarking footgun.
3. Keep `hybrid` as an explicitly-named experimental mode in the choices list, with help-text marking it experimental.
4. Add three new fields to the bench summary:
   - `judge_family_count: int` — number of distinct LLM provider families that contributed judgments (V1 always 1 for MiniMax; V2 may be 2+)
   - `cross_family_validated: bool` — true iff `judge_family_count >= 2` AND each LLM judgment was confirmed by at least one other family. V1 always false.
   - `judge_caveats: list[str]` — human-readable caveats. For V1 mode=minimax, populates with `["single-LLM-family judge; cross-family validation pending T-JUDGE-HYBRID-V2"]`.
5. Mirror the same fields in `rejudge_run.py`.
6. Update `--judge-mode` help text to surface the V1 default and the experimental status of hybrid.
7. Document in `docs/ai/specs/stage20-judge-verdict.md` (add an addendum) that V1 ships as MM-only; the hybrid path is preserved as experimental for V2.

## Files to edit

1. `apps/backend/scripts/run_qa_benchmark.py`
   - argparse `--judge-mode` choices: drop `auto`. Add: `minimax`, `anthropic`, `claude_code`, `codex`, `rule`, `hybrid` (in stable order). Default `minimax`.
   - argparse help text: `"Default: minimax (semantic judge). Pin one explicitly for official benchmarks. 'hybrid' is experimental — see docs/ai/tasks/T-JUDGE-HYBRID-V2.md."`
   - `KeypointJudge` dispatch: drop the `auto` chain branch. `KeypointJudge(requested_mode='auto')` should now raise an explicit error with the message `"auto judge mode is no longer supported; pin --judge-mode explicitly"`. (Removes silent rule-fallback footgun.)
   - `build_summary()` (or wherever summary is constructed): add three new fields per Goal #4. Compute as:
     - `judge_family_count = 1 if requested_judge_mode in {minimax, anthropic, claude_code, codex} else 0` (rule is not an LLM family). For hybrid, count distinct LLM families used.
     - `cross_family_validated = False` (always for V1; reserved for V2)
     - `judge_caveats = []` plus the MM-only caveat string when in minimax mode.
2. `apps/backend/scripts/rejudge_run.py`
   - Mirror the argparse changes (drop auto, default minimax, hybrid experimental).
   - Mirror summary field additions.
3. `apps/backend/tests/scripts/test_run_qa_benchmark.py`
   - Update any test using `judge_mode='auto'` (if any) to `'minimax'` or another explicit mode.
   - Add 2 new tests:
     - `test_auto_mode_rejected` — calling `KeypointJudge('auto')` raises with the expected message.
     - `test_summary_includes_judge_family_metadata` — summary record contains `judge_family_count`, `cross_family_validated`, `judge_caveats` with V1 expected values.
4. `docs/ai/specs/stage20-judge-verdict.md`
   - Add an addendum section at the bottom titled `## V1 Landing (2026-05-01): MM-only after disagreement audit`. Briefly summarize: hybrid scaffolding committed (experimental flag) but V1 default is MM-only; reference the disagreement audit (this spec's Background); link to `T-JUDGE-HYBRID-V2.md` and `T-JUDGE-AMBIG-CALIBRATION.md`.

## Acceptance

- `python -m compileall scripts/run_qa_benchmark.py scripts/rejudge_run.py tests/scripts/test_run_qa_benchmark.py` clean
- All existing tests pass (35 baseline after T-JUDGE-HYBRID-V1-FIX) + 2 new tests pass
- `python -m scripts.run_qa_benchmark --help` shows `minimax` as default and lists `auto` as REMOVED (no choice for it)
- `python -m scripts.run_qa_benchmark --judge-mode auto ...` exits non-zero with a clear message
- Smoke run: `python -m scripts.run_qa_benchmark --judge-mode rule --limit 2` still works (legacy lexical mode preserved)
- Summary in any new artifact contains the three new metadata fields

## Sanity invariants

1. Removing `auto` does not break any current pinned bench workflow (we always pin in the runbooks).
2. `judge_caveats` for an MM-only artifact is exactly `["single-LLM-family judge; cross-family validation pending T-JUDGE-HYBRID-V2"]`.
3. For `rule` mode, `judge_family_count = 0` and caveat reflects "lexical-only judge; not suitable for cross-stack comparison".
4. For `hybrid` mode, `judge_family_count = 1` (MiniMax is the single LLM rung in V1's hybrid impl) and caveat reflects "experimental hybrid judge; not the official V1 default".

## Out of scope

- Adding a second LLM family — separate ticket `T-JUDGE-HYBRID-V2.md`.
- Re-rejudging existing artifacts with the new metadata schema — old artifacts predate the schema and are kept as-is. Only new runs get the fields.
- Tuning MM prompt or judge_samples — separate ticket if Stage 20 verdict needs strengthening.
- Changing rule judge's 75% token-overlap threshold — separate ticket `T-JUDGE-RULE-THRESHOLD-AUDIT` if/when calibration data shows it's needed.

## Workflow

This is a ~30-line change with 2 new tests. Two execution paths:

**Direct-apply (preferred per session precedent for small config-style changes)**: user authorizes; I patch in main worktree on a new branch `fix/judge-v1-default-minimax`, commit isolated, leave for review/merge.

**Codex dispatch**:
```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/judge-v1-default-minimax" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-JUDGE-DEFAULT-MINIMAX-V1.md
```
Worktree NEW from `checkpoint/pre-reclassify` HEAD AFTER the hybrid + timeout merges land.

## Merge sequence (the actual landing path)

1. Merge `feat/judge-hybrid-v1` into `checkpoint/pre-reclassify` — hybrid scaffolding becomes available as experimental flag (NOT default).
2. Merge `fix/bench-question-timeout` (commit `c1001bb`) into `checkpoint/pre-reclassify`.
3. Apply this spec on a new branch `fix/judge-v1-default-minimax` off the post-merge checkpoint.
4. Merge that — Stage 20A V1 officially lands.
5. Update `DECISIONS.md` D-010 with the V1 amendment (separate doc commit).
6. Update `SESSION_HANDOFF.md` with the merge state.

Steps 1-2 require user explicit-merge authorization (per memory rule: never merge without user permission). Step 3-4 once user authorizes V1 spec.

## Why retire hybrid as V1 default

See disagreement audit in Background. The 10-case manual inspection produced TP=2, FP=3, ambiguous=5. Net signal `TP − FP = −1`. Even the most generous reading of ambiguous cases (all → TP) gives 7 TP / 3 FP, which is 30% false-positive rate on the cases where hybrid lifts above MM. That's high enough that hybrid would distort cross-stack comparisons unreliably.

V2 will revisit hybrid with: second LLM family agreement, stricter rule gating (require exact symbol match not just 75% token overlap), and a calibration dataset built from the audit cases.
