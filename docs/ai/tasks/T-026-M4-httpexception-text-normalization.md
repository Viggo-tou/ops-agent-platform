# T-026-M4 — HTTPException `detail` Text Normalization (MiniMax)

## Owner

MiniMax (mechanical text cleanup).

## Goal

Normalize the wording of every `raise HTTPException(..., detail=...)` in `apps/backend/app/api/` so error bodies are consistent and CJK-safe. Zero behavior change — only the string inside `detail=` can change.

## Scope

All files under `apps/backend/app/api/` only. Do not touch:

- `apps/backend/app/services/**`
- `apps/backend/app/agents/**`
- anything outside `apps/backend/app/api/`

## Normalization rules (apply in order)

1. **Capitalization.** First letter of the sentence must be uppercase. "missing x" → "Missing x".
2. **Terminal punctuation.** Every `detail` ends with a period. No exclamation marks.
3. **ASCII punctuation only.** Replace full-width colons `：`, commas `，`, periods `。`, parentheses `（）` with their ASCII equivalents. Keep CJK letters themselves untouched.
4. **No trailing whitespace.** Strip.
5. **Identifier quoting.** Tool names, field names, and IDs wrapped in backticks must become single quotes for JSON readability: `` `sandbox.run_command` `` → `'sandbox.run_command'`.
6. **Avoid leaking file paths.** If a detail currently contains a filesystem path (e.g. `data/sandboxes/...`), replace the path with a placeholder describing what was wrong, e.g. "Sandbox workspace not initialized."
7. **No f-string hygiene changes.** If the detail is `f"Missing: {name}"`, you may improve wording but keep the interpolation expression byte-identical.

## What NOT to do

- Do not change `status_code=...`.
- Do not change which exception is raised.
- Do not change the control flow.
- Do not change strings that are not inside `detail=` of an `HTTPException`.
- Do not add new exceptions.
- Do not translate English to Chinese or vice versa.

## Workflow hint for the executor

1. Grep for `raise HTTPException` in `apps/backend/app/api/`.
2. For each hit, look at the `detail=` argument.
3. Apply the rules above. If the string is already compliant, skip.
4. Save.

## Acceptance

- `python -m pytest apps/backend/tests/` passes with the same test count.
- `git diff --stat apps/backend/app/api/` shows only line-level string edits.
- No non-api file is modified.
- `grep -rn "：\|，\|。" apps/backend/app/api/` returns no matches inside `detail=` strings.
- No `detail=` without a terminal period.
