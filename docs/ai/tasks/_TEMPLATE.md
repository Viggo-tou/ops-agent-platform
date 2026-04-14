# {TASK_ID} — {Title}

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: xhigh | medium | low -->
<!-- Executor: codex | minimax -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

{One paragraph: what this task delivers.}

## Background

{Why this task exists. Dependencies on prior tasks.}

## Design

{Dataclasses, interfaces, logic. Code blocks for key structures.}

## Files to create

{Numbered list.}

## Files to edit

{Numbered list.}

## Tests

{Numbered test cases with names, setup, assertions.}

## Acceptance criteria

- `python -m compileall app` exits 0.
- All new tests pass.
- Full suite still green.
- {Task-specific criteria.}

## Workflow (for the executor)

<!-- Effort: xhigh | medium | low -->

1. Read {relevant files}.
2. {Implementation steps.}
3. Run compile + tests.

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/{TASK_ID}.md
```
