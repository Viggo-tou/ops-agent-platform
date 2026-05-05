# T-CODEGEN-SELF-VALIDATION — Codegen self-validation (Stage A)

**Status:** in-progress (2026-05-04)
**Priority:** P0 (root-cause fix per session 2026-05-04 dogfood diagnosis)
**Branch:** `feat/codegen-self-validation`

## Background

Today's 9 stages (X.6.a/b, X.7.a/b/d, X.5, X.4, X.1, plus B/E/F env tuning,
X.8.a, X.8.b) built robust infrastructure around the LLM but did not address
codegen output quality at the source. Pattern observed: codegen produces
diffs with hunk anchor drift; sandbox `git apply` fuzzy-matches and produces
structurally broken files; compile_gate catches; repair codegen reverts the
feature; final file = baseline; LLM gates pass on diff text mentioning
right keywords; task ships with no feature.

Root cause: codegen has NO fast feedback loop on "did your patch apply
cleanly to the actual source?" All validation is downstream in sandbox
apply + compile gate, by which point codegen has already returned.

## Goal

Move patch validation INTO codegen, before it returns. New service
`codegen_self_validate` runs:

1. `git apply --check` against the source repo: catches hunk drift / context
   mismatch.
2. Language-specific parse on the post-apply file: catches syntax errors
   that would only surface in compile_gate.
   - .py: py_compile
   - .js/.jsx/.mjs: node --check
   - .kt: SKIP (no fast standalone parser; gradle compile_gate handles)

If validation fails, codegen retries with the validation error in the
retry prompt. After max_retries (default 1), raises CodegenError so the
existing batch-failure path triggers.

## Files

- `apps/backend/app/services/codegen_self_validate.py` (NEW): service
- `apps/backend/app/services/codegen.py`: hook into `generate_patch`
- `apps/backend/app/core/config.py`: 2 settings
- `apps/backend/tests/services/test_codegen_self_validate.py` (NEW)

## Config

- `OPS_AGENT_CODEGEN_SELF_VALIDATION_ENABLED=true` (default)
- `OPS_AGENT_CODEGEN_SELF_VALIDATION_MAX_RETRIES=1` (default)

## Acceptance

1. compileall clean
2. 7+ unit tests pass
3. orchestrator regression suite passes
4. Future re-dogfood of P69-17 / P69-19 should see codegen retry with
   validation feedback when patch doesn't apply cleanly, instead of
   shipping garbage to sandbox.

## Out of scope

- Runtime-behavior validation (just structural): that's still review-gate
  territory.
- Kotlin parse: deferred until fast standalone Kotlin parser available.
