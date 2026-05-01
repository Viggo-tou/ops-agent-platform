# T-SYNTH-MULTIFILE-COVERAGE-V1-WIDEN-EXTRACTOR — broaden entity regex to fix Phase 1 zero-fire problem

<!-- Effort: small -->
<!-- Executor: codex (xhigh) -->

**Status:** todo (P0 — fixes mechanism non-validation in T-SYNTH-MULTIFILE-COVERAGE-V1)
**Priority:** P0 (blocks Phase 2 + UX dogfood)
**Created:** 2026-05-01
**Branch:** continue on `feat/synth-multifile-coverage-v1` (existing worktree)
**Linked:**
- `docs/ai/tasks/T-SYNTH-MULTIFILE-COVERAGE-V1.md` (parent ticket; this is a follow-up)
- Phase 1 results: 5/5 Qs got `multifile_mode_active = False` (regex too narrow)
- Score still lifted +8.8 average → mechanism unattributed; this ticket validates it

## Background

T-SYNTH-MULTIFILE-COVERAGE-V1 implementation landed and produced a +8.8 average score lift on the Phase 1 regression set (5 Qs). However `multifile_mode_active = False` on **all 5** records — the entity extractor's regex did not match the natural-language entities in the targeted questions. The score lift is therefore unattributed to the spec's hypothesis.

Examples of unmatched entities (from Phase 1 questions):

- `PaginationControls` — PascalCase but no role suffix in the original `(Fragment|Activity|Adapter|...|Page)` list
- `Firebase`, `RecyclerView` — PascalCase, single-word, no extension
- `customer KYC`, `handyman KYC` — lowercase phrase containing ALLCAPS abbrev
- "fragments consume the customer-side job list" — list/conjunction phrasing without explicit code names

Without the regex firing, the multi-entity coverage block is never injected into the synthesis prompt, so the spec's mechanism is structurally untested.

## Goal

Widen `ENTITY_PATTERN` and the `extract_question_entities` helper in `apps/backend/app/services/knowledge_synthesis.py` to recognize the entity classes the natural-language Phase 1 questions actually use, while preventing over-firing on simple A-tier single-file questions.

## Design

Replace the V1 regex with a multi-rule pipeline. Each rule produces candidate entity strings; the union is normalized + deduped through the existing `_normalize_entities`.

### Rule 1: PascalCase (≥2 internal capitals) — broader than V1

```
(?<![A-Za-z0-9_])
[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+    # at least one PascalCase boundary
(?![A-Za-z0-9_])
```

Matches `PaginationControls`, `FirebaseAuth`, `FormValidator`, `RecyclerView`, `ServiceAnalytics`, `CustomerJobListFragment`, `Dashboard` (no — single capital + lowercase is one word, see exclusion below). Drops the role-suffix requirement entirely.

**Exclusion**: single capital + lowercase is NOT enough. Require ≥2 capital boundaries to avoid ordinary capitalized English nouns ("Dashboard" alone wouldn't match; "DashboardPage" or `Dashboard.js` does). Acceptable false-negative on `Dashboard` standalone — it's caught by Rule 2 if `.js` follows.

### Rule 2: Filenames with code/resource extensions — unchanged from V1

```
[A-Za-z][A-Za-z0-9_-]*\.(?:kt|js|tsx|ts|jsx|py|java|go|xml|json|yml|yaml|gradle)
```

Catches `Login.js`, `Dashboard.js`, `nav_graph.xml`, `Job.kt`. Already in V1.

### Rule 3: ALLCAPS-abbrev compound phrases — NEW

Recognize multi-word phrases where one component is an ALLCAPS abbreviation 2-5 chars long (KYC, API, OAuth uppercase variants, REST, JWT). Match patterns:

```
([A-Za-z][a-z]+\s+){0,2}[A-Z]{2,5}(\s+[a-z]+){0,2}
```

with constraint that the ALLCAPS token is 2-5 chars and the whole match has at least one lowercase word adjacent (so `KYC` alone is NOT an entity, but `customer KYC`, `KYC flow`, `OAuth login` are).

**Exclusion**: standalone ALLCAPS word with no lowercase neighbor (HTML / API / KYC by itself in question) is NOT an entity. ALLCAPS is too easy to false-positive on metadata words.

### Rule 4: Backticked tokens — unchanged

```
`[^`]{3,80}`
```

### Rule 5: Comma/and-list expansion — NEW

When the question contains "X, Y[, Z]" or "X and Y" patterns where each X/Y/Z matches Rule 1-4, extract each component as a separate entity. Use a permissive list-pattern detector:

```
(<entity_match>)(\s*,\s*<entity_match>){1,}\s+(?:and\s+)?(<entity_match>)?
|<entity_match>\s+and\s+<entity_match>
```

Implementation note: easier to do as a post-processor — after extracting via rules 1-4, scan the question for "and"/"," tokens between two extracted entities to confirm list-pattern usage. Doesn't add new entities but logs the list relationship in `record["entity_list_pattern_detected"] = True` (for diagnostic only, not used in scoring).

## Sanity invariants (must hold)

1. **Single-entity Qs stay single-focus**. A question with exactly 1 detected entity does NOT trigger `multifile_mode_active`. The threshold remains ≥2 distinct entities.
2. **Standalone English words don't match**. "fragment", "controller", "service", "page", "screen" alone are not entities. Only PascalCase compounds containing them, or filenames, or backticked phrases.
3. **Plain ALLCAPS words don't match alone**. "API", "JSON", "HTML", "KYC" alone do not become entities. Only compounds containing them.
4. **Order preserved**. `extract_question_entities` returns entities in question-order (already true in V1; preserve).
5. **Existing single-entity tests continue to pass** unchanged.

## Files to edit

1. `apps/backend/app/services/knowledge_synthesis.py`
   - Update `ENTITY_PATTERN` to combine Rules 1-4. Use a single `re.compile` with alternation, OR multiple sub-patterns combined in `extract_question_entities`.
   - Update `_normalize_entities` if needed to handle the new compound-phrase entries (strip leading/trailing whitespace, dedupe by lowercased form).
   - Optionally add `_detect_list_pattern(question, entities)` returning bool — used only as a diagnostic field if cheap.
2. `apps/backend/tests/services/test_knowledge_synthesis.py`
   - Replace existing entity extraction tests with broader coverage. ALL of these must pass:
     ```python
     # PascalCase no-suffix
     assert extract("PaginationControls and FirebaseAuth") == ["PaginationControls", "FirebaseAuth"]
     assert extract("How does FormValidator work") == ["FormValidator"]
     assert extract("the RecyclerView pattern") == ["RecyclerView"]

     # ALLCAPS-compound
     assert extract("the customer KYC and handyman KYC flows") == ["customer KYC", "handyman KYC"]
     assert extract("uses OAuth login") == ["OAuth login"]

     # File extensions
     assert extract("Login.js, Dashboard.js, ServiceAnalytics.js") == ["Login.js", "Dashboard.js", "ServiceAnalytics.js"]
     assert extract("nav_graph.xml routes") == ["nav_graph.xml"]

     # Standalone words EXCLUDED
     assert extract("how does the page render") == []
     assert extract("Fragment lifecycle") == []     # Fragment alone, capital+lowercase
     assert extract("the API returns json") == []   # ALLCAPS alone
     assert extract("KYC validation logic") == ["KYC validation"] or [] (TBD — see note below)

     # Combined
     assert extract("Login.js calls FirebaseAuth.signIn during the customer KYC flow") == ["Login.js", "FirebaseAuth", "customer KYC"]
     ```
   - Note for "KYC validation": ambiguous. Rule 3 says "ALLCAPS-abbrev with adjacent lowercase". `KYC validation` matches Rule 3. Document the choice in the test.
   - Add 2-3 negative tests confirming A-tier-style simple Qs don't over-fire (e.g. `extract("Where is HandymanLogin.kt") == ["HandymanLogin.kt"]` → 1 entity, not 2; multifile mode wouldn't fire).

## Acceptance

1. `python -m compileall apps/backend/app/services/knowledge_synthesis.py apps/backend/tests/services/test_knowledge_synthesis.py` clean.
2. All existing tests in test suites mentioned in T-SYNTH-MULTIFILE-COVERAGE-V1 still pass + new entity tests pass. Total test count should be 56 baseline + new tests (estimate 10-15 new test cases including negatives).
3. Standalone unit test:
   ```python
   for q, expected_min_entities in [
       ("DASH B-09 question text", 1),  # PaginationControls
       ("DASH C-05 question text", 3),  # multiple files
       ("HAND C-09 question text", 2),  # fragments + multi
       ("HAND C-12 question text", 2),  # customer KYC + handyman KYC
       ("DASH B-04 question text", 1),  # Support Feedback page
   ]:
       assert len(extract_question_entities(q)) >= expected_min_entities
   ```
4. **No bench run in this ticket**. Phase 1 re-run is the parent session's responsibility AFTER this lands.

## Sanity invariants (must hold post-fix)

1. Existing 56 tests pass (no regression on the V1 implementation).
2. All 5 Phase 1 question texts return ≥1 entity from `extract_question_entities`.
3. At least 3 of the 5 Phase 1 question texts return ≥2 entities (would trigger multifile_mode).
4. A-tier "where is X" simple questions (any from the existing 26Q handymanapp dataset) return ≤1 entity (don't over-fire).

## Out of scope

- Changing the `multifile_mode_active` threshold (stays at ≥2 entities).
- Changing the multi-entity coverage prompt block.
- LLM-based entity extraction (deferred to V2; this ticket stays regex-only).
- Running benchmarks (parent session does that).
- Changing the harness post-processor's coverage check semantics.

## Workflow

```bash
codex exec --full-auto --sandbox workspace-write \
  -C "D:/项目/ops-worktrees/synth-multifile-coverage-v1" \
  -c model_reasoning_effort=xhigh \
  - < /tmp/codex-prompt-widen.txt
```

(Where `/tmp/codex-prompt-widen.txt` contains an activation wrapper + this spec.)

The worktree already has the V1 implementation (commit `8d2e653`). This ticket builds on top — same branch, additive edits.

**Implementation order**:
1. Update `ENTITY_PATTERN` and `extract_question_entities` per Rules 1-5.
2. Add the new tests (positive + negative + Phase 1 question texts).
3. Run the test suite from the worktree. Target: all green.
4. Stop. Do not benchmark.

## Why this is the right fix

Three options were considered:
- **Skip validation, run Phase 2 anyway** (rejected — 3 hours wall on unattributed lift)
- **LLM-based entity extraction** (rejected for V1 — overkill, cost, latency)
- **Widen regex** (THIS) — small change, deterministic, reuses existing pipeline

This ticket should land in <1 hour wall (codex 30-45 min + parent session re-Phase 1 after).
