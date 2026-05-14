# Failure Taxonomy (v16+)

> **Purpose**: every incident, every gate, every fix declares the failure
> class it addresses. Without this document, fixes accumulate as a patch
> pile with no shared vocabulary; with it, repeated occurrences of one
> class become the trigger to abstract that class into a system-layer
> capability.
>
> **Discipline** (set 2026-05-12):
> 1. New incident report → tag it with one or more class IDs from this
>    file. If the failure doesn't fit any class, add a new class entry
>    *before* writing the fix.
> 2. New gate / prompt / retry mechanism → declare which class(es) it
>    catches AND what it deliberately doesn't catch.
> 3. Same class seen in 2+ stages → next stage opens "consolidate
>    <class>" as its trigger. (Caveat: at 2 occurrences "fill once more
>    to confirm the shape" is still allowed if explicitly stated; default
>    to abstraction at 3.)

---

## Class status legend

- `OPEN` — class is named but no mitigation is in place
- `MITIGATED` — 止血 layer in place (gate / prompt / classifier) but the
  architectural fix is still pending
- `SOLVED` — addressed by a load-bearing structural change; the symptom
  shouldn't recur without a deeper regression

---

## C1: `plan_under_specified`

**Definition**: planner emits a plan whose `acceptance_tests` array is
empty (or trivially short) for a Jira that asks for a feature add. The
plan looks structurally valid (must_touch_files non-empty, change_summary
present) but supplies no contract for what "done" means. Downstream gates
all measure correctness; without acceptance_tests they have nothing to
measure against, so a polish-only patch slips through as success.

**Recognized by**:
- `plan_generated.payload_json.plan.acceptance_tests` is empty or has
  length < expected_domain_contract_count
- Subsequent run reaches AWAITING_APPROVAL with `added_lines < 30` on a
  Jira whose description clearly asks for a feature

**Seen in**:
- `b5d0a085-4676-4f24-aa15-082e88d61fea` (2026-05-12, P69-19) — passed
  all gates with 12-line patch; planner emitted no acceptance_tests for
  the map-picker feature

**Current mitigation** (v16.0 — not yet in place at time of writing):
- (planned) empty-acceptance gate: feature-task classifier + domain
  classifier + minimum-contract-count check before codegen

**Architectural fix**:
- Contract Builder authored separately from Planner; planner only maps
  contracts to files. Plan can't enter codegen without at least the
  domain's required_contract_count satisfied. (v16.2+)

**Status**: OPEN → MITIGATED (after v16.1 ships)

---

## C2: `polish_passing_as_feature`

**Definition**: technical gates (compile, evidence_chain, semantic_review)
all pass on a patch that does NOT implement the user's requested feature.
This is the failure mode that emerges when correctness gates are present
but completeness gates are absent. Paired with C1 (under-specified plan)
but distinct: this is the gate-side failure, C1 is the planner-side
failure. C1 fixed alone doesn't fix C2 — you can still pass technical
gates on a non-implementation if the plan has acceptance_tests but the
gates don't *enforce* them.

**Recognized by**:
- AWAITING_APPROVAL reached but `diff_shape_check.added` is small AND
  the change_summary describes a substantive feature
- Manual diff inspection shows boilerplate / polish / import-only
  changes
- `acceptance_check.evaluate` either didn't run or ran on a permissive
  pattern set

**Seen in**:
- `b5d0a085-4676-4f24-aa15-082e88d61fea` (2026-05-12) — same run as C1;
  acceptance_check didn't run because the plan had no tests for it to
  run against

**Current mitigation**: none yet.

**Architectural fix**:
- Contract Coverage Output from codegen: model returns
  `implemented_contracts` + `unimplemented_contracts`. Harness verifies
  the declaration against diff content. Non-empty `unimplemented` blocks
  approval. (v16.4+)

**Status**: OPEN

---

## C3: `repair_intent_drop`

**Definition**: compile_repair regenerates a whole section to make
compile pass, dropping intentional functionality along with the bad
line. The post-repair file compiles but the feature added by the
initial codegen is gone. Distinct from C1/C2 because the plan WAS
sufficient and codegen DID implement it; repair then erased it.

**Recognized by**:
- `compile_repair.intent_dropped` event with `protected_symbols_dropped`
  non-empty OR `intent_preservation_ratio < threshold`
- Initial codegen had `+MapView`, `+showMap`, etc.; final merged diff
  has zero occurrences of those identifiers in added lines
- acceptance_check.evaluate fails on patterns that DID match initial
  codegen but no longer match the repair-modified file

**Seen in**:
- `a34a94b5-12eb-4f6f-bf15-dd63868aea94` (2026-05-12) — initial codegen
  wrote correct OSMDroid map UI; one bad `setOnMapClickListener` call
  triggered compile_repair; repair regenerated whole map block with 0
  MapView/showMap/Geocoder references

**Current mitigation** (v16.0 — shipped 2026-05-12):
- F1: `PROTECTED SYMBOLS:` block in repair prompt
- F2: symbol-level invariant gate parallel to line-ratio gate
- OSMDroid YAML card with `methods_NOT_provided` + `replacement_pattern`
  so the bad API call shouldn't happen in the first place

**Architectural fix**:
- ChangeIntent Ledger records protected_symbols per contract per file;
  repair reads ledger and CANNOT propose a patch that drops a protected
  symbol without explicit `repair_escalation_request`. (v16.3+)
- Diagnostic-scoped JSON repair edits: model loses the authority to
  regenerate a whole section; only constrained edit operations within
  a propagation window. (v16.4+)

**Status**: MITIGATED

---

## C4: `library_api_hallucination`

**Definition**: model invents a method/class that doesn't exist on a
real library. Common: applying Google Maps API shape to OSMDroid
MapView (`setOnMapClickListener`, `getMap`, `addMarker`). Compile
correctly rejects the bad call; the secondary harm is that
compile_repair then often resolves it by deletion rather than
correction (chains into C3).

**Recognized by**:
- `compile_failed.errors[*].error == "Unresolved reference 'X'"` where X
  matches a known forbidden-import surface
- Forbidden-import prefix appears in initial codegen + (now obsolete)
  fallback to compile error

**Seen in**:
- `a34a94b5-12eb-4f6f-bf15-dd63868aea94` (`setOnMapClickListener` on
  OSMDroid MapView)
- `67dbf533` (earlier 2026-05-11) had Google Maps SDK imports

**Current mitigation** (shipped 2026-05-12):
- Structured OSMDroid YAML library card with
  `classes.methods_NOT_provided` + `replacement_pattern` +
  `conflicts_with.forbidden_import_prefixes`. Injected into both initial
  codegen prompt and compile_repair prompt.
- (Pre-existing) demo hint `_PROJECT_LIBRARY_CONSTRAINTS["handymanapp"]`
  as fallback for projects without a card.

**Architectural fix**:
- Auto dependency fingerprint (build.gradle / package.json /
  requirements.txt scan) to replace hand-tagged `project_tags`.
  (v16 P0-1)
- Import-dependency gate using
  `forbidden_import_prefixes_for_project()` as a post-patch fail-fast
  before compile. (v16 P0-3)
- Per-library contract cards for the next 5 most-used libraries
  (Firebase, AndroidX Navigation, Coroutines, Compose, Hilt). (v16.1+)

**Status**: MITIGATED (one card; more coming)

---

## C5: `new_file_patch_malformed`

**Definition**: planner asks codegen to create a brand-new file; model
emits the diff using `--- a/<path>` header (existing-file shape)
instead of `--- /dev/null` (new-file shape). `git apply --check`
rejects with "No such file or directory". Self-validation retries
without targeted guidance produce the same error.

**Recognized by**:
- `ValidationResult.error_kind == "MISSING_NEW_FILE"` (added 2026-05-12)
- `validation.error_detail` contains "No such file or directory" on a
  path that's in `plan.expected_new_files`

**Seen in**:
- `67dbf533-82dd-4b83-b1d4-da40ccf1bd99` (2026-05-11) — batch 5 for
  MapPickerFragment.kt failed after 2 retries with this exact error

**Current mitigation** (shipped 2026-05-12):
- `classify_apply_error(stderr)` classifier in codegen_self_validate.
- Targeted retry prompt: when error_kind == MISSING_NEW_FILE, inject
  `TARGETED FIX:` block with the canonical new-file shape (`new file
  mode 100644 / --- /dev/null / +++ b/<path>`) and the plan's
  `expected_new_files` list.

**Architectural fix**:
- JSON `kind=create` batch outputs: structured `{path, content}` for new
  files, harness constructs the diff. Removes the unreliable raw-diff
  path for new files entirely. (v16.4+)
- Planner-emitted batch_plan with `kind: create | modify | wire` so the
  batcher routes correctly. (v16.2+)

**Status**: MITIGATED

---

## C6: `partial_batch_success_with_plan_conflict`

**Definition**: parallel per-file codegen batches each see the full
plan including `acceptance_tests` targeting other files. A batch scoped
to file A is shown an acceptance_test requiring a pattern in file B,
correctly realizes it cannot satisfy that test, emits
`## PLAN_CONFLICT` and produces no diff. batch_coverage.check then
flags the unpatched file A as `missing_must_touch` and fails the
pipeline.

**Recognized by**:
- `codegen.generate_patch.payload.error` contains
  `codegen_terminal: PLAN_CONFLICT: Hard acceptance requirement N
  requires pattern X in <other_file>, but this codegen call is scoped
  exclusively to <this_file>`
- `batch_coverage.check.payload.kind == "missing_must_touch"` with
  `status: other_failure` and `reason` quoting PLAN_CONFLICT

**Seen in**:
- `24ecfb5c-7760-4a1b-bb73-4751ba310b08` (2026-05-12, P69-19) — batch
  2/4 for CustomerSignup.kt emitted PLAN_CONFLICT over an acceptance
  test targeting CustomerKYCAddressForm.kt

**Current mitigation** (shipped 2026-05-12):
- `_build_prompt` filters `acceptance_tests` by batch scope. Tests with
  a `file` field outside `context_files.keys()` go to an informational
  "HANDLED BY OTHER BATCHES" section that explicitly tells the model NOT
  to emit PLAN_CONFLICT for them. Global tests (no `file`) still appear
  in every batch's HARD REQUIREMENTS.

**Architectural fix**:
- ChangeIntent Ledger ties acceptance_tests to contracts and contracts
  to files; the batcher routes only the contract parts relevant to each
  batch. (v16.3+)

**Status**: MITIGATED

---

## C7: `pipeline_stage_deadlock`

**Definition**: orchestrator coroutine stops emitting events with no
LLM API call in flight (zero HTTPS connections), no CPU growth, and no
visible error. Watchdog eventually kills the task after the stage timer
(30 min). Distinct from "slow LLM call" — the latter has active HTTPS
connections.

**Recognized by**:
- ≥ 10 min silence between events while `status == executing`
- `Get-NetTCPConnection` shows zero established HTTPS connections from
  python.exe
- Backend stdout/err log has no error trace

**Seen in**:
- `7544ee84-d5a9-4b66-b126-8d109ccfd30d` (2026-05-11) — 27 min silent
  after `evidence_pack.build ✓`; 0 HTTPS connections

**Current mitigation**:
- Per-batch codegen deadline (720s, shipped 2026-05-12). Catches the
  case where the stall is inside a codegen batch; doesn't catch stalls
  between batches.
- Backend stdout/err logging (shipped 2026-05-12). Makes diagnosis
  possible on the next occurrence; doesn't prevent.

**Architectural fix**:
- Reproduce under stdout-captured backend; stack trace will reveal
  whether it's an asyncio `await` deadlock, a missing future
  resolution, or a deadlocked DB lock. Then fix at the actual deadlock
  site.
- (Defensive) per-orchestrator-stage soft timeout that surfaces the
  current async task name + traceback to the err log before letting the
  watchdog fire.

**Status**: OPEN (one occurrence, not reproduced; needs next sighting
under captured stdout to diagnose)

---

## C8: `phantom_no_change`

**Definition**: codegen returns `NO_CHANGE_NEEDED` claiming the file is
already correct, but the supporting evidence quote doesn't actually
appear in the file. Distinguished from `NO_CHANGE_NEEDED_VERIFIED`
(quotes match) by the v15 quote_verifier path.

**Recognized by**:
- `codegen_terminal: PHANTOM_NO_CHANGE` in error message
- Codegen returned `## NO_CHANGE_NEEDED` block with evidence quotes that
  failed `quote_verifier.verify_evidence_quotes`

**Seen in**:
- Several v15-era tasks pre-T1 (phantom-no-change was the v15 Ticket 1
  motivation)

**Current mitigation** (shipped v15 — 2026-05-10):
- v15 Ticket 1: quote_verifier with exact + whitespace-normalized match,
  ≥4-char minimum
- v15 Ticket 2A: NO_CHANGE_NEEDED schema enforced (`{reason, evidence:
  [{file_path, claim, quote}]}`)
- v15 Ticket 2B: batch_coverage classifies `phantom_no_change` as a hard
  fail (not eligible for plan_codegen_conflict)

**Architectural fix**: largely done by v15. May need refinement if a
new phantom shape appears (e.g. cherry-picked quotes that match but
mislead).

**Status**: SOLVED (no recurrence since v15 ship)

---

## C9: `payload_field_loss`

**Definition**: patch modifies a write call (Firebase `updateChildren`,
Firestore `set/update`, REST POST body, DTO mapper) and silently drops
a top-level key the original was writing. Symptoms appear only at
runtime when the consumer (DB, downstream service) finds the field
missing. Semantic_review doesn't catch it because the change is
syntactically valid and the missing field isn't referenced in the diff.

**Recognized by**:
- Pre-patch file contains `payload.put("X", ...)` or
  `mapOf("X" to ..., ...)` AND post-patch file does not, AND the
  planner's change_summary doesn't explicitly say "remove X"
- Common offender: `createdAt`, `updatedAt`, `userId`, `metadata`

**Seen in**:
- (User report from v15 retro, not a specific incident-report-tracked
  task) — `createdAt` deleted repeatedly across patches

**Current mitigation**: none.

**Architectural fix**:
- Payload-preservation gate: per-file, compare top-level keys in
  pre-patch vs post-patch payload-shaped expressions. Drop without
  `plan.expected_removed_fields` mention → block. (v16.5+)
- Regret memory: when a payload deletion passes review but causes a
  field-loss regression, write a memory entry tagged
  `payload_field_protected:<class>.<field>` so future codegen on the
  same payload sees the warning.

**Status**: OPEN

---

## C10: `kotlin_structural_codegen_breakage`

**Definition**: codegen emits the right high-level symbols/contracts, and
the patch applies to the correct existing files, but the resulting Kotlin
tree is structurally invalid: try/catch lands outside a valid try block,
Compose/Firebase lambdas are split, braces/parentheses no longer balance,
callback methods fall outside their listener object, or state declarations
land in an invalid scope. This is not a normal missing-symbol issue.

**Recognized by**:
- Kotlin compiler parser/scope messages such as `Expecting ')'`,
  `Unexpected tokens`, `Expecting an element`
- `Unresolved reference 'catch'` / `Unresolved reference 'e'` paired with
  parser expectation errors
- many cascading errors in one Kotlin file after an otherwise scope-correct
  codegen attempt

**Seen in**:
- `72eb9545-6235-4f26-a47c-ba194bd65c2b` (Round11f, 2026-05-13,
  P69-19) — planner was clean, no fake files, contract coverage passed,
  but codegen inserted Kotlin/Compose/Firebase code into invalid structure.
  compile_repair intent protection prevented deletion-based fixes, but the
  old free-form repair path could not reliably repair the structure.

**Current mitigation** (2026-05-14):
- `compile_error_classifier` classifies Kotlin parser/scope explosions as
  `kotlin_structural_breakage` before generic `unresolved_reference`.
- C10 compile repair first requests structural edit JSON, not raw diff.
- Harness applies structural edits through a small Kotlin locator/applier,
  validates basic structure, preserves protected symbols, then generates the
  final diff.
- Legacy repair remains as fallback when structural edit cannot produce a
  valid scoped patch.

**Architectural fix**:
- Expand diagnostic-scoped JSON repair into the default repair path with a
  Tree-sitter locator for nearest function/block/import regions.
- Later, move initial codegen from raw text patches toward structured edit
  intent where the harness owns placement and diff generation.

**Status**: MITIGATED

---

## Cross-cutting notes

**Why C1 ≠ C2 even though they paired today**: C1 is fixable on the
planner side (force acceptance_tests to be non-empty). C2 is fixable on
the gate side (force gates to actually enforce acceptance_tests).
You need both. A v16.1 that fixes C1 alone leaves C2 silently failing
when a future bug makes acceptance_check skip itself. A v16.2 that fixes
C2 alone is wasted effort if the planner never emits tests in the first
place.

**Why C4 → C3 is a chain**: library hallucination produces a compile
error; the compile error triggers repair; repair tries to fix the error
by deleting the offending line; deletion drops the surrounding feature.
A C4 mitigation (the OSMDroid card) reduces the FREQUENCY of C3 but
doesn't eliminate it — other failure modes can also trigger a repair
that drops intent.

**Why C7 isn't in the contract-first arc**: it's an orchestrator-layer
async bug, not a codegen-layer correctness bug. Contract-first won't
fix it. Needs separate investigation under captured stdout.

---

## Failure-class tag for STAGE_LOG entries

Every new STAGE_LOG entry adds:

```
**Failure classes addressed:** C1, C3
**Failure classes NOT addressed (deferred):** C2
```

When the same class shows up in 2+ entries' `addressed` line, the next
stage's `Trigger:` should reference it: "consolidate C3 — three rounds
of止血 mitigations have stacked; build the structural fix".

---

## Open invariant: every failure has a class

If a new live run surfaces a failure that doesn't fit any of C1-C9, the
correct response is:

1. Stop coding the fix.
2. Add the new class entry to this document first.
3. Then write the fix referencing the new class ID.

This prevents the patch pile.
