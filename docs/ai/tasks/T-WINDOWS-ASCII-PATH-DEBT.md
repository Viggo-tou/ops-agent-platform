# T-WINDOWS-ASCII-PATH-DEBT — Resolve persistent Windows non-ASCII path issues

<!-- SPEC TEMPLATE v2 -->
<!-- Effort: low (decision) + medium (execution depending on path chosen) -->
<!-- Executor: claude (decision) → user (execution if path A) → codex (path C tooling) -->

**Status:** todo (P2 — recurring noise; not blocking but persistent)
**Priority:** P2 (every codex dispatch hits this; every test run reports false failures; 8 backend test failures + 1 error in latest baseline are all from this)
**Created:** 2026-04-28

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (project lives at `D:\项目\Ops_agent_platform\`).
Backend root: `apps/backend/`.

## Goal

Decide on and execute one of three paths to stop Windows + non-ASCII path (`项目` 中文字符) issues from contaminating every codex dispatch, test run, and benchmark. Either:
- **Path A**: Move the project to an ASCII-only path (one-time pain, permanent fix)
- **Path B**: Add tooling to make the existing setup robust (locale config + git encoding + scripted ASCII shadow)
- **Path C**: Document the constraint as permanent and live with it (worst, but acceptable if A and B too painful)

## Background

### Where this has bitten in 2026-04-23 → 2026-04-28

| Symptom | Where | Fix attempted |
|---|---|---|
| Java I/O `IOException: 文件名、目录名或卷标语法不正确` | gradle build of HandymanApp project (separate but in same root tree) | robocopy to ASCII path D:\HandymanApp-build |
| Node.js `Invalid package config` from `package_json_reader` | sandbox in P69-7 develop pipeline | T-COMPILE-GATE-ERROR-CLASSIFICATION (in-flight) |
| codex sandbox can't create git tag | every codex dispatch ("Required baseline tag could not be created because Git metadata writes are permission-denied under D:/项目") | tolerated; codex documents the miss |
| Backend test discovery failures | 8/151 backend tests fail every run; all with mojibake D:/椤圭洰/... in error message | none |
| python -m compileall sometimes fails | only on specific extensions / .pyc paths | ad-hoc retry |
| pytest cache invalidates between runs because of path encoding inconsistency | observed sporadically | tolerated |

### What's NOT broken

- the actual code lives in UTF-8 source files, encoding is fine within Python / Node
- git operations work (after small adaptations)
- npm / npx / claude-code-cli mostly work

The pain points cluster on **tools that walk paths and embed them into error messages or config files** — Java, Node module resolver, OpenTelemetry, gradle, sandbox subprocess wrappers.

### Why we haven't fixed it

User has explicitly preferred not to rename `D:\项目` (2026-04-28: "项目名先不改了"). Reasons cited:
- cross-cutting impact: every project under `D:\项目\` would need to follow
- many IDE / tool / config files cache the path
- sandbox / data dirs carry the path string in many places

But the cost of NOT fixing it accumulates: every benchmark report has 8 "expected" test failures masking real regressions; every codex dispatch has a session-tag-blocked warning; every new tool stack we introduce (Android, Vercel, Anthropic API, etc.) discovers the issue independently.

## Design — three paths to evaluate

### Path A — Move the project to ASCII path (recommended, but big upfront pain)

```
D:\项目\Ops_agent_platform\  →  D:\projects\ops-agent-platform\
```

**Pros**:
- Eliminates the entire class of problems permanently
- All future tools / dispatches / test runs become signal-clean
- Makes the project portable (clone to new machine without translating path)

**Cons**:
- One-time disruption: ~3-5 hours of finding all hardcoded `D:\项目` references
- Existing data dirs / sandboxes / DB paths may have absolute path strings cached
- IDE workspace files (.idea/, .vscode/) need re-indexing
- All open worktrees need recreation
- All env vars and shell history with the old path become wrong

**Implementation outline**:
1. Tag everything: `git tag pre-ascii-migration-2026-MM-DD`
2. Backup `.env`, DB, and any local-only files
3. `robocopy D:\项目\Ops_agent_platform D:\projects\ops-agent-platform /MIR /NFL /NDL`
4. Update `.env` paths
5. Re-create worktrees pointing to new location
6. Verify backend starts, tests run, benchmark runs, frontend builds
7. Delete old location

### Path B — Tooling around the existing setup

Don't move; instrument:

1. Add `OPS_AGENT_PROJECT_ROOT_OVERRIDE` env var that scripts use instead of hardcoded `D:\项目\...`
2. Set `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1` globally (Windows-wide)
3. Add `LC_ALL=en_US.UTF-8` and `LANG=en_US.UTF-8` to all dispatch envs
4. For Java/gradle work, document the "robocopy to ASCII shadow" pattern as standard procedure
5. For sandbox dirs: ensure orchestrator creates them under `D:\sandboxes\` (ASCII) instead of under project tree
6. Run a sweep through codebase for hardcoded `D:\项目\` strings; replace with config-driven paths

**Pros**:
- No big migration
- Most fixes are localized
- Removes recurring noise

**Cons**:
- Whack-a-mole: every new tool may surface a new mojibake bug
- 8 currently-failing tests still need individual fixes
- Doesn't fix the underlying class

### Path C — Live with it, document, monitor

Accept the noise. Add a section to `AGENTS.md` / `CLAUDE.md` / `STAGE_LOG.md` reading:

> **Known Constraint**: Project lives at `D:\项目\Ops_agent_platform\`. The non-ASCII path causes:
> - 8/151 backend tests fail every run (test_sandbox_*, test_*_git_init); these are tolerated
> - codex `session-start` git tags cannot be created (warning only; commit still works)
> - Java tools require ASCII shadow (robocopy pattern)
> - Sandbox config files may emit mojibake error messages
>
> When evaluating test failures, check if the failure is in the known list. If not, treat as real.

**Pros**: zero work
**Cons**: persistent noise; future sessions re-discover the issues

## Recommendation

**Path A** if you have a 3-5 hour window in the next 2 weeks. The compounding cost of B/C exceeds that one-time cost within ~1 month at current development pace.

**Path B** if you want to avoid migration pain. Budget ~1 day of focused tooling work.

**Path C** is a placeholder; should not be the long-term answer.

## Acceptance criteria (depending on path)

### Path A
- Project at new ASCII location
- Backend starts: `curl /health` returns 200
- All worktrees recreated
- Full backend test suite green count ≥ 159 (vs current 151; the 8 mojibake-related tests should now pass)
- Benchmark runs to completion in same env

### Path B
- New env vars wired through start scripts
- `PYTHONUTF8=1` set globally
- Sweep complete: `grep -rE "D:\\\\项目" --include="*.py" --include="*.ps1" --include="*.json"` returns 0 matches in source code (data files / docs allowed)
- Backend test failures from mojibake reduced from 8 to ≤2

### Path C
- AGENTS.md / CLAUDE.md / STAGE_LOG.md updated with the "known constraint" section
- Add a `pytest` marker `@pytest.mark.windows_ascii_known_failure` on the 8 known failures so they don't appear as red

## Files / actions per path

### Path A files (the migration steps)
- `.env` — paths updated
- `local.properties` (if any) — updated
- `apps/backend/.env` — updated
- `scripts/*.ps1` — verify no hardcoded D:\项目 paths
- `data/sandboxes/` — relocate if absolute paths cached

### Path B files
- `apps/backend/app/core/config.py` — new env-driven path settings
- `scripts/start-backend.ps1` — set UTF-8 env vars
- `scripts/common.ps1` — same
- New helper `apps/backend/scripts/ascii_shadow_helper.py` (?)

### Path C files
- `CLAUDE.md` — add Known Constraint section
- `AGENTS.md` — same
- `apps/backend/conftest.py` — register the marker
- 8 test files — apply marker

## Workflow

This is a **decision ticket**, not an implementation ticket. The decision belongs to the user. Once decided:

- Path A: user executes the migration; codex doesn't fit because the migration touches absolute paths everywhere
- Path B: codex can do the sweep + tooling
- Path C: trivial, anyone

Recommend: schedule a 30-min decision meeting, then 1 day execution if A or B chosen.

## Out of scope

- Switching to Linux / WSL — that's a different project entirely
- Renaming Chinese-named SUBdirectories within the project (e.g. inside data/) — focus is on the project root path

## Risks

| Risk | Mitigation |
|---|---|
| Migration loses uncommitted work | Tag + backup before moving |
| New ASCII path conflicts with another project | Pick a clearly unique name |
| Path B sweep misses some hardcoded ref | Track new mojibake errors as Path-B-followups, accept long tail |
