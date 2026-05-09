# DeepSeek Agent Harness V1

Status: drafted 2026-05-09, in implementation on `feat/harness-v1` branch.

## Why

Today's SWE-bench-Lite run produced a 0/4 pass rate with our default
configuration (DeepSeek-V4-Pro codegen + dump-everything context). The
two failure modes were:

1. LLM emits non-diff output ("LLM response does not contain valid diff
   after 3 attempts") — happens at ~110-140k bytes of injected file
   context.
2. LLM emits a diff but its context lines drift from source ("git apply
   --check failed: hunk drift / context mismatch") — happens on slightly
   smaller injections (~93-100k bytes).

Both root-cause to the same problem: **DeepSeek's reliable codegen
budget is roughly 30k tokens; our pipeline routinely drives it past 40k
before any intelligent reasoning happens.** Switching codegen to
`claude_code` masks the problem (Claude tolerates more context) but
isn't a structural fix — the next hard task or larger repo blows up
again.

Claude Code CLI / Codex CLI succeed because they ship as **agentic
harnesses**: file search, scoped reading, plan-then-edit, tool feedback,
multi-round repair. When we use DeepSeek directly via API, we get only
the *model*, none of the harness. The fix is to externalize those
capabilities into our orchestrator so DeepSeek can be a constrained
patch worker rather than the primary agent.

## Non-goals

- Replacing claude_code / codex CLI paths. They stay as one of the
  available codegen providers; this work makes DeepSeek a viable peer.
- Cross-model voting / cross-validation (this is the deferred E
  recommendation, only justified for production high-risk paths).
- Self-grading via an extra LLM critique call (deferred; LLM
  self-confidence is poorly calibrated and we have free
  alternative signals).

## Scope summary

| Tier | Items | LLM-call delta | Status |
|------|-------|----------------|--------|
| 1 | AGENTS.md + playbook router; patch budget gate; acceptance tests in plan; evidence pack hard cap; Aider search/replace format | 0 (some paths *reduce* calls due to fewer retries) | implementation |
| 2 | Categorical context budgeter; multi-stage codegen (plan → per-file → merge); symbol/import graph (tree-sitter); confidence proxy from free signals | 0 | next |
| 3 | Layered RAG + summary tree; failed-pattern memory across tasks | 0 | next |
| 4 | Lightweight tool-use loop for codegen (read_file / search_symbol / list_directory) | +N per turn (bounded), only fires when codegen requests | later |

E (cross-model) and C (LLM self-grading) are deliberately out.

## Tier 1 — design

### 1.1 AGENTS.md + `docs/playbooks/`

**Problem.** Every task prompt today re-states project conventions
inline. That's wasteful and inconsistent.

**Solution.** A small static layer the planner / codegen can opt into:

```
AGENTS.md                                 # 1-2k tokens
docs/playbooks/
  codegen-rules.md                        # 2k tokens
  python-django-orm.md                    # 1.5k tokens
  python-astropy-nddata.md                # 1.5k tokens
  android-compose-navigation.md           # 1.5k tokens
  firebase-rtdb-fallback.md               # 1k tokens
docs/agent-docs-index.md                  # routing table
```

`docs/agent-docs-index.md` maps task signals (scenario, repo, anchor
keywords) to playbook paths. The planner consults it and includes the
matched playbooks in the evidence pack.

**Acceptance.**
- AGENTS.md exists at repo root and is non-empty.
- At least 3 playbooks shipped under `docs/playbooks/`.
- A `docs_router` module exposes `route(task) → list[Path]` and is
  unit-tested for at least one Python (django) and one general
  scenario.
- Planner prompt includes the routed playbooks under a `## Playbook
  rules` heading; the orchestrator passes the routed list through to
  codegen as part of its context pack.

### 1.2 PatchBudget gate

**Problem.** DeepSeek occasionally produces runaway patches (rewrites
unrelated files, balloons line count). Today this only gets caught at
review by `feature_presence_check`, after wasting a compile cycle.

**Solution.** A pre-apply structural budget:

```python
@dataclass(frozen=True)
class PatchBudget:
    max_files_changed: int = 8
    max_added_lines: int = 300
    max_removed_lines: int = 200
    max_new_imports_per_file: int = 5
    max_new_files: int = 2
    max_function_signatures_changed: int = 3

@dataclass(frozen=True)
class PatchBudgetReport:
    passed: bool
    violations: list[str]           # human-readable
    metrics: dict[str, int]         # observed counts
```

`evaluate_patch_budget(diff: str, budget: PatchBudget) → PatchBudgetReport`
is called between codegen and `sandbox.apply_patch`. On violation, the
orchestrator records `tool_failed` with the violation list and either
asks codegen for a focused retry or fails the task with a clear
explanation.

Per-task overrides: planner may request a higher budget by including
`patch_budget_override` in its plan output (must be justified in the
plan rationale). Reviewer enforces the rationale.

**Acceptance.**
- `app/services/patch_budget.py` with `PatchBudget`,
  `PatchBudgetReport`, `evaluate_patch_budget(diff, budget)`.
- 8+ unit tests covering each violation path + happy paths.
- Orchestrator wires the gate between codegen and apply_patch with a
  `patch_budget_failed` event type.
- 0 LLM calls.

### 1.3 acceptance_tests in plan output

**Problem.** `feature_presence_check` is token-level
("must_touch file contains spec word"). It's defeated by a diff that
adds the word in a comment.

**Solution.** Planner emits structured acceptance criteria as part of
the plan. Reviewer uses these as a stronger gate.

```json
{
  "plan_steps": [...],
  "must_touch_files": [...],
  "acceptance_tests": [
    {
      "kind": "diff_contains_pattern",
      "pattern": "if mask is None",
      "scope": "astropy/nddata/mixins/ndarithmetic.py",
      "rationale": "operand-without-mask branch must be implemented"
    },
    {
      "kind": "function_signature_unchanged",
      "function": "NDArithmeticMixin._arithmetic",
      "rationale": "external API contract"
    },
    {
      "kind": "no_new_file_outside",
      "scope": "astropy/nddata/",
      "rationale": "scope guard"
    }
  ]
}
```

Six initial `kind` values:
1. `diff_contains_pattern` — substring or regex must appear in added
   lines (not just any line).
2. `diff_contains_pattern_in_file` — same, scoped to a specific file.
3. `function_signature_unchanged` — named function's signature line
   must not change.
4. `function_signature_changed` — named function's signature MUST
   change (rare; for refactor tasks).
5. `no_new_file_outside` — no new files outside the named directory.
6. `import_added` — a specific import must appear somewhere in added
   lines.

**Acceptance.**
- `app/schemas/plan.py` extended with `acceptance_tests:
  list[AcceptanceTest]`.
- `app/services/acceptance_check.py` with `evaluate_acceptance(diff,
  tests) → AcceptanceReport`.
- 12+ unit tests (2 per kind).
- Planner prompt updated to ask for acceptance_tests; absence is
  permissive (warning in event log) for the first iteration so we can
  ship without forcing every legacy task to regenerate plans.
- Reviewer wires the gate between feature_presence and final approval.
- 0 LLM calls (the planner already runs).

### 1.4 Evidence pack hard cap

**Problem.** Today `evidence_bundle.build_evidence_bundle` collects up
to 20 must_touch files at unbounded size and we inject them all. Real
observed payloads: 93-140k bytes.

**Solution.** Bounded, relevance-ranked evidence pack with explicit
budget:

```python
@dataclass(frozen=True)
class EvidencePackBudget:
    max_files: int = 6              # was 20
    max_total_bytes: int = 18_000   # was unbounded
    max_per_file_bytes: int = 6_000

@dataclass(frozen=True)
class EvidencePack:
    primary_file: Path | None        # the one we expect to edit most
    must_touch_files: list[FileEvidence]
    related_symbols: list[SymbolHit]
    constraints: list[str]
    failure_memory: list[str]
    dropped: list[Path]              # files we couldn't fit, with reason
```

Ranking, in order of priority for inclusion:
1. The plan's `primary_file` (always included if it fits).
2. Files explicitly named in the plan steps as edit targets.
3. Files that contain functions referenced by the plan steps.
4. Files containing symbols mentioned in the request_text.
5. Files containing the most-distinctive anchor tokens (FTS5 score).

Truncation, when individual files exceed `max_per_file_bytes`:
1. Keep the function bodies that contain plan-mentioned symbols.
2. Keep imports.
3. Drop docstrings and comments.
4. If still over, keep only the first N lines + a `... (truncated)`
   marker.

**Acceptance.**
- `app/services/evidence_pack.py` (new module wrapping or replacing
  parts of `evidence_bundle.py`).
- 10+ unit tests on ranking + truncation paths.
- Pipeline events `evidence_pack_built` recording bytes_used,
  files_included, files_dropped.
- Default budgets configurable via `OPS_AGENT_EVIDENCE_PACK_*` env
  vars.
- Old code paths kept available as `legacy_full_dump` mode behind a
  flag so we can A/B against the new path during validation runs.
- 0 LLM calls.

### 1.5 Aider search/replace format

**Problem.** Two of our four DeepSeek failures were "valid diff produced
but hunk drift" — context lines didn't match source character-for-
character. Unified diff format is unforgiving on that. The Aider project
has empirical data showing search/replace blocks improve mid-tier model
pass rates by 15-25 points on coding benchmarks.

**Solution.** Add an alternative codegen output format that DeepSeek
emits as a sequence of file-scoped replace blocks:

```
filename.py
<<<<<<< SEARCH
def old_function(x):
    return x + 1
=======
def new_function(x, y=0):
    return x + y
>>>>>>> REPLACE
```

Multiple blocks per file allowed. SEARCH must be unique within the
file (we error if it's ambiguous). Empty SEARCH means insert (with a
required anchor specifier line above). Empty REPLACE means delete.
Special header `### NEW FILE: <path>` with empty SEARCH means create.

**Apply algorithm.**
1. Parse the LLM output into `[(file, search_block, replace_block)]`.
2. For each tuple: read the current file from sandbox, count occurrences
   of `search_block`. If exactly 1, replace. If 0, fail with "anchor
   not found". If ≥2, fail with "anchor ambiguous".
3. Write the file back.
4. Convert all changes into a unified diff via `difflib.unified_diff`
   for downstream consumers (sandbox.apply_patch, SWE-bench
   predictions.jsonl).

**Why this format vs JSON patch.**
- Reads as prose (closer to the human-rewrite examples models trained
  on); no schema knowledge needed.
- Anchor IS the SEARCH block; no separate `anchor` / `operation` field
  to misuse.
- Whole-block replacement is the natural case (just SEARCH + REPLACE)
  whereas JSON patch needs a delete + insert pair.
- Trivially convertible to unified diff at the boundary.

**Output mode selection.**
- Per-provider config `OPS_AGENT_CODEGEN_OUTPUT_FORMAT` with values
  `unified_diff` (default for claude_code, codex), `aider_blocks`
  (default for deepseek, openai-gpt-4o), `auto` (provider-default
  lookup table).
- Codegen prompt is templated per format.

**Acceptance.**
- `app/services/aider_format.py` with `parse_aider_blocks(text)`,
  `apply_aider_blocks(blocks, sandbox_dir)`, and
  `aider_blocks_to_unified_diff(blocks, sandbox_dir)`.
- 15+ unit tests covering: parser happy path, parser malformed input,
  ambiguous anchor, anchor not found, empty SEARCH (insert), empty
  REPLACE (delete), new file marker, mixed-format file.
- Codegen integrated: when format is `aider_blocks`, the prompt is
  rewritten and the parser+apply pipeline runs in place of the unified
  diff path.
- The unified-diff conversion is verified to produce a `git apply
  --check`-clean diff.
- 0 LLM calls.

## Tier 1 ordering and dispatch

| # | Item | Effort | Owner |
|---|------|--------|-------|
| 1 | AGENTS.md + 3 playbooks + docs_router | 1-2 h | Claude (this session) |
| 2 | PatchBudget + tests | 1-2 h | Claude (this session) |
| 3 | acceptance_tests + tests | 2 h | Claude (this session) |
| 4 | Evidence pack hard cap + tests | 2-3 h | Claude (this session) |
| 5 | Aider format + tests | 3-4 h | DeepSeek dispatch |
| 6 | Tier 1 validation: 4-task SWE-bench rerun | 1 h | Claude |

After (6) we have a real number to compare against the 0% baseline
before moving to Tier 2.

## Tier 2 — sketch (next session)

- **Categorical context budgeter** sitting on top of Evidence pack so
  every budget category (system / playbook / spec / evidence / memory
  / output) has its own cap and a documented drop priority.
- **Multi-stage codegen**: plan call → per-file edit calls in parallel
  with file-local context only → trivial textual merge. Reuses Aider
  blocks so each per-file call's output is small and structured.
- **Symbol graph** built with tree-sitter (already in deps): exposes
  `find_definition`, `find_callers`, `imports_of` to the planner so
  must_inspect can be precise instead of LLM-guessed.
- **Confidence proxy** synthesised from free signals (diff_len, anchor
  hit rate, retry count, feature_presence pass) instead of an LLM
  self-grade call. Used to decide whether to re-route to a stronger
  model on weak signal.

## Tier 3 — sketch (next session)

- **Layered RAG + summary tree**: every indexed file gets a 1-2k
  structural summary card (purpose / key symbols / links). Retrieval
  returns cards first, then full chunks on demand.
- **Failed-pattern memory**: cross-task store of "patches that looked
  right but failed compile / acceptance". Planner consults it to avoid
  known-bad shapes.

## Tier 4 — sketch (later)

- **Lightweight tool-use loop for codegen**: codegen LLM gets
  read-only `read_file`, `search_symbol`, `list_directory` tools so
  it can pull additional evidence on demand instead of relying on a
  one-shot context pack. Bounded to 5 tool calls per attempt.
  This is the structural feature that separates Codex from a
  one-shot diff generator.

## Out of plan

- C — LLM self-grading on every patch. Free signals (diff_len, anchor
  hits, retry count) provide most of the information at zero cost.
- E — Cross-model verification (DeepSeek + Claude in parallel). Only
  triggered for production high-risk merges, not benchmarks.

## Validation strategy

Each Tier closure re-runs the same 4-task SWE-bench-Lite subset
(`astropy-14995`, `django-11283`, `django-11797`, `django-12284`) with
DeepSeek as codegen. Numbers tracked:

- Pass rate (terminal status not in {failed, stale_failed, error})
- Diff length distribution (zero-diff is the smoking gun for context
  overflow)
- Tool failures by type (`feature_presence_failed`, `compile_failed`,
  `patch_budget_failed`, `aider_anchor_not_found`)
- Wall clock per task

Tier 1 target: ≥ 1/4 producing a valid diff that passes Stage A
self-validation. Aspirational: 2/4.

Tier 2 target: ≥ 2/4 reaching `awaiting_approval` (i.e. all gates pass).

Tier 3 target: ≥ 2/4 patches that the SWE-bench Docker evaluator
scores as resolved.

Final target with full harness: 30-40% on the full 50-task subset,
matching public Aider+DeepSeek-V3 numbers.
