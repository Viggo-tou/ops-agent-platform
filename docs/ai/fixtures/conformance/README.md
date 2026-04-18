# Spec-Conformance Replay Fixtures

Each fixture represents one historical `jira_issue_develop` run and the
verdict the spec-conformance gate should produce for it.

## Schema

```json
{
  "id": "string (short label)",
  "description": "string (human summary)",
  "request_text": "string (Jira summary+description the user saw)",
  "normalized_request": "string | null",
  "diff": "string (unified diff the model produced)",
  "must_touch_files": ["path", ...],
  "source_tree_files": {
    "relative/path.txt": "file contents (used to seed a temp tree)"
  },
  "expected_verdict": "pass | block",
  "expected_rules": ["shadow_implementation" | "hit_delta" | "must_touch" | "planner_must_touch", ...]
}
```

`source_tree_files` is materialized into a temp directory so the gate's
anchor-scan rules can hit real files.

Run with:

```powershell
python scripts/replay_conformance.py
```

Exit code is non-zero if any fixture's observed verdict/rules drift from
expectation.
