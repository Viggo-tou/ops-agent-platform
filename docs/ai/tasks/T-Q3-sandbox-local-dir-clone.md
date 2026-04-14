# T-Q3 — Sandbox: Support Local Non-Git Directories

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: medium -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

Fix the sandbox `clone()` method so it works with local directories that are NOT git repositories. Currently it runs `git clone` which fails with "does not appear to be a git repository" for directories like `D:\项目\HandymanApp-master` that were extracted from a zip archive.

## Background

The develop pipeline (Phase N) fetches code context from the knowledge source path, generates a diff via codegen, then tries to clone the source repo into a sandbox to apply the patch. The knowledge source path `D:\项目\HandymanApp-master` is a plain directory (no `.git`), so `git clone` fails.

The fix: when the source is a local directory path (not a URL), check if it has `.git`. If not, use `shutil.copytree` to copy the directory into the sandbox, then run `git init && git add . && git commit -m "initial"` to create a baseline git repo that `git apply` can work against.

## Design

In `apps/backend/app/services/sandbox.py`, modify the `clone()` method:

```python
def clone(self, repo_url_or_path: str, *, timeout_seconds: float = 120.0) -> dict:
    source_path = Path(repo_url_or_path)
    
    # Case 1: Local directory (not a git repo)
    if source_path.is_dir() and not (source_path / ".git").is_dir():
        shutil.copytree(str(source_path), str(self.work_dir), dirs_exist_ok=True)
        # Initialize git so git apply works
        self.run("git init", timeout_seconds=30)
        self.run("git add .", timeout_seconds=60)
        self.run('git commit -m "initial sandbox baseline"', timeout_seconds=30)
        return {"method": "copytree", "source": str(source_path)}
    
    # Case 2: Local git repo
    if source_path.is_dir() and (source_path / ".git").is_dir():
        # git clone from local path
        ...existing logic...
    
    # Case 3: Remote URL
    ...existing logic...
```

Key points:
- Check `Path(repo_url_or_path).is_dir()` first — only local paths will match.
- Use `dirs_exist_ok=True` in case the sandbox dir was pre-created.
- After copytree, must `git init + add + commit` so `git apply` and `git diff` work.
- Return a dict indicating the method used.
- Import `shutil` at the top of the file if not already imported.

## Files to edit

1. `apps/backend/app/services/sandbox.py` — modify `clone()` to handle non-git local directories.

## Tests

Add to existing sandbox tests. Use `unittest.TestCase`.

1. **`test_clone_local_non_git_dir`** — Create a temp directory with a file (no `.git`). Call `clone()` with that path. Assert the sandbox `work_dir` contains the file and has a `.git` directory (from `git init`).
2. **`test_clone_local_git_dir_still_works`** — Create a temp directory, `git init` it. Call `clone()`. Assert existing git clone path still works.

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass.
- Full suite still green.
- `clone("D:/some/plain/dir")` copies files and initializes git in sandbox.
- `clone("D:/some/git/repo")` still uses `git clone` as before.
- `clone("https://github.com/...")` still uses `git clone` as before.

## Workflow (for the executor)

<!-- Effort: medium — modify existing sandbox logic with new branch -->

1. Read `app/services/sandbox.py` — focus on `clone()` method and how `work_dir` is set up.
2. Add the local non-git directory handling branch.
3. Add tests.
4. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-Q3-sandbox-local-dir-clone.md
```
