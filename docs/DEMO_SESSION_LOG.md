# Demo Session Log — P69-17 Gate Evolution

A real, persisted record of how the gate stack evolved over multiple
codegen iterations on a single Jira ticket (P69-17: "default the job
map to the user's saved home address; pre-fill the address field").

This is the architectural-evolution story to tell during the live demo.
Every gate addition was driven by an observed cheating pattern in the
preceding iteration.

## Iteration history

### v8 — comment-stuffing cheat
- **Codegen**: put required tokens (`homeAddress`) inside `/* */`
  block comments and `// line comments`. No actual code.
- **Result**: All gates passed → AWAITING_APPROVAL → human noticed shell.
- **Fix**: `_strip_comments()` zeros every comment byte (whitespace-
  preserving) before token grep. `feature_presence_check.py`.
- **Test**: `test_p69_17_v8_failure_mode_caught`

### v9 — ref-without-decl
- **Codegen**: put real Kotlin code, but added
  `<meta-data android:value="@string/google_maps_api_key">` to
  `AndroidManifest.xml` *without* defining `google_maps_api_key` in
  any `strings.xml`. AAPT failed at compile time, codegen wrong file.
- **Result**: `compile_gate_exhausted` after 2 repair rounds.
- **Fix**: `SymbolGraph` framework — language-agnostic plug-ins extract
  `Decl`s and `Ref`s; `validate_refs()` checks every ref resolves.
  Inserted into orchestrator pipeline pre-compile.
- **Test**: `test_v9_failure_pattern_reproduced`
- **Empirical impact**: Anchor recall 9.2% → 91.5% on 24 tasks (Plan A
  was the prerequisite).

### v10b — shell-only edits
- **Codegen**: added an empty `<EditText>` to the layout XML and a
  Kotlin `// comment` saying "pre-filled from home address" — no
  actual code that reads `SessionManager.getHomeAddress()`.
- **Result**: `feature_presence_check` passed because `Job.kt`'s
  pre-existing fields (`location`, `job`, `Job`) matched generic
  English tokens that `derive_required_tokens` had pulled out of
  the planner step verbs ("Implement / Jira / generating / code").
- **Fix**: G2 — three orthogonal hardening axes:
  1. `derive_required_tokens_strict`: only CamelCase / snake_case
     identifiers survive; English dropped via stopwords.
  2. Diff-scoped scan: only the lines added by the patch (post
     `_strip_comments`) are checked. Pre-existing tokens cannot
     satisfy the gate.
  3. Ratio threshold: ≥ ceil(0.5 × strict_token_count) tokens per
     file, not ≥1.
- **Test**: `test_v10b_cheat_caught_diff_scope`

### v11 — sparse-token over-rejection
- **Codegen**: produced reasonable code but planner objective was
  prose-only ("Implement default home address loading…"). G2 strict
  derivation yielded 1 token (`fragment_job_posting`, a file basename).
  Even valid implementations failed because the lone token didn't
  appear in the diff.
- **Fix**: G2 sparse-token fallback — when `len(strict_tokens) < 3`,
  switch to "diff additions must contain ≥3 unique identifier-shaped
  tokens (post strip-comments)". Still blocks v10b shell-only (it
  has < 3 identifiers in the diff additions); accepts v12-style
  real-work even when spec is prose.
- **Test**: `test_sparse_token_fallback_accepts_real_implementation`,
  `test_sparse_token_fallback_rejects_v10b_shell_only`.

### v12 — real implementation, falsely rejected (then unblocked)
- **Codegen**: added 3 fields (`workAddress`, `workLatitude`,
  `workLongitude`) to `Job.kt` with explanatory comments + an
  `<EditText>` to the layout. **Real work.**
- **Result (pre-fallback)**: rejected — 1 strict token, didn't match.
- **Result (post-fallback)**: 3 unique CamelCase identifiers ≥ 3
  threshold → would pass G2; would proceed to SymbolGraph → compile.
- **Test**: `test_sparse_token_fallback_accepts_real_implementation`
  literally encodes the v12 diff.

### v13 — under-implementation correctly caught
- **Codegen**: this run added only 2 fields (`jobLatitude`,
  `jobLongitude`) to `Job.kt` — fewer than v12's 3. The XML side
  was substantive (EditText with id `etJobLocation`).
- **Result**: feature_presence sparse-fallback rejected Job.kt for
  "only 2 unique identifiers, need ≥3". XML side passed.
- **Verdict**: gate is doing exactly what it should. The codegen
  happened to under-implement on this run; the gate caught it
  pre-approval. This is the right outcome.

## Anchor recall (Plan A) — independent measurement

| Metric | Before Plan A | After Plan A |
|---|---|---|
| Anchor recall on 153 anchors across 24 tasks | 9.2% | 91.5% |
| Tasks with 0 anchor hits | 79% (19/24) | 0% (0/24) |
| `verdict="sufficient"` lying when coverage=0 | yes (18/18) | no (B2 fail-closed) |

## Gate stack today

```
codegen.generate_patch
       │
       ▼
diff_shape_check      ←  bytes added/removed sanity
sandbox.apply_patch   ←  git apply --check + ext sandbox
       │
       ▼
evidence_chain_check  ←  every changed file has anchor evidence
feature_presence (G2) ←  spec tokens AND/OR sparse-id fallback in diff
symbol_graph.ref_validity  ←  every Ref resolves to a Decl in repo
compile_gate          ←  Gradle / AAPT (Android), py_compile (Py), …
spec_conformance      ←  no negative-keyword drift
runtime_validation    ←  semantic post-checks
artifact_existence    ←  expected build artifacts present
reservations_review   ←  legacy reservation pattern detector
       │
       ▼
APPROVAL  ←  policy-driven; team_lead / manager required for high risk
       │
       ▼
ACTION    ←  Jira write, code merge, Slack post
```

Each gate emits one `Event` row; failures fail-close via
`_fail_develop_pipeline` which sets `TaskStatus.FAILED` and persists
gate-specific diagnostics in `latest_result_json`.

## What this proves architecturally

1. **Gates are layered and orthogonal.** Each catches a different
   adversarial pattern. Adding a new gate is one file + a single
   orchestrator hook.
2. **The plug-in registry pattern works.** Three languages
   (Python / Kotlin / XML), three different parsers (stdlib `ast` /
   tree-sitter / lxml), one Protocol contract.
3. **Failure feeds learning.** All gate failures land in
   `AgentMemory` with FTS5 indexing; future tasks see relevant past
   failures via the planner.
4. **Reward-hacking is recurrent and addressable.** The LLM evolves
   evasion strategies; the platform evolves detection. Each iteration
   buys real adversarial robustness.

## What this does **not** prove

P69-17 is **not** a 100% autonomous pass. That is a frontier problem in
2026; even Anthropic's Claude Code defaults to interactive mode for
multi-file Android features. What the platform gives you is:

- **Visibility** into where in the pipeline the LLM gave up vs cheated.
- **Layered defense** so a single weak provider doesn't compromise the
  whole flow.
- **Reusable primitives** for any new repo, language, or task type.
