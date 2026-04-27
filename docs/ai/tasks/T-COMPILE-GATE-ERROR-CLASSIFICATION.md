# T-COMPILE-GATE-ERROR-CLASSIFICATION — Distinguish target-file syntax errors from external/sandbox config errors

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

**Status:** todo (P1)
**Priority:** P1 (fixes a real misattribution bug; saves repair-loop budget on healthy files)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Make the compile gate distinguish **target-file syntax errors** (e.g. `SyntaxError: Unexpected token in jobData.js:42`) from **external dependency / sandbox-config errors** (e.g. `Invalid package config .../package.json` from `node:internal/modules/package_json_reader`). Only the former should trigger `codegen.repair` of the target file. The latter should fail the gate immediately with a typed error that the repair loop refuses to enter.

## Background

### The bug (observed on P69-7, task `29fe0fc3`, 2026-04-27)

- compile gate ran `node --check src/data/jobData.js`
- Node returned non-zero exit because the sandbox-root `package.json` was malformed JSON — completely unrelated to jobData.js
- The compile gate captured exit code + stderr and reported "jobData.js compile failed"
- T-PIPELINE-REPAIR-CAP did its job and dispatched `codegen.repair` 3 rounds for jobData.js
- Claude Code CLI looked at jobData.js each round and correctly said "the file is syntactically valid"
- 3 rounds wasted, ~3 minutes of LLM time, task transitioned to `awaiting_approval` with a misleading `repair_summary` blaming jobData.js

### Why this matters beyond P69-7

This is a **classification bug pattern**: the gate trusts Node's exit code as a proxy for "the file is broken", but Node fails for many reasons that aren't the file's fault:
- `Invalid package config` (package.json scope walk failed)
- `ERR_MODULE_NOT_FOUND` (transitive import resolution)
- `Cannot find module 'X'` (npm not installed)
- `EACCES` / `EPERM` (sandbox path permission)
- Path encoding errors (non-ASCII path on Windows + older Node)

Repairing the *target file* won't fix any of these. The repair loop will burn budget repairing healthy code while the actual broken state (sandbox / deps / config) goes untreated.

### Dependencies

- T-CODEGEN-PROVIDER-OBSERVABILITY recommended (so we can verify "claude_code refused to modify" matches the gate's misclassification). Not strictly required.
- T-FAILURE-DIAGNOSIS landing alongside is useful (diagnosis can read the typed error and explain it cleanly), but neither blocks the other.

## Design

### A. Typed compile-gate failures

`apps/backend/app/services/compile_gate.py` (or wherever the gate currently lives) returns a structured failure today as `{passed: bool, errors: list[CompileError]}`. Extend `CompileError` with a `kind`:

```python
@dataclass
class CompileError:
    file: str                          # the file path the gate was checking
    line: int | None
    column: int | None
    raw_message: str                   # full stderr line
    # NEW:
    kind: Literal[
        "target_syntax",               # actual syntax error in `file`
        "external_package_config",     # package.json walk / scope error
        "external_module_not_found",   # ERR_MODULE_NOT_FOUND for non-target file
        "external_path_encoding",      # ERR_INVALID_FILE_URL_PATH or similar
        "external_filesystem",         # EACCES / EPERM / ENOENT for non-target
        "sandbox_setup",               # gate command itself couldn't run (node not on PATH, etc.)
        "unknown",                     # didn't match any pattern; treat as untrusted
    ]
    classification_confidence: Literal["high", "medium", "low"]
```

### B. Classifier

New module `apps/backend/app/services/compile_error_classifier.py`:

```python
def classify_compile_error(
    target_file: str,
    raw_stderr: str,
    raw_exit_code: int,
) -> CompileError:
    """
    Pattern-match the stderr output to assign a `kind`.

    Patterns are ordered most-specific-first. The first match wins.
    Patterns include both Node and Python tooling because the gate
    runs against multiple language fixtures.
    """
```

Pattern table (ordered):

| Pattern (regex) | kind | confidence |
|---|---|---|
| `Invalid package config .*[/\\]package\.json` | external_package_config | high |
| `ERR_MODULE_NOT_FOUND.*Cannot find package '[^']+'` | external_module_not_found | high |
| `Error: Cannot find module '[^']+'.*at .*node:internal/modules` | external_module_not_found | high |
| `ERR_INVALID_FILE_URL_PATH` | external_path_encoding | high |
| `EACCES.*permission denied` | external_filesystem | high |
| `'\w+' is not recognized as an internal or external command` | sandbox_setup | high |
| `<target_file>:\d+:\d+: SyntaxError` (target file in message) | target_syntax | high |
| `<target_file>:\d+\b.* SyntaxError` (target file in message) | target_syntax | high |
| `SyntaxError: ` (with no file or different file) | unknown | low |
| (no pattern matches) | unknown | low |

`<target_file>` is the file the gate was checking; if stderr mentions a *different* file, classification is `unknown` low (don't blame the target).

### C. Repair-loop trust filter

`apps/backend/app/orchestrator/service.py::_run_compile_repair_loop`:

```python
# Before dispatching codegen.repair:
non_target_failures = [e for e in compile_errors if e.kind != "target_syntax"]
if non_target_failures and all(e.kind != "target_syntax" for e in compile_errors):
    # No target-syntax errors → repair loop CANNOT fix this. Skip the rounds.
    record_event(
        ...
        event_type=REVIEW_FAILED,
        message=(
            f"Compile gate failed but no target-file syntax errors detected. "
            f"Failure kinds: {[e.kind for e in compile_errors]}. "
            "Skipping repair loop; this requires sandbox / dependency intervention."
        ),
    )
    transition_to_awaiting_approval(reason="compile_gate_external_failure")
    return
```

Mixed case (some `target_syntax` + some external): repair loop runs, but only on files with `target_syntax` errors. External errors recorded for the diagnosis step but not retried.

### D. Approval payload

When transitioning to `awaiting_approval` with `compile_gate_external_failure`, the `repair_summary` includes the typed kind, so T-FAILURE-DIAGNOSIS can produce a much sharper root-cause statement:

> "Compile failed because the sandbox `package.json` is malformed JSON (line 8). The target files (jobData.js, …) are syntactically valid; no LLM repair will fix this. Suggested fix: validate the source repo's package.json before clone, or copy a known-good package.json into the sandbox manually before granting approval."

### E. Configuration

```python
compile_gate_skip_repair_on_external_failure: bool = True
compile_gate_classifier_log_unknowns: bool = True  # log every "unknown" classification with full stderr for pattern-table improvement
```

## Files to create

1. `apps/backend/app/services/compile_error_classifier.py` — classifier module + `classify_compile_error()`.
2. `apps/backend/tests/services/test_compile_error_classifier.py` — pattern-matching unit tests.
3. `apps/backend/tests/orchestrator/test_compile_repair_loop_external_failure.py` — repair-loop trust-filter integration tests.

## Files to edit

1. `apps/backend/app/services/compile_gate.py` (or current path) — extend `CompileError` with `kind` + `classification_confidence`. Wire classifier into the result-building path.
2. `apps/backend/app/orchestrator/service.py::_run_compile_repair_loop` — trust filter logic from section C.
3. `apps/backend/app/core/config.py` — add the 2 new settings.

## Tests

### Classifier unit tests (test_compile_error_classifier.py)

1. `test_classify_invalid_package_config` — stderr containing `Invalid package config D:\项目\.../package.json` → kind=external_package_config, confidence=high. (This is the P69-7 reproduction.)
2. `test_classify_err_module_not_found` — `ERR_MODULE_NOT_FOUND ... Cannot find package 'foo'` → external_module_not_found.
3. `test_classify_target_syntax_with_filename_match` — `src/data/jobData.js:42:5: SyntaxError: Unexpected token` → target_syntax (target_file == jobData.js path) confidence=high.
4. `test_classify_syntax_error_in_different_file` — target=jobData.js, stderr says `someOtherFile.js:1: SyntaxError`. Result: kind=unknown, confidence=low.
5. `test_classify_eacces_permission_denied` → external_filesystem.
6. `test_classify_node_not_on_path` — `'node' is not recognized as an internal or external command` → sandbox_setup.
7. `test_classify_completely_unrecognized_stderr` → unknown, low.
8. `test_classify_empty_stderr_with_nonzero_exit` — exit_code=2, stderr empty → unknown, low (don't fabricate).
9. `test_classifier_pattern_order` — stderr containing both `package.json` AND a syntax error mention; first matching pattern wins (external_package_config); test the order is stable.
10. `test_classifier_handles_non_ascii_paths` — stderr with `D:\项目\` paths → still matches package_config pattern correctly (regex doesn't break on UTF-8).

### Orchestrator repair-loop integration tests (test_compile_repair_loop_external_failure.py)

11. `test_repair_loop_skipped_when_all_failures_external` — fixture compile gate result has 1 error of kind=external_package_config. Repair loop is NOT entered; task transitions to AWAITING_APPROVAL with reason=compile_gate_external_failure.
12. `test_repair_loop_runs_only_on_target_syntax_files` — fixture has 2 errors: kind=target_syntax for fileA, kind=external_module_not_found for fileB. Repair loop dispatches codegen.repair for fileA only.
13. `test_repair_loop_normal_when_only_target_syntax` — fixture has 3 errors all kind=target_syntax. Existing T-PIPELINE-REPAIR-CAP behavior preserved (3 rounds attempted).
14. `test_external_failure_event_payload_records_kinds` — when repair loop is skipped, the `REVIEW_FAILED` event payload includes the `kinds` list and the raw stderr summary. (Diagnosis step reads this.)
15. `test_repair_skip_setting_can_disable_filter` — set `compile_gate_skip_repair_on_external_failure=False` → repair loop runs even on external failures (T-PIPELINE-REPAIR-CAP behavior, for backwards compatibility).

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 15 new tests pass.
- Full suite still green.
- Manual verification: corrupt sandbox `package.json` (P69-7 reproduction), trigger a develop pipeline. Assert:
  - Compile gate fails with `kind="external_package_config"` recorded
  - `_run_compile_repair_loop` does NOT enter (no `compile_repair.round_started` events)
  - Task transitions to `AWAITING_APPROVAL` with message mentioning "external" / "package.json" / "no target-file syntax errors"
  - `repair_summary` payload contains `kinds=["external_package_config"]`
- Backend logs include classifier `unknown` cases with full stderr (for future pattern-table improvement).

## Out of scope (explicitly NOT in this card)

- **Auto-fixing** the external failure (e.g. "if package.json is broken, automatically restore from git HEAD"). This is read-only classification; the repair-loop refuses to spin and humans decide.
- Sandbox pre-flight validation (that's `T-SANDBOX-PREFLIGHT`).
- Adding new compile gate languages (e.g. Rust, Go). Current scope: JS/TS via Node, Python via py_compile.
- Replacing `node --check` with a more robust syntax checker (e.g. esbuild). Tracked separately.

## Workflow (for the executor)

<!-- Effort: medium -->

1. Read current `apps/backend/app/services/compile_gate.py` (or grep for "CompileError" / "node --check" to find it).
2. Read `_run_compile_repair_loop` in `orchestrator/service.py` to understand the integration point.
3. Implement `compile_error_classifier.py` with the pattern table from section B. Patterns must work on raw stderr — including Windows `\` paths and non-ASCII chars.
4. Extend `CompileError` dataclass; wire classifier into compile gate result-building.
5. Add the trust filter in `_run_compile_repair_loop`. Three branches: all external, mixed, all target_syntax.
6. Write classifier unit tests (test 1-10).
7. Write orchestrator integration tests (test 11-15).
8. Add config settings.
9. Run `python -m compileall app` and unit suite.
10. Manually run the P69-7 reproduction (intentionally corrupt sandbox package.json), verify the loop is skipped.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-COMPILE-GATE-ERROR-CLASSIFICATION.md
```
