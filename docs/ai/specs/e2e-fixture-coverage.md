# Spec: Expand E2E Fixture Coverage (T-E2EFIX)

## Goal

Today the develop pipeline has only been proved to work on two Jira scenarios:
P69-8 (small multi-file bug fix) and P69-10 (spec_conformance-blocked scenario).
That is not enough coverage to claim "generation pipeline is ready." This
task adds a **fixture-driven integration test suite** that runs the full real
pipeline against a set of representative ticket types, producing pass/fail +
artifact evidence per fixture.

This task is **backend only**. The Playwright smoke variant is explicitly
deferred to a follow-up spec.

## Scope

- 4 fixture tickets targeting the currently-configured KB
  (`OPS_AGENT_KNOWLEDGE_SOURCE_PATH = D:\项目\HostedDashboard\handyman-admin-dashboard`).
- A pytest-driven integration test per fixture, marked `@pytest.mark.e2e`
  and **excluded from the default `pytest apps/backend` run** (too slow, uses
  real LLM calls).
- A small report generator that collects per-fixture outputs into
  `data/e2e-reports/{timestamp}/report.md`.

## The 4 fixture tickets

All target HostedDashboard/handyman-admin-dashboard so the existing KB config
works unchanged.

| # | id         | type                | Intent text                                                                     | Expected outcome          |
|---|------------|---------------------|----------------------------------------------------------------------------------|---------------------------|
| 1 | FX-NEWFILE | new file            | "Add a useDebounce hook in src/hooks/useDebounce.js that accepts value and delay and returns the debounced value." | awaiting_approval, 1 new file under `src/hooks/` |
| 2 | FX-RENAME  | cross-file rename   | "Rename the component `JobCategoryStats` to `ServiceCategoryStats` across the codebase, keeping all props and behavior." | awaiting_approval, diff touches JobCategoryStats.js + all importers |
| 3 | FX-CSS     | style-only change   | "In the sidebar, change the active-link background color to #2563eb without altering any layout or behavior." | awaiting_approval, diff touches a `.css` / `.scss` file only |
| 4 | FX-FEATURE | feature add         | "Add a 'Last Login' column to the UserManagement table that shows the `lastLogin` timestamp formatted as YYYY-MM-DD HH:mm when present, else em-dash." | awaiting_approval, UserManagement.js modified with new column and optional helper |

For each fixture, codegen may hit compile_gate / spec_conformance failures —
that is acceptable as long as the pipeline reaches a terminal state
(AWAITING_APPROVAL or FAILED with recorded findings). The tests record the
outcome; they do **not** assert success. The assertion is "pipeline ran to
completion and produced a diff."

## Files to touch

1. `apps/backend/tests/fixtures/e2e_tickets/` *(new dir)*
   - One JSON file per fixture:
     `fx_newfile.json`, `fx_rename.json`, `fx_css.json`, `fx_feature.json`
   - Schema per file:
     ```json
     {
       "id": "FX-NEWFILE",
       "type": "new_file",
       "title": "Add useDebounce hook",
       "intent": "Add a useDebounce hook in src/hooks/useDebounce.js ...",
       "expected": {
         "min_files_touched": 1,
         "file_path_patterns": ["src/hooks/"],
         "disallowed_path_patterns": []
       }
     }
     ```
   - `disallowed_path_patterns` lets FX-CSS assert the diff stayed inside
     stylesheets (pattern = regex or simple glob).

2. `apps/backend/tests/e2e/` *(new dir)* + `tests/e2e/__init__.py` + 
   `tests/e2e/test_fixture_coverage.py`
   - Single test file using `pytest.mark.parametrize` over the 4 fixture
     JSONs.
   - Marker: `@pytest.mark.e2e` at class or test level.
   - Per fixture:
     1. Load fixture JSON.
     2. Construct `TaskCreateRequest` with `request=intent`, admin actor.
     3. Call `TaskService(db).create_task(payload)` — this triggers the
        sync-overridden executor from the autouse fixture (P0), so the
        pipeline runs inline to completion inside the test.
     4. Re-fetch task after `create_task` returns.
     5. Record artifact: write
        `data/e2e-reports/{timestamp}/{fixture_id}.json` containing
        `{task_id, status, workflow_stage, events[], review_json, duration_s}`.
     6. **Assertions** (soft — pipeline must conclude, not necessarily pass):
        - `task.status in {AWAITING_APPROVAL, FAILED, COMPLETED}` (not stuck
          in `CREATED`/`PLANNING`).
        - `task.workflow_stage in {REVIEW, DONE}`.
        - When status=AWAITING_APPROVAL and fixture has
          `expected.file_path_patterns`: every changed file path in the diff
          matches at least one allowed pattern; and no file path matches any
          `disallowed_path_patterns`.
        - `duration_s < 600` (10-min hard ceiling — beyond this is a regression).

3. `apps/backend/tests/e2e/conftest.py` *(new)*
   - Session-scoped fixture `e2e_report_dir` producing
     `data/e2e-reports/{YYYYMMDD-HHMMSS}/` and printing its path at session
     start so the user can `tail -f` or open it.
   - Autouse fixture to skip the whole `tests/e2e/` directory when the
     `OPS_AGENT_KNOWLEDGE_SOURCE_PATH` env var points at a missing path (so
     these tests never run on a CI box without the KB mounted).

4. `apps/backend/tests/e2e/_report.py` *(new helper, private)*
   - `write_fixture_report(report_dir: Path, fixture_id: str, payload: dict) -> None`
   - `append_summary_row(report_dir: Path, row: dict) -> None` — appends to
     `report.md` with a markdown table: `| fixture | status | duration | notes |`.

5. `pyproject.toml` **or** create `apps/backend/pytest.ini`
   - There is no pytest config file today — adding `pytest.ini` is minimal
     and keeps the marker declaration out of app code:
     ```ini
     [pytest]
     markers =
         e2e: slow integration tests that run the real pipeline against a real KB
     addopts = -m "not e2e"
     testpaths = tests
     ```
   - `addopts = -m "not e2e"` makes the default invocation (`pytest apps/backend`)
     skip these fixtures. Running them requires `pytest -m e2e`.

6. `scripts/run-e2e-fixtures.ps1` *(new)*
   - Thin wrapper:
     ```powershell
     Push-Location "$PSScriptRoot\..\apps\backend"
     & python -m pytest -m e2e -v tests/e2e/
     Pop-Location
     ```

## Explicit constraints

- **Do NOT** mock codex / minimax / the LLM providers. These tests must
  exercise the real pipeline, including the network/subprocess calls. The
  point of this suite is that we catch regressions in real behavior, not
  that we verify mock wiring.
- **Do NOT** invent new fixture ticket types beyond the 4 listed.
- **Do NOT** parallelize. Run fixtures serially; the KB is shared state.
- **Do NOT** introduce new runtime deps.
- **Do NOT** modify any orchestrator / service / API code. If a test
  requires data the pipeline doesn't expose, capture what IS exposed and
  note the gap as a finding in the fixture report — but leave the code
  alone.
- Test artifacts under `data/e2e-reports/` must be `.gitignore`d. Add
  `data/e2e-reports/` to the root `.gitignore` if not already covered by a
  broader rule — check first.

## Non-requirements (explicit DO NOTs)

- No Playwright / browser driving. That's a separate spec.
- No backend `BackgroundTasks` introspection — the P0 autouse sync fixture
  already makes `create_task` synchronous inside tests.
- No fixture for a backend-language target (e.g. express/FastAPI). The
  configured KB is React; don't force a second KB.
- No CI wiring — document the manual command, nothing more. Adding these
  to CI is a separate discussion.
- No screenshots. These are pure backend integration tests.

## Acceptance criteria

- `pytest apps/backend` (no args) still passes in the same time as today —
  the new tests **must not** run by default.
- `pytest apps/backend -m e2e -v` discovers 4 tests (one per fixture) and
  runs them serially.
- Each fixture produces a JSON artifact under `data/e2e-reports/{ts}/`.
- A `report.md` summary table appears under the same directory after the
  run, with one row per fixture + its final status + duration.
- When the KB path is missing, the e2e collection is skipped with a clear
  message ("Knowledge source path not found — skipping e2e fixtures").
- No regression to the default 289-test suite baseline.

## Out of scope

- Playwright UI smoke (later spec).
- GateStatusPanel / DiffViewer visualization (separate in-flight tasks).
- Git push/PR (item 3 in the roadmap — later spec).
- Auto-grading fixtures pass/fail beyond "pipeline concluded + file-path
  pattern match." Quality judgment stays human.
