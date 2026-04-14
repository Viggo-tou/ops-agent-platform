# T-026-M2 — API Schema Docstrings (MiniMax)

## Owner

MiniMax (mechanical text insertion).

## Goal

Add `Field(..., description="...")` to every exported Pydantic schema field in three files so auto-generated OpenAPI docs are self-documenting. Zero behavior change.

## Files

- `apps/backend/app/schemas/memory.py`
- `apps/backend/app/schemas/model_config.py`
- `apps/backend/app/schemas/knowledge.py`

## Transformation rule (strict)

For every field in every `BaseModel` subclass in those files:

- If the field currently has no default and no `Field(...)`, replace:
  `name: Type` → `name: Type = Field(..., description="<one-sentence description>")`
- If the field already uses `Field(...)`, add `description="..."` if missing. Do not touch any existing keyword argument.
- If the field has a simple default, rewrite to `Field(default=<existing>, description="...")`.
- Do not touch `model_config = ConfigDict(...)` lines — those are not schema fields.

## Import assumption

Where `Field` is not already imported, add it to the existing `pydantic` import line. No new import blocks.

## Description guidelines

- One sentence, present tense, ≤120 chars.
- Say what the field *means in this domain*, not its type. Bad: "The id string." Good: "Stable identifier of the memory item, returned by the server."
- For timestamps: "UTC timestamp of <event>."
- For enums / constrained strings: mention allowed range if short. Otherwise reference the enum by name.
- For nested lists: describe what one element represents.

## What NOT to do

- Do not rename fields.
- Do not change types or defaults.
- Do not change `model_config`.
- Do not reorder fields.
- Do not edit any file outside the three listed.
- Do not add examples (`example=`) — descriptions only.
- Do not add class-level docstrings unless the class currently has none AND the class is non-trivial; in that case add one short sentence.

## Acceptance

- `python -m pytest apps/backend/tests/` passes (same count as before).
- `diff --stat` shows edits only in the three listed files.
- Spot-check: load `/docs` in Swagger UI — field descriptions appear under each model.
- No `description=""` (empty) entries.
