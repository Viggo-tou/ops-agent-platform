# T-SANDBOX-PREFLIGHT — Validate critical sandbox state before pipeline starts

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

**Status:** todo (P2)
**Priority:** P2 (defense in depth; T-COMPILE-GATE-ERROR-CLASSIFICATION addresses the same root issue from a different angle)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Add a sandbox pre-flight stage that validates critical config files (e.g. `package.json`, `tsconfig.json`, `pyproject.toml`) immediately after sandbox clone. If any are malformed (JSON parse error, schema mismatch), fail the pipeline with a clear typed error BEFORE any LLM tool runs — avoiding wasted LLM budget on a sandbox that was never going to succeed.

## Background

### The class of bugs this prevents

P69-7 (task `29fe0fc3`, 2026-04-27) wasted 3 compile-repair rounds + 3 minutes of LLM time because the sandbox-root `package.json` was malformed. The compile gate eventually surfaced the issue (Node `Invalid package config`), but only after the orchestrator had already burned through codegen + repair loop. T-COMPILE-GATE-ERROR-CLASSIFICATION fixes that gate's mis-blame. But the cleaner architecture is: don't even ENTER the codegen pipeline if the sandbox is broken on arrival.

### What "broken sandbox" looks like in practice

Observed and likely future cases:
- `package.json` invalid JSON (P69-7's actual cause)
- `tsconfig.json` invalid JSON
- `pyproject.toml` invalid TOML
- Missing `node_modules` when source tree expects them
- Path encoding issues (non-ASCII path on Windows + tools that don't handle it)
- `.git` corrupted / missing (after a partial clone failure)

### Dependencies

- T-COMPILE-GATE-ERROR-CLASSIFICATION should land first (gate's typed errors give us the patterns to validate against).
- T-FAILURE-DIAGNOSIS benefits from this (preflight failures get clean reasons).
- Otherwise independent.

## Design

### A. Preflight stage

After sandbox clone, before plan execution, run:

```python
# apps/backend/app/services/sandbox_preflight.py

@dataclass
class PreflightFinding:
    kind: Literal[
        "package_json_invalid",
        "tsconfig_invalid",
        "pyproject_invalid",
        "git_corrupted",
        "path_encoding_risk",
        "missing_critical_file",
    ]
    severity: Literal["block", "warn"]
    file: str | None
    detail: str
    likely_fix: str  # short hint for the human

@dataclass
class PreflightResult:
    passed: bool
    findings: list[PreflightFinding]


def run_preflight(sandbox_dir: Path, settings: Settings) -> PreflightResult:
    """
    Run all preflight checks. Block findings cause the pipeline to abort
    with a typed error. Warn findings are recorded but don't block.
    """
```

### B. Checks (initial set)

| Check | Severity | What it does |
|---|---|---|
| `package_json_valid` | block | Walk sandbox tree, find every `package.json`, attempt `json.loads`. Any parse error → block. |
| `tsconfig_valid` | block | Same for every `tsconfig.json`. |
| `pyproject_valid` | block | Same for every `pyproject.toml`, using `tomllib`. |
| `git_repo_intact` | warn | If `.git/` exists, run `git status` from sandbox root; non-zero exit → warn (some pipelines don't need git). |
| `path_encoding_risk` | warn | If sandbox absolute path contains any non-ASCII character on Windows, emit warn. (Windows + non-ASCII has bitten us repeatedly.) |
| `node_modules_present_when_expected` | warn | If `package.json` declares dependencies AND `node_modules/` is absent → warn. (Some workflows install on first run, so warn not block.) |

### C. Where it runs

`apps/backend/app/orchestrator/service.py`: after the sandbox clone completes (where exactly depends on current code; likely in `_create_sandbox_for_task` or equivalent), before any tool dispatch. If any block-severity finding, transition to `FAILED` with a typed message, do NOT enter the develop pipeline.

Event recorded: `SANDBOX_PREFLIGHT_FAILED` with full findings list as payload. `T-FAILURE-DIAGNOSIS` reads this and produces a human summary.

### D. Configuration

```python
sandbox_preflight_enabled: bool = True
sandbox_preflight_block_on_warn: bool = False  # if True, warn findings also block
```

Env override:
```
OPS_AGENT_SANDBOX_PREFLIGHT_ENABLED=true
```

When disabled, pipeline behaves as today (sandbox bugs surface deeper in the pipeline).

## Files to create

1. `apps/backend/app/services/sandbox_preflight.py` — module + checks.
2. `apps/backend/tests/services/test_sandbox_preflight.py` — unit tests for each check + aggregator.
3. `apps/backend/tests/orchestrator/test_orchestrator_preflight_integration.py` — orchestrator integration tests.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — wire preflight call after sandbox clone, before pipeline entry. Handle block-severity result by transitioning to FAILED with typed payload.
2. `apps/backend/app/core/config.py` — add the 2 new settings.
3. `apps/backend/app/events.py` — add `EventType.SANDBOX_PREFLIGHT_FAILED`.

## Tests

### Unit tests (test_sandbox_preflight.py)

1. `test_package_json_valid_passes` — sandbox dir with valid `package.json` → no findings.
2. `test_package_json_invalid_blocks` — sandbox dir with malformed `package.json` (P69-7 reproduction: missing comma at line 8) → finding kind=package_json_invalid, severity=block. `detail` contains "Expecting ',' delimiter: line 8".
3. `test_multiple_package_jsons_all_checked` — sandbox has root `package.json` (valid) + nested `subdir/package.json` (invalid) → finding for the nested one only.
4. `test_tsconfig_invalid_blocks` — sandbox has malformed `tsconfig.json` → finding kind=tsconfig_invalid, severity=block.
5. `test_pyproject_invalid_blocks` — malformed `pyproject.toml` → finding kind=pyproject_invalid, severity=block.
6. `test_git_status_failure_warns` — sandbox has `.git/` but git is not on PATH (mock) → warn, not block.
7. `test_no_git_no_finding` — sandbox without `.git/` → no finding (not all sandboxes are git).
8. `test_non_ascii_path_warns_on_windows` — sandbox path contains `项目` AND running on Windows → finding kind=path_encoding_risk, severity=warn.
9. `test_non_ascii_path_no_finding_on_linux` — same path on non-Windows → no finding.
10. `test_node_modules_absent_with_deps_warns` — `package.json` declares dependencies AND no `node_modules/` → finding kind=missing_critical_file, severity=warn.
11. `test_run_preflight_aggregates_findings` — 2 valid, 1 invalid file → result.passed = False, len(findings) == 1.
12. `test_run_preflight_passed_when_no_blocks` — only warn-severity findings → result.passed = True (warns don't block by default).
13. `test_block_on_warn_setting` — `sandbox_preflight_block_on_warn=True` + warn finding → result.passed = False.

### Integration tests (test_orchestrator_preflight_integration.py)

14. `test_orchestrator_aborts_on_block_finding` — fixture orchestrator + sandbox with malformed package.json. Verify `SANDBOX_PREFLIGHT_FAILED` event recorded; task transitions to `FAILED`; NO codegen tool calls happen.
15. `test_orchestrator_continues_on_warn_only` — fixture sandbox with warn-only findings. Verify warning event recorded but pipeline proceeds normally.
16. `test_preflight_disabled_setting_skips_check` — `sandbox_preflight_enabled=False` + corrupted package.json → preflight skipped, pipeline runs (existing pre-T-SANDBOX-PREFLIGHT behavior).

## Acceptance criteria

- `python -m compileall app` exits 0.
- All 16 new tests pass.
- Full suite still green.
- Manual verification: corrupt sandbox `package.json` (P69-7 reproduction). Trigger develop pipeline. Assert:
  - `SANDBOX_PREFLIGHT_FAILED` event in DB
  - Task status = `FAILED`
  - NO `codegen.generate_patch` events (LLM was not called)
  - Latest result JSON contains the typed findings list
- Disable via env var: `OPS_AGENT_SANDBOX_PREFLIGHT_ENABLED=false` → pipeline runs as before.

## Out of scope (explicitly NOT in this card)

- Auto-repairing malformed config files (e.g. "delete and re-clone from source"). Read-only validation; humans decide.
- Validating config file *schema* (e.g. is the package.json `dependencies` object well-typed). Just JSON / TOML parseability for now.
- Pre-running `npm install` to populate node_modules. Out of scope; warn only.
- Cross-platform path encoding fixes. Just warn for now.

## Workflow (for the executor)

<!-- Effort: low -->

1. Read current sandbox setup code in `apps/backend/app/orchestrator/service.py` (search for sandbox path construction / clone).
2. Implement `sandbox_preflight.py` with the 6 checks from section B. Each check is a small function returning `PreflightFinding | None`.
3. Wire `run_preflight()` into orchestrator after clone, before tool dispatch.
4. Write all 13 unit tests + 3 integration tests with fixture sandboxes.
5. Run `python -m compileall app` + unit suite.
6. Manual verification with the P69-7 reproduction.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-SANDBOX-PREFLIGHT.md
```
