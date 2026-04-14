# T-Q2 — Jira Issue Key Extraction Fallback

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Fix a bug where the `jira_issue_develop` pipeline fails with "No Jira issue key was found" when the LLM-based semantic translator returns `issue_key: null` even though the original request text clearly contains a Jira key like `P69-10`.

## Background

The orchestrator classifies the request as `jira_issue_develop` correctly (it found the Jira reference in the raw text). But after semantic translation, it relies on `translation.issue_key` to fetch the Jira issue. When MiniMax does the translation, it may not extract the issue key into the structured field, causing the pipeline to fail even though the key is right there in the original text.

The function `extract_jira_issue_reference()` in `app/core/jira.py` can parse Jira keys from raw text. It's already used during `classify_request()`. The fix is to also use it as a fallback after translation if `translation.issue_key` is null.

## Design

In the orchestrator's `bootstrap_task()` method (or wherever it reads `issue_key` from the translation to fetch the Jira issue), add a fallback:

```python
# After getting translation result:
issue_key = translation.issue_key
if not issue_key:
    # Fallback: extract from raw request text
    jira_ref = extract_jira_issue_reference(task.request_text)
    if jira_ref:
        issue_key = jira_ref.get("issue_key")
```

This should happen BEFORE the "No Jira issue key" failure check. The existing `extract_jira_issue_reference()` function handles:
- Plain keys like `P69-10`
- Full URLs like `https://example.atlassian.net/browse/P69-10`
- Board URLs with `selectedIssue=P69-10`

Also update the translation document's `issue_key` field in-place so downstream code sees the correct value.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — add fallback extraction after semantic translation, before Jira issue fetch.

## Tests

Add to existing orchestrator tests. Use `unittest.TestCase`.

1. **`test_jira_develop_fallback_extraction`** — Create a task with request "implement P69-10". Mock the semantic translator to return `issue_key=None`. Assert the orchestrator still extracts `P69-10` from raw text and does NOT fail with "No Jira issue key".

## Acceptance criteria

- `python -m compileall app` exits 0.
- New test passes.
- Full suite still green.
- Requesting "implement P69-10" with a translator that returns `issue_key=null` still proceeds to fetch the Jira issue.

## Workflow (for the executor)

<!-- Effort: low — one-line fallback in existing method -->

1. Read `app/orchestrator/service.py` — find where `issue_key` is read from the translation result and where the "No Jira issue key" error is raised. Also read `app/core/jira.py` for `extract_jira_issue_reference`.
2. Add the fallback extraction logic.
3. Add test.
4. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q2-jira-key-fallback.md
```
