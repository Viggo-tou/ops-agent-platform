# T-E1 — DiffReviewer Service

## Goal

Add a `DiffReviewer` service that inspects a diff (plus optional test results and task description) against configurable rules and returns a structured pass/block verdict. Wire it into the tool registry and gateway so the orchestrator can invoke it between the test pipeline and the approval gate.

## Background

Phase E of the multi-agent MVP roadmap. Depends on T-C2 (sandbox apply_patch) and T-D1 (test pipeline). The reviewer is the last automated quality gate before a human approval is requested.

## Design

### ReviewRule protocol

Each rule is a callable that receives a `ReviewContext` and returns an optional `ReviewViolation`. Rules are registered by name so they can be enabled/disabled per project or policy.

### Data classes

New file: `apps/backend/app/services/reviewer.py`

```python
@dataclass
class ReviewContext:
    diff: str                           # unified diff text
    test_result: dict | None = None     # TestRunResult as dict (optional)
    task_description: str = ""          # original request / Jira summary
    changed_files: list[str] = field(default_factory=list)  # parsed from diff

@dataclass
class ReviewViolation:
    rule_name: str
    severity: str   # "block" | "warn"
    message: str

@dataclass
class ReviewResult:
    verdict: str                        # "pass" | "block"
    violations: list[ReviewViolation]
    rules_checked: int
    duration_ms: int
```

### Built-in rules (rule-based, no LLM)

1. **`tests-must-pass`** — If `test_result` is provided, `overall_passed` must be `True`. Severity: `block`.
2. **`no-secrets`** — Scan diff added lines for common secret patterns: `password\s*=`, `api_key\s*=`, `secret\s*=`, `token\s*=`, `-----BEGIN .* PRIVATE KEY-----`, `AKIA[0-9A-Z]{16}` (AWS access key). Severity: `block`.
3. **`protected-paths`** — Block diffs that touch files matching configurable glob patterns. Default protected list: `**/migrations/**`, `**/.env*`, `**/secrets/**`, `**/*.pem`, `**/*.key`. Severity: `block`.
4. **`max-diff-size`** — Block diffs larger than a configurable threshold (default: 50 000 chars). Prevents accidentally committing generated files or dumps. Severity: `block`.

### DiffReviewer class

```python
class DiffReviewer:
    def __init__(self, *, protected_paths: list[str] | None = None,
                 max_diff_size: int = 50_000):
        ...

    def review(self, context: ReviewContext) -> ReviewResult:
        """Run all rules, return verdict."""
        ...

    @staticmethod
    def parse_changed_files(diff: str) -> list[str]:
        """Extract file paths from unified diff headers (--- a/... / +++ b/...)."""
        ...
```

Verdict logic: if **any** violation has `severity == "block"`, the overall verdict is `"block"`. Otherwise `"pass"`.

### Tool

`diff_reviewer.review` — registered in the tool registry as `READ_ONLY` (it does not mutate state).

Gateway executor:
- Required payload: `diff: str`.
- Optional: `test_result: dict`, `task_description: str`, `protected_paths: list[str]`, `max_diff_size: int`.
- Returns `ReviewResult` as dict.

### Governance seed

Add 1 rule in `DEFAULT_POLICY_RULES`:
- `diff_reviewer.review.*.allow.v1` — all roles can invoke the reviewer (read-only).

## Files to create

1. `apps/backend/app/services/reviewer.py`
2. `apps/backend/tests/services/test_reviewer.py`

## Files to edit

3. `apps/backend/app/tools/registry.py` — add `diff_reviewer.review` tool definition.
4. `apps/backend/app/tools/gateway.py` — add dispatcher + executor method.
5. `apps/backend/app/services/governance.py` — add 1 policy rule.

## Tests

All tests in `apps/backend/tests/services/test_reviewer.py`. Use `unittest.TestCase`.

1. **`test_clean_diff_passes`** — Diff touching only safe paths, test result passing, no secrets. Assert `verdict == "pass"`, `violations == []`, `rules_checked == 4`.
2. **`test_secret_pattern_blocks`** — Diff with `+API_KEY = "sk-abc123"` in added line. Assert `verdict == "block"`, one violation with `rule_name == "no-secrets"`.
3. **`test_protected_path_blocks`** — Diff touching `migrations/0001_init.py`. Assert `verdict == "block"`, violation `rule_name == "protected-paths"`.
4. **`test_failing_tests_block`** — Pass `test_result={"overall_passed": False}`. Assert `verdict == "block"`, violation `rule_name == "tests-must-pass"`.
5. **`test_max_diff_size_blocks`** — Diff exceeding `max_diff_size=100`. Assert `verdict == "block"`, violation `rule_name == "max-diff-size"`.
6. **`test_parse_changed_files`** — Feed a sample unified diff, assert correct file paths extracted.
7. **`test_no_test_result_skips_test_rule`** — `test_result=None`, clean diff. Assert `verdict == "pass"`, `rules_checked == 3` (tests-must-pass skipped).
8. **`test_custom_protected_paths`** — Override `protected_paths=["**/custom/**"]`. Diff touching `custom/foo.py` blocked; diff touching `migrations/0001.py` not blocked.

## Acceptance criteria

- `python -m compileall app` exits 0 from `apps/backend/`.
- `diff_reviewer.review` in tool registry as `READ_ONLY`.
- All 8 tests pass: `python -m unittest tests.services.test_reviewer -v`.
- Full suite still green: `python -m unittest discover -s tests -v`.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/services/sandbox.py`, `test_pipeline.py`, `tools/registry.py`, `tools/gateway.py`, `services/governance.py`.
2. Create `app/services/reviewer.py` with `DiffReviewer`, 4 rules, dataclasses.
3. Wire tool in registry, gateway, governance.
4. Create `tests/services/test_reviewer.py` with 8 tests.
5. Run `python -m compileall app && python -m unittest tests.services.test_reviewer -v && python -m unittest discover -s tests -v`.

Invocation:

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-E1-diff-reviewer.md
```
