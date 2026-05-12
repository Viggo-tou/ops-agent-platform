---
language: python
applies_to:
  - "*.py"
  - pyproject.toml
  - setup.py
  - requirements.txt
audience: codegen-llm
priority: high
---

# Python codegen rules

You are generating a *minimal* edit to fix the described issue. These
rules apply on every Python edit; when they conflict with task-specific
instructions, the task wins, but you must call out the conflict in your
output.

## Scope

- Only modify files that the plan's `must_touch_files` lists. If you
  feel another file must change, stop and surface that in your output
  rather than silently editing it. The reviewer can request a plan
  amendment.
- Do not rename functions, classes, or modules unless the plan
  specifies it. Renames cascade through call sites that may not be in
  your evidence pack.
- Do not reorganize imports. Add new imports at the bottom of the
  existing import block; do not reorder existing ones.
- Do not reformat. No `black` / `isort` / whitespace passes. The
  reviewer will reject patches whose diff size is dominated by
  unrelated whitespace.

## Imports

- Add an import only when you reference the symbol in your changes.
- Prefer `from X import Y` if the file already uses that style for
  similar imports.
- Never `import *`. Never relative imports beyond what already exists
  in the file.

## API surface

- Public function signatures (no leading underscore) are part of the
  contract. Do not add required parameters; if you must add one, give
  it a default that preserves existing behavior.
- Adding **optional** keyword arguments with defaults is allowed.
- Removing parameters, even keyword ones, is forbidden without an
  explicit plan note.

## Behavior preservation

- The patch must not regress any code path the plan didn't claim to
  change. If the issue is "X fails in case A", your edit affects case
  A's branch; cases B/C/D must observe identical behavior.
- Adding `if/elif` branches that special-case the new behavior is
  preferred over rewriting the existing logic.
- Watch out for short-circuit semantics. `a and b or c` means
  `(a and b) or c`. If you change the condition, you may inadvertently
  flip behavior; use parentheses + named locals for clarity.

## None / mutability

- Treat the case where an argument is `None` as a real branch. Many
  bugs we ship are "the patch added a code path but didn't gate it on
  `arg is None`".
- Do not mutate kwargs or default-mutable arguments inherited from the
  signature. Build a copy.

## Tests

- Do not add tests unless the plan explicitly asks for them. The
  benchmark / CI grader runs its own tests; adding tests dilutes your
  diff and triggers the patch_budget gate.
- If you do add a test (because plan says so), put it in the same
  `tests/` location as the file you modified.

## What to emit

- Use the format the codegen prompt specifies (unified diff or Aider
  search/replace blocks). Do NOT mix.
- Do NOT emit explanatory prose. The first line of your output should
  be either `diff --git` (unified diff mode) or the first filename
  (Aider blocks mode).
- If the plan is unclear or the evidence pack is missing context you
  would need, emit a single-line marker `## EVIDENCE_GAP: <what's
  missing>` and stop. The orchestrator will retry with more context.

## Common failure modes (do not repeat)

- Adding a comment instead of code. The reviewer's
  `feature_presence_check` will catch you.
- Editing a file the plan did not name. The `patch_budget` gate will
  reject the patch.
- Returning a "summary of what I changed" instead of the diff. Your
  output is parsed by a regex, not read by a human.
- Drift in unified diff context lines (whitespace, trailing comments).
  When the format is unified diff, copy context lines from source
  byte-for-byte; do not paraphrase.
