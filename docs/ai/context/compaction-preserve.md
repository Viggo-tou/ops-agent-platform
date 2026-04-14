# Compaction Preservation Guide

When context is compacted, the following MUST survive in the summary:

## Always preserve
1. **Current task ID and phase** — e.g., "Working on T-E1, Phase E"
2. **What codex is currently running** — background task ID, spec file, expected output
3. **Test suite count** — e.g., "21/21 tests passing as of last run"
4. **Failed attempts** — what was tried and why it failed (prevents retry loops)
5. **Design decisions made this session** — any architectural choices not yet in DECISIONS.md
6. **Pending verification** — anything dispatched but not yet verified

## Never preserve (derivable from repo)
- File contents already on disk
- Git history
- Spec file contents (they're in docs/ai/tasks/)
- Codex log contents (they're in docs/ai/runs/)

## Compaction format
```
Phase: [X], Task: [T-XX], Tests: [N/N green]
Running: [codex task ID] for [spec file]
Failed: [what and why, if any]
Decisions: [list]
Next: [immediate next action]
```
