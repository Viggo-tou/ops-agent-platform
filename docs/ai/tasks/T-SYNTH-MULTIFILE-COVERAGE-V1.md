# T-SYNTH-MULTIFILE-COVERAGE-V1 — Force synthesis to cover all explicitly-mentioned entities

<!-- Effort: small (prompt + harness post-processor) -->
<!-- Executor: codex (xhigh), but DO NOT IMPLEMENT until next session -->

**Status:** todo (DEFERRED — implement next session, not this one)
**Priority:** P0 (Stage 20C cheapest-first lever per V2 disagreement audit)
**Created:** 2026-05-01
**Branch (future):** `feat/synth-multifile-coverage-v1` based on whatever HEAD has the V2-CLI merged
**Linked:**
- `DECISIONS.md` D-010 second amendment (V2 verdict)
- `docs/ai/specs/stage20-judge-verdict.md` (V1 landing addendum)
- `docs/ai/tasks/T-JUDGE-HYBRID-V2-CLI.md` (V2 cross-family judge that exposed this signal)

## Why this exists

The V2 cross-family rejudge surfaced 50 keypoints in `both_no_evidence_yes` cell — retrieval found the expected file with the keypoint substring in card_text, but neither MiniMax nor Codex saw the answer articulating it. Manual inspection of 8 of these (mixed dashboard + handymanapp, A/B/C tiers) found:

- **5/8 = 62.5% real synthesis gaps**, dominated by ONE failure mode: when the question explicitly mentions multiple files/classes/components, synth fixates on one (usually the highest-confidence retrieval) and silently omits others.
- 2/8 = judge over-strict on path-token format (separate ticket: `T-JUDGE-V3-PATH-TOLERANCE` if pursued)
- 1/8 = ambiguous / partial coverage

Concrete example (DASH C-05): question asks about Firebase usage across `Login.js`, `ServiceAnalytics.js`, `Dashboard.js`. Answer covers Login.js comprehensively but never mentions ServiceAnalytics or Dashboard at all. Both LLM judges correctly say "missed" on those 2 keypoints.

This is **NOT a model-ability problem** — MiniMax and Codex both write decent prose. It's a **synthesis instruction problem**: current prompt doesn't enforce per-mentioned-entity coverage. The fix is the cheapest available Stage 20C lever (~1-2 days) and runs entirely in the prompt + a deterministic harness post-processor; no model swap, no cards rewrite, no API budget.

## Goal

Prevent synthesis from over-focusing on one retrieved file when the question explicitly mentions multiple files/classes/components or asks for a flow.

Specifically: when the question text mentions ≥2 distinct code entities (file names, class names, component names matching standard regex patterns), the synthesized answer must include ≥1 specific factual claim about each mentioned entity. "Specific factual claim" = a sentence referencing at least one of: an API call, a method/field name, a data path (e.g. Firebase path), a routing decision, a state field, or a concrete behavior on that entity.

## Design

### A. Entity detection (deterministic, not LLM-based)

In `apps/backend/app/services/knowledge.py` (or wherever the synthesis prompt is assembled), before calling the LLM, run an entity extractor on the question text. **Use regex extraction for V1** — it's cheap, auditable, and good enough for 70-80% of cases. Concrete pattern:

```python
ENTITY_PATTERN = re.compile(
    r"\b("
    r"[A-Z][A-Za-z0-9]+(?:Fragment|Activity|Adapter|ViewModel|Screen|Service|Controller|Component|Page)"  # PascalCase + Android/React role suffix
    r"|[A-Z][A-Za-z0-9]+\.(?:kt|js|tsx|ts|jsx|py|java|go)"  # File names with extension
    r"|[a-z][a-z0-9_]*\.(?:xml|json|yml|yaml|gradle)"  # Lowercase resource files
    r"|`[^`]{3,80}`"  # Anything explicitly backticked in the question
    r")\b"
)

def extract_question_entities(question: str) -> list[str]:
    raw = ENTITY_PATTERN.findall(question)
    # Strip backticks, dedupe preserving order, filter common English words
    return _normalize_entities(raw)
```

Output: ordered list of entity names. Empty list → use the legacy single-focus prompt; non-empty → use the multi-entity coverage prompt (B).

**Spec invariant**: regex is the V1 mechanism. Future T-SYNTH-MULTIFILE-COVERAGE-V2 may add LLM-based extraction as fallback for entities the regex misses, but V1 ships regex-only.

### B. Multi-entity synthesis prompt (when ≥2 entities detected)

Inject these instructions into the existing synthesis system prompt **only** when entity count ≥2:

```
The user's question explicitly mentions the following code entities, in order:
  1. <entity_1>
  2. <entity_2>
  3. <entity_3>

Your answer MUST include at least one specific factual claim about each
of these entities. A "specific factual claim" is a sentence that references
a concrete API, method, field, file path, data path, routing decision, or
behavior of that entity. A generic mention like "X is also part of this
flow" does NOT count.

If the retrieved evidence does not contain enough information to make a
specific claim about a mentioned entity, write exactly: "<entity_name>:
not covered by retrieved evidence." Do not omit it silently. Do not
fabricate behavior.

Structure: when answering a flow / comparison / multi-file question, use
either ordered steps (for flow) or per-entity bullets (for comparison /
listing). Avoid prose-only structure when ≥3 entities are listed.
```

**Spec invariant**: prompt addition is appended, not rewritten. Existing single-focus mode stays unchanged for questions with 0-1 detected entities.

### C. Harness post-processor (deterministic verification)

After the synth answer is generated, the harness checks coverage. New per-record fields in the bench artifact:

```python
record["mentioned_entities"] = extract_question_entities(question)
record["covered_entities"] = [
    e for e in record["mentioned_entities"]
    if _entity_appears_in_answer(e, answer)
]
record["omitted_entities"] = [
    e for e in record["mentioned_entities"]
    if e not in record["covered_entities"]
]
record["multifile_mode_active"] = len(record["mentioned_entities"]) >= 2
record["coverage_rate"] = (
    len(record["covered_entities"]) / len(record["mentioned_entities"])
    if record["mentioned_entities"] else 1.0
)
```

`_entity_appears_in_answer(e, a)`: checks if entity name (or its core class/file token, ignoring extension) appears as a substring in answer. Deterministic, no LLM call. Used for instrumentation, NOT for scoring (V1 scoring stays unchanged).

Summary aggregates:

```python
summary["multifile_mode_records"] = <count of records with ≥2 mentioned entities>
summary["multifile_mode_avg_coverage_rate"] = <mean over those records>
summary["total_omitted_entities"] = <sum of omitted across all records>
```

### D. Files to edit (in implementation phase)

1. `apps/backend/app/services/knowledge.py` (or the synthesis-prompt assembly site) — entity extraction + conditional prompt injection.
2. `apps/backend/scripts/run_qa_benchmark.py` — post-processor + new artifact fields + summary aggregates. Confirm: `extract_answer_and_citations` should also surface entity coverage in the per-record output.
3. `apps/backend/scripts/rejudge_run.py` — mirror entity coverage post-processing if rejudge re-extracts answers.
4. `apps/backend/tests/scripts/test_run_qa_benchmark.py` — 4 new tests:
   - `test_extract_question_entities_finds_camelcase_classes`
   - `test_extract_question_entities_finds_dotted_filenames`
   - `test_extract_question_entities_finds_backticked_tokens`
   - `test_summary_aggregates_multifile_coverage`
5. `apps/backend/tests/services/test_knowledge_synthesis.py` (or existing synthesis tests) — 2 new tests asserting that with ≥2 entities, the prompt contains the multi-entity coverage block; with 0-1 it does not.

## Acceptance — targeted regression set FIRST, then full bench

### Phase 1: Targeted regression (5 specific questions)

Re-run ONLY these 5 questions (extract them into `tests/benchmarks/qa_regression_multifile_v1.jsonl`):

| Q | Dataset | Expected entity coverage |
|---|---|---|
| DASH C-05 | dashboard | Must mention `Login.js`, `ServiceAnalytics.js`, `Dashboard.js`, `SupportFeedback.js` (and `firebase.js`) per their respective Firebase usage. |
| HAND C-12 | handymanapp | Must distinguish customer KYC flow (CustomerKYC* files) from handyman KYC flow (HandymanKYC* files). |
| HAND C-09 | handymanapp | Must mention CustomerJobListFragment AND CustomerJobDetailsFragment AND CustomerJobListAdapter, including the row-tap → Safe Args → details navigation. |
| DASH B-09 | dashboard | Must preserve specific UX constraints (prev/next disable behavior, current-page styling). |
| DASH B-04 | dashboard | Must mention `replies` array append AND status transition to "In Progress" AND EmailJS reply email. |

**Pass criteria for Phase 1**:

- Re-running these 5 Qs under V1 (MM-only) judge:
  - `multifile_mode_avg_coverage_rate` ≥ 0.85 across the 5 (vs current ~0.4 measured from V2 disagreement set)
  - per-Q `coverage_rate` ≥ 0.66
- Per-Q score change vs prior run: each must be ≥ prior score, none regress more than -3 points.
- Targeted-regression artifact committed alongside the implementation.

### Phase 2: Full bench (only after Phase 1 passes)

- Run handymanapp 26Q + dashboard 34Q with `--judge-mode minimax` (V1 official) under the new prompt.
- `multifile_mode_records` count > 0 (sanity check that detection is firing).
- Aggregate handymanapp `multifile_mode_avg_coverage_rate` ≥ 0.75.
- Aggregate dashboard same metric ≥ 0.80.
- Per-tier mean change vs current V1 baseline (handymanapp 51.78 / dashboard 60.24): non-negative on aggregate; per-tier deltas allowed within ±3.
- `both_no_evidence_yes` count (re-derived from a hybrid_v2 rejudge of the new bench): ≥25% reduction from current 50.

### Sanity invariants

1. Questions with 0-1 detected entities use the existing single-focus prompt unchanged. Their behavior must not change.
2. The post-processor's `coverage_rate` is computed deterministically; same answer text yields same coverage_rate.
3. When the LLM legitimately writes "<entity>: not covered by retrieved evidence", the entity counts as **omitted** (not covered) for instrumentation, but the harness does not penalize the score — that signal is for diagnostics only.

## Out of scope

- LLM-based entity extraction (deferred to V2 if regex precision is too low).
- Synth model swap (Codex CLI as synthesizer) — separate ticket if V1 prompt fix proves insufficient.
- Cards-v2 (separate Stage 20C track for `both_no_rule_no_evidence_no` cases).
- Judge V3 path-token relaxation (separate ticket targeting Samples 1-2 from the audit).
- Re-rejudging existing artifacts under the new synthesis prompt — those artifacts capture pre-fix synthesis, leave them as-is for audit.

## Workflow (next session)

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/synth-multifile-coverage-v1" \
  -c model_reasoning_effort=xhigh \
  - < docs/ai/tasks/T-SYNTH-MULTIFILE-COVERAGE-V1.md
```

Worktree from current `checkpoint/pre-reclassify` HEAD (after V2-CLI merge).

**Implementation phase order** (mandatory, do not parallelize):

1. Implement entity extractor + tests for it.
2. Implement prompt injection + tests for it.
3. Implement harness post-processor + tests.
4. Run targeted regression set (Phase 1 acceptance).
5. ONLY IF Phase 1 passes: run full bench (Phase 2).
6. If Phase 2 passes: merge.
7. If Phase 1 or 2 fails: stop, report metrics, do not merge. Iterate spec.

## Why this is the right Stage 20C P0

Three small-scope levers were considered after the V2 audit:

| Lever | Targets | Cost | Estimated lift |
|---|---|---|---|
| **This (synth prompt fix)** | 5/8 of `both_no_evidence_yes` (multi-file selection) | 1-2 days | 25-40% reduction in `both_no_evidence_yes` |
| Judge V3 path tolerance | 2/8 of `both_no_evidence_yes` (path-token format) | 1 day | 10-15% reduction |
| Cards-v2 narrow | `both_no_rule_no_evidence_no` (62 kps, true misses) | 2-4 weeks | unknown, larger commitment |

This ticket is cheapest AND highest-leverage among the three. Synthesis model upgrade (Codex synthesizer) is deferred until prompt fix is measured — if it picks up most of the 50 kps, model swap is unnecessary.

## What this ticket does NOT claim

- Does NOT claim synth prompt fix will close the entire cross-stack gap.
- Does NOT claim ≥0.85 coverage_rate is achievable without LLM-based extraction (regex precision is the V1 ceiling; V2 may need LLM extractor for the residual).
- Does NOT change the official judge or scoring formula.
- Does NOT change retrieval, cards generation, or FTS5.
