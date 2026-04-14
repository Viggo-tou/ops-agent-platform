# T-C2 — `sandbox.apply_patch` Tool

## Goal

Add a `sandbox.apply_patch` tool so the orchestrator can apply unified diffs to the sandboxed repository. Records the pre-patch commit SHA for rollback.

## Background

T-C1 added `ExecutionSandbox` and `sandbox.run_command`. T-C2 builds on it by adding a patch application tool that:

1. Takes a unified diff string and applies it via `git apply` inside the sandbox.
2. Records the `before_sha` (HEAD commit before the patch) so Phase G rollback can `git checkout <before_sha>`.
3. Optionally commits the applied patch with a descriptive message.

## Files to edit

### 1. `apps/backend/app/services/sandbox.py`

Add method to `ExecutionSandbox`:

```python
def apply_patch(
    self,
    patch: str,
    *,
    commit: bool = True,
    commit_message: str = "Applied patch via sandbox",
    timeout_seconds: float = 30,
) -> dict:
    """Apply a unified diff to the sandbox repo. Returns before/after SHAs."""
    if not self.exists():
        raise SandboxError(f"Sandbox does not exist: {self.sandbox_dir}")

    # Record before SHA
    before_result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True, text=True,
        cwd=str(self.sandbox_dir), timeout=10,
    )
    before_sha = before_result.stdout.strip() if before_result.returncode == 0 else ""

    # Write patch to temp file and apply
    patch_file = self.sandbox_dir / ".claude_patch.diff"
    patch_file.write_text(patch, encoding="utf-8")
    try:
        apply_result = subprocess.run(
            ["git", "apply", "--stat", "--apply", str(patch_file)],
            capture_output=True, text=True,
            cwd=str(self.sandbox_dir), timeout=timeout_seconds,
        )
        if apply_result.returncode != 0:
            raise SandboxError(f"git apply failed: {apply_result.stderr[:500]}")
    finally:
        patch_file.unlink(missing_ok=True)

    after_sha = before_sha
    if commit:
        subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True,
            cwd=str(self.sandbox_dir), timeout=10,
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message, "--allow-empty"],
            capture_output=True, text=True,
            cwd=str(self.sandbox_dir), timeout=10,
        )
        if commit_result.returncode == 0:
            sha_result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True,
                cwd=str(self.sandbox_dir), timeout=10,
            )
            after_sha = sha_result.stdout.strip()

    return {
        "before_sha": before_sha,
        "after_sha": after_sha,
        "committed": commit,
        "patch_stats": apply_result.stdout[:500] if apply_result.returncode == 0 else "",
        "sandbox_dir": str(self.sandbox_dir),
    }
```

### 2. `apps/backend/app/tools/registry.py`

Add tool definition:

```python
"sandbox.apply_patch": ToolDefinition(
    name="sandbox.apply_patch",
    display_name="Sandbox Apply Patch",
    description="Apply a unified diff to the sandboxed repository, recording the pre-patch state for rollback.",
    provider_name="sandbox",
    permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
    enabled=True,
    status_message="Sandbox patch application is available.",
    missing_configuration=(),
    requires_network=False,
    timeout_seconds=30.0,
    retry_count=0,
    tags=("sandbox", "execution", "patch"),
),
```

### 3. `apps/backend/app/tools/gateway.py`

**Dispatcher entry:**
```python
if definition.name == "sandbox.apply_patch":
    return self._execute_sandbox_apply_patch(definition=definition, payload=payload)
```

**New method `_execute_sandbox_apply_patch`:**

- Required payload: `task_id: str`, `patch: str`.
- Optional: `commit: bool = True`, `commit_message: str`.
- Validates non-empty `task_id` and `patch`.
- Creates `ExecutionSandbox(task_id, base_dir=settings)`.
- Calls `sandbox.apply_patch(...)`.
- Returns structured result with `status="patched"`, `before_sha`, `after_sha`, `tool_name`, `provider`.

### 4. `apps/backend/app/services/governance.py`

Add 2 `DEFAULT_POLICY_RULES` entries for `sandbox.apply_patch`:
- `sandbox.apply_patch.employee.approval.v1` — REQUIRE_APPROVAL, HIGH risk, CHANGE_MANAGEMENT
- `sandbox.apply_patch.team_lead.allow.v1` — ALLOW_WITH_CONSTRAINTS

## Files to create

### 5. `apps/backend/tests/services/test_sandbox_patch.py`

Unit tests:
1. **`test_apply_patch_success`** — Initialize a git repo in a temp dir, write a file, commit it, create a valid unified diff, apply via `apply_patch()`. Assert `before_sha != after_sha`, `committed=True`.
2. **`test_apply_patch_bad_diff`** — Apply an invalid diff string. Assert `SandboxError`.
3. **`test_apply_patch_no_sandbox`** — Call on a non-existent sandbox dir. Assert `SandboxError`.
4. **`test_apply_patch_no_commit`** — Apply with `commit=False`. Assert `before_sha == after_sha`.

Use `tempfile.mkdtemp()` and initialize real git repos for these tests (git init + initial commit).

## Acceptance criteria

- `python -m compileall app` exits 0.
- `sandbox.apply_patch` appears in tool registry.
- All 4 tests pass.
- Save test output to `docs/ai/runs/T-C2.log`.

## Workflow (for the executor, i.e. Codex)

1. Read the files from T-C1 first: `apps/backend/app/services/sandbox.py`, registry, gateway, governance.
2. Add the `apply_patch` method to the existing `ExecutionSandbox` class.
3. Add the tool definition, gateway executor, and governance seeds.
4. Write tests. Use real git repos in temp directories.
5. Run `python -m compileall app` and the tests. Save to `docs/ai/runs/T-C2.log`.

Invocation:

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-C2-sandbox-apply-patch.md
```
