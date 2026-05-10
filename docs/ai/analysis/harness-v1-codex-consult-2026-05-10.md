# Harness V1 — Codex Consultation Brief

**Date**: 2026-05-10
**Branch**: `feat/harness-v1` HEAD `8a72fc8`
**Validation set**: 4 SWE-bench-Lite tasks (astropy-14995, django-11283, django-11797, django-12284), DeepSeek-V4-Pro codegen, claude_code planner.
**Stage**: Stage 31 (in-flight)

## Why I'm asking

Six validation iterations produced eight commits worth of pinpoint fixes,
each correctly diagnosing and patching the specific failure mode that
surfaced. Numbers improved (0/4 → 1/4 substantive real diffs), but every
iteration uncovered a new lower-level bottleneck. The remaining failures
are:

1. **Real bugs at the parser level** (Python 3.14 rejects astropy
   ndarithmetic.py) — fixable, just landed.
2. **Hallucinated bypass patterns** — model adds a `SUBQUERY_GROUP_BY_PRESERVE = True` settings flag instead of fixing the actual ORM logic; pattern survives across attempts and across runs.
3. **Closure-defined functions** — the bug needs `Field.contribute_to_class`'s body (the closure that names `_get_FIELD_display`); my AST/regex pin only catches top-level defs.

The pattern: I keep finding more specific bottlenecks. Each fix is
correct, but the iteration count is climbing and we're now in
diminishing returns territory. **Before continuing this whack-a-mole
loop, is there a more systemic angle I'm missing?**

## What's working

- Aider format codegen — DeepSeek consistently produces parseable
  search/replace blocks; converted to unified diff at boundary.
- Per-model context budgeter — DeepSeek 18 KB / Claude 80 KB split
  reflects actual reliable windows.
- Acceptance_tests from planner — `_get_FIELD_display` /
  `no_new_file_outside django/db/models/**` correctly populated for
  django-12284. Reviewer's acceptance_check rejects wrong-files diff.
- EVIDENCE_GAP / NO_CHANGE_NEEDED terminal markers — model honestly
  signals when it can't proceed; no more 3-attempt retry storms.
- Task 2 (django-11283 migration) — stable, growing real diff
  (1184 → 1207 → 1034 → 1503 chars across runs) on the right file.

## What's not working — failure-mode classes

### Class A: AST elision of pinned-but-deeply-nested target

**Symptom**: model emits EVIDENCE_GAP saying "the function I need is
not in the provided snippet."

**Mechanism**:
1. File is bigger than per-file budget (e.g. 25 KB > 6 KB).
2. AST truncator pins a function via `keep_symbols`, keeps body whole.
3. AST output is ~ 8 KB (signatures + small bodies + pinned body).
4. Caller byte-caps at 6 KB → pinned body sliced off (now fixed at
   `d26abed` to overshoot when pin honoured).
5. *Or* `ast.parse` fails outright (Python 3.14 strict triple-quote
   parity check rejects valid astropy code), returns source unchanged,
   caller byte-caps raw → pinned body never even considered (now fixed
   at `8a72fc8` with regex/indent fallback).

**Status**: addressed. Awaiting v7 to confirm.

### Class B: model hallucinates settings-flag bypass

**Symptom**: instead of editing `Query._build_filter()` or similar,
model adds `SUBQUERY_GROUP_BY_PRESERVE = True` to `global_settings.py`
plus an unused `_ = ...` reference plus a test that just asserts the
setting exists.

**Observed in**:
- django-11797 v3: invented `PRESERVE_SUBQUERY_GROUP_BY = True`.
- django-11797 v6: invented `SUBQUERY_GROUP_BY_PRESERVE = True` (same
  pattern, different name).
- django-12284 v4: invented edits to AUTHORS / CONTRIBUTING / INSTALL
  (different shape but same "tangential bypass" anti-pattern).

**Hypothesis**: when the model can see `must_touch_files` but NOT a
clear minimal-edit anchor inside them, it falls back to "add a
top-level switch" because that's the easiest valid SEARCH/REPLACE to
write. The structural correctness of the diff matters more to the
model than whether the diff fixes anything.

**What I haven't tried**: explicit anti-pattern prompting in the
playbook, e.g. "Settings flags, monkey-patches, and 'preserve_*' style
opt-ins are NOT VALID FIXES for ORM/query/feature bugs. The fix must
modify the actual code path that produces the wrong behaviour."

### Class C: target lives in a closure / nested scope

**Symptom**: model says "`_get_FIELD_display` is not present in
fields/__init__.py" because `_get_FIELD_display` is *defined inside*
`Field.contribute_to_class()` as a closure, not as a top-level def.

**Mechanism**: my keep_symbols pin walks `def`/`async def` at any
indent, but it pins by name. `_get_FIELD_display` IS a `def`, just
nested. So in principle it should be pinned. But either:
- regex fallback couldn't see it (need to verify on real file)
- AST output ordered nested closures inside `contribute_to_class`,
  byte-cap may have still chopped (now overshooting per d26abed
  should fix)
- the right name to pin is `contribute_to_class`, not
  `_get_FIELD_display` — the model needs to see the *enclosing*
  function's body to understand the closure

**Open question**: when pinning a closure-defined function, should we
ALSO pin its enclosing function so the closure's defining context is
visible?

### Class D: planner must_touch is right but harness 二次注入引入垃圾

**Already fixed at `103fe8d`** — metadata files (LICENSE / AUTHORS /
*.po / *.rst) excluded from the must_touch fallback. Listed for
completeness.

## Iteration history (8 commits, 6 validations)

| Run | HEAD | Astropy | Django-11283 | Django-11797 | Django-12284 |
|---|---|---|---|---|---|
| baseline | pre-Tier1 | 0/0/0/0 (90-140 KB context overflow, no diffs) | | | |
| v2 (Tier 1) | 841cbf3 | 2049 (no fix) | 1496 (real, sandbox apply timeout) | 0 (byte-trunc on 25 KB query.py) | hung 21 h |
| v3 (Tier 1.5+2) | 950d577 | 0 (Aider parse fail on EVIDENCE_GAP, retried 3×) | 1184 (real) | 776 (hallucinated setting) | 0 |
| v4 (e2ee413) | e2ee413 | 0 (425 s — fast EVIDENCE_GAP termination) | 1207 | 0 (honest EVIDENCE_GAP, not hallucinating) | 2627 (junk metadata edits) |
| v5 (103fe8d) | 103fe8d | 0 (ast.parse on the file) | 1034 | 0 | 0 (metadata exclusion fixed junk) |
| v6 (d26abed) | d26abed | 0 (ast.parse still failing) | **1503** | 1286 (hallucinated setting, awaiting_approval) | 0 (closure / nested target) |

Commits added between runs:
- `7a14b02`: Tier 1.5 Aider format wired
- `45ca755`: Tier 1.3 planner emits acceptance_tests
- `b5c3c54`: Tier 2 AST structural truncation
- `764d3f2`: Tier 2 per-model context budgets
- `e2ee413`: EVIDENCE_GAP terminal handler + keep_symbols (direct mention)
- `54f1848`: AST cross-reference (concept word → function name)
- `103fe8d`: metadata file exclusion
- `d26abed`: keep_symbols pin overrides per-file byte cap
- `8a72fc8`: regex/indent fallback when ast.parse fails

## Open questions for codex

1. **Hallucinated bypass class** (Class B): is anti-pattern prompting
   the right intervention, or does this signal a deeper "model can't
   make progress so it fakes one" issue that prompting alone won't
   fix? The Aider/Cursor literature on this would help.

2. **Closure pinning** (Class C): when we pin a nested function, should
   we also pin its enclosing chain? Or is the right move to extract
   *all* function names within ~ 200 bytes of any top-level pin?

3. **Diminishing returns**: I'm ~ 8 commits deep into specific fixes.
   Is the right next step Tier 4-H (let the model fetch additional
   file content via tool calls when it sees EVIDENCE_GAP) instead of
   another round of context-budget tuning?

4. **Sanity check the harness-first KPI**: does our SWE-bench-Lite
   smoke test (4 tasks: astropy / 3 django) bias toward Class C
   failures because Django's ORM code is densely closure-based?
   Should we add 2-3 simpler-codebase tasks (e.g. Flask / requests /
   pytest) to tell apart "harness has a structural gap" from "harness
   is fine for non-Django code"?

5. **Anti-pattern via acceptance_tests**: the planner now emits
   `diff_contains_pattern` and `no_new_file_outside`. Could a third
   kind, like `forbids_pattern_in_diff` (e.g. forbids
   `^[A-Z_]+ = True$` at module level when the issue is about ORM
   logic), kill Class B at the gate level rather than at the prompt?

## Validation methodology going forward

Backend currently on commit `d26abed`. `8a72fc8` (regex fallback) is
not yet active in any validation run; v7 will be the first run that
includes it. Before v7, want a synthesis from codex to know whether
to:

- Continue iterating (run v7 with current chain, then act on results)
- Pivot to Tier 4-H (tool-use loop, model fetches file content
  on-demand)
- Add anti-pattern prompting + forbids-pattern acceptance_test kind
- Some combination

## Repository state

```
$ git log --oneline feat/harness-v1 ^checkpoint/pre-reclassify | head -20
8a72fc8 fix(harness-v1): regex/indent truncation fallback when ast.parse fails
d26abed fix(harness-v1): keep_symbols pin overrides per-file byte cap
103fe8d fix(harness-v1): exclude repo metadata from must_touch + injection paths
54f1848 fix(harness-v1): cross-reference issue concept words against file AST
e2ee413 fix(harness-v1): respect terminal markers + pin issue-mentioned symbols
0e28b48 docs: SESSION_HANDOFF for 2026-05-10 — Stage 31 Tier 1.5 + Tier 2 wave
950d577 docs: STAGE_LOG entry for 2026-05-10 — Stage 31 Tier 1.5 + Tier 2 wave
764d3f2 feat(harness-v1): per-model categorical context budgeter (Tier 2)
b5c3c54 feat(harness-v1): AST-aware structural truncation for big Python files (Tier 2)
45ca755 feat(harness-v1): planner emits acceptance_tests (Tier 1.3 closure)
7a14b02 feat(harness-v1): wire Aider search/replace format into codegen (Tier 1.5)
```
