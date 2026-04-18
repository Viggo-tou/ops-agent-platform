# T-040 Validator Hardening — Evidence of "no-found > fabrication"

**Date:** 2026-04-16 00:00:20 UTC (2026-04-15 local)
**Task:** `4063abb4-73e4-4142-8629-d826f040a7ac`
**Knowledge source:** `hosteddashboard` → `D:\项目\HostedDashboard\handyman-admin-dashboard`

## Request

> P69-10 fix: in hosteddashboard, delete the array element with id "master1" from src/data/mockUsers.js, and in src/pages/Dashboard.js move the top-level localStorage.getItem currentUser read into a useEffect inside the Dashboard component. Touch only those two files. Do not create new files. Apply the patch.

## Pipeline outcome

| Stage | Result |
|---|---|
| classify_request | `jira_issue_develop` ✓ |
| translation (MiniMax) | objective captured, grounding_terms = `[master1, currentUser, localStorage.getItem, useEffect, mockUsers.js, Dashboard.js]` ✓ |
| plan (MiniMax) | **hallucinated** must_touch_files: `[src/data/mockUsers.js, src/data/feedbackData.js, src/pages/Login.js, src/pages/FirebaseTest.js]` — two of those have nothing to do with the request |
| codegen (MiniMax) | over-engineered patch that INCREASED `localStorage` occurrences from 14 → 66 and `getItem` from 11 → 63; touched zero of the must_touch files |
| **conformance validator (T-040)** | **verdict=block** — 12 findings across `hit_delta`, `must_touch`, and `planner_must_touch` |
| final task status | `failed`, `jira_transitioned=false` |
| attempts_used | 2 |

## Validator findings (abridged)

```
hit_delta      master1       before=2 after=2  (no decrease)
hit_delta      mockUsers     before=3 after=3  (no decrease)
hit_delta      localStorage  before=14 after=66  (INCREASED)
hit_delta      getItem       before=11 after=63  (INCREASED)
hit_delta      useEffect     before=27 after=27  (no change)
must_touch     master1       hit_files=[src/data/mockUsers.js], touched_files=[]
must_touch     mockUsers     hit_files=[...2 files...], touched_files=[]
must_touch     localStorage  hit_files=[...11 files...], touched_files=[]
must_touch     getItem       hit_files=[...11 files...], touched_files=[]
must_touch     currentUser   hit_files=[...13 files...], touched_files=[]
must_touch     useEffect     hit_files=[...12 files...], touched_files=[]
planner_must_touch  missing_from_diff = all 4 planner-declared files
```

## Conclusions

1. **T-040 防线4 (spec_conformance validator) works as intended.** The hallucinated patch from MiniMax was blocked with surgical precision — the validator identified exactly which anchors should have decreased but did not, and exactly which declared must_touch files the patch skipped.
2. **`jira_transitioned=false`** — the governance gate held. No bad patch reached Jira.
3. **T-040 objective ("no-found > fabrication") achieved** — rather than silently accepting a fabricated patch (the T-039 failure mode), the pipeline halted with concrete evidence explaining why.
4. **Remaining gap (not a T-040 defect):** MiniMax codegen is not surgical enough to produce a passing patch for this request. Getting a `verdict=pass` outcome on this exact P69-10 request would require either tightening the codegen prompt (forbid new-file creation when request says so) or delegating codegen to a stricter model (e.g. codex, available from 2026-04-17). T-040's job is to **block hallucinations**, which it does.

## Artifacts

- Full task JSON: `docs/ai/evidence/T-040/autotest-4063abb4-full.json`
- Second run (tightened codegen prompt): `docs/ai/evidence/T-040/autotest-b46daf51-full.json` — 7 findings including new `shadow_implementation` rule firing
- Autotest runner: `scripts/t040_autotest.py`
- Previous blocked task (same pattern): `599ef22a-67ca-45da-8427-fde1bf7f3511`

## Second run (2026-04-16 00:39)

Same request, after tightening `CODEGEN_SYSTEM_PROMPT_JSON_MODE` to forbid new-file creation
when task text says "do not create new files" / "touch only" / "only these files".

- Task `b46daf51-2ae1-4c5d-8f6b-2e270026ef93` → status=`failed`, attempts_used=2
- 7 findings:
  - `shadow_implementation` — "Request asks to modify or remove existing behavior, but patch only creates new files"
  - `hit_delta` on master1 (2→2), currentUser (52→74), mockUsers (3→6), localStorage (14→19), getItem (11→16), useEffect (27→44)
- Planner again hallucinated must_touch: `[mockUsers.js, UserManagement.js, UserVerification.js, App.js]` — the last 3 are wrong scope.

Conclusion: **MiniMax codegen will not produce a surgical patch for P69-10 regardless of prompt tightening.** The validator is the only thing standing between MiniMax and a wrong Jira transition, and it correctly holds the gate every time.
