---
language: any
applies_to: any
audience: codegen-llm
priority: high
---

# Diff / patch discipline

This applies regardless of language. Most of our codegen failures are
not "the model wrote wrong logic" — they're "the model wrote an output
that the apply step couldn't apply." Follow these rules and the apply
step succeeds.

## When the prompt asks for a unified diff

- The first line is `diff --git a/<path> b/<path>`. Nothing before it.
- Hunks (`@@ -<old_start>,<old_count> +<new_start>,<new_count> @@`)
  must reflect the actual line numbers and counts in the source you
  were shown. Do not invent counts.
- Context lines (lines without `+` or `-`) must match the source
  *byte-for-byte*. Trailing whitespace, tabs vs spaces, blank-line
  counts — everything must be identical.
- If the source uses CRLF, your diff must too. If the source uses LF,
  your diff must too. Mismatches = `git apply` fails.
- One trailing newline at end of file. No more, no less.
- Do not emit a "no changes" diff. If you have nothing to change,
  output `## EVIDENCE_GAP: nothing in the evidence pack supports a
  change` and stop.

## When the prompt asks for Aider search/replace blocks

```
filename.py
<<<<<<< SEARCH
exact text from the source, unchanged
=======
the new text
>>>>>>> REPLACE
```

- The filename is on its own line, no leading whitespace, before the
  `<<<<<<< SEARCH` marker.
- Inside `SEARCH` you reproduce the source text *byte-for-byte*. If
  there's a trailing newline in source, include it.
- The `SEARCH` block must occur exactly once in the file. If your
  intended anchor appears multiple times, include enough surrounding
  lines to make the block unique.
- An empty `SEARCH` block (just `<<<<<<< SEARCH\n=======\n...`) means
  "create a new file with this content" only when paired with a
  `### NEW FILE: <path>` marker on the line above the filename.
- An empty `REPLACE` block means "delete the matched region".
- One block edits one region. To make multiple edits in one file, emit
  multiple blocks back-to-back, all under the same filename header.

## Picking the smallest correct change

- Prefer adding a new branch over rewriting an existing branch.
- Prefer extending a function over replacing it.
- Prefer the deepest scope: edit at the function level when possible,
  not the class or module level.
- Each redundant line in your diff is a chance for hunk drift. Smaller
  is more reliable.

## What to do when you can't proceed

- Evidence missing: `## EVIDENCE_GAP: <missing thing>`.
- Plan internally inconsistent: `## PLAN_CONFLICT: <what conflicts>`.
- Source already implements the requested behavior:
  `## NO_CHANGE_NEEDED: <reason>`.

These are valid terminal outputs. The orchestrator routes them to the
planner for clarification rather than to apply_patch.

### Asking the harness for missing source (preferred over EVIDENCE_GAP)

When the only thing blocking a real fix is "I need to see the body of
function X in file Y", emit a structured request instead of plain
`EVIDENCE_GAP`. The harness fetches the requested span from disk and
re-runs codegen ONE more time:

```
## EVIDENCE_GAP_REQUEST
file: django/db/models/fields/__init__.py
symbol: contribute_to_class
why: need closure binding site for _get_FIELD_display
```

Rules:

- Up to 4 requests per response. Separate each with a blank line.
- ``file`` and ``symbol`` are both optional but at least one MUST be
  present. ``symbol`` alone (no file) is rejected; ``file`` alone
  fetches the whole file (capped at 4 KB).
- Only request files / symbols that the planner or evidence pack has
  already mentioned. Names you invent will not be found.
- Do NOT mix EVIDENCE_GAP_REQUEST with diff hunks in the same response.
  The harness either retries with fetched evidence OR accepts your diff;
  it cannot do both.

When the harness retries with fetched spans you'll see a new section
``=== EVIDENCE FETCH (you asked for these) ===``. Use the spans there
as your Aider SEARCH anchors. Do NOT emit EVIDENCE_GAP again for those
names — they are now ground truth.
