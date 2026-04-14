# T-C1 — Sandbox Execution Service + `sandbox.run_command` Tool

## Goal

Add an `ExecutionSandbox` service and a `sandbox.run_command` tool so the orchestrator can clone a target repository into an isolated directory and run shell commands there with captured output. This is the foundation for Phase C of the multi-agent MVP roadmap.

## Background

Today the orchestrator has no execution environment — it can read Jira, post messages, and query APIs, but cannot clone repos, apply patches, or run test pipelines. Phase C adds this capability.

The sandbox is deliberately simple for the MVP: a directory under `data/sandboxes/<task_id>/` that holds a git clone. Commands run synchronously via `subprocess.run` with hard timeouts and output capture. No containerization, no network isolation — those are hardening concerns for later.

Existing infrastructure to follow:

- Tool registry: `apps/backend/app/tools/registry.py` — same `ToolDefinition` shape as Jira tools.
- Tool gateway: `apps/backend/app/tools/gateway.py` — same `_request_json` / executor pattern.
- Dispatcher: `_execute_tool_impl` ladder in gateway.
- Settings: `apps/backend/app/core/config.py` — add new settings here.
- `ToolPermissionCategory.APPROVAL_REQUIRED` for write tools.

## Design

### ExecutionSandbox service

New file: `apps/backend/app/services/sandbox.py`.

```python
class ExecutionSandbox:
    """Manages an isolated working directory for a single task."""

    def __init__(self, task_id: str, *, base_dir: str = "data/sandboxes"):
        self.task_id = task_id
        self.sandbox_dir = Path(base_dir) / task_id
        self._cloned = False

    @property
    def work_dir(self) -> Path:
        return self.sandbox_dir

    def clone(self, repo_url: str, *, branch: str | None = None, timeout_seconds: float = 120) -> dict:
        """Clone a git repo into the sandbox directory. Returns clone metadata."""
        if self.sandbox_dir.exists():
            raise SandboxError(f"Sandbox directory already exists: {self.sandbox_dir}")
        self.sandbox_dir.mkdir(parents=True, exist_ok=False)
        cmd = ["git", "clone", "--depth", "1"]
        if branch:
            cmd.extend(["--branch", branch])
        cmd.extend([repo_url, str(self.sandbox_dir)])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds,
        )
        if result.returncode != 0:
            raise SandboxError(f"git clone failed: {result.stderr[:500]}")
        self._cloned = True
        return {
            "repo_url": repo_url,
            "branch": branch,
            "sandbox_dir": str(self.sandbox_dir),
        }

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float = 60,
        max_output_bytes: int = 64 * 1024,
    ) -> dict:
        """Run a shell command inside the sandbox. Returns structured result."""
        work_dir = Path(cwd) if cwd else self.sandbox_dir
        # Ensure work_dir is under sandbox_dir (path traversal guard)
        try:
            work_dir.resolve().relative_to(self.sandbox_dir.resolve())
        except ValueError:
            raise SandboxError(
                f"Working directory {work_dir} is outside sandbox {self.sandbox_dir}"
            )

        start = time.monotonic()
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=str(work_dir),
            )
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout[:max_output_bytes],
                "stderr": result.stderr[:max_output_bytes],
                "duration_ms": duration_ms,
                "timed_out": False,
                "command": command,
                "cwd": str(work_dir),
            }
        except subprocess.TimeoutExpired:
            duration_ms = int((time.monotonic() - start) * 1000)
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout_seconds}s",
                "duration_ms": duration_ms,
                "timed_out": True,
                "command": command,
                "cwd": str(work_dir),
            }

    def teardown(self) -> None:
        """Remove the sandbox directory."""
        if self.sandbox_dir.exists():
            import shutil
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)

    def exists(self) -> bool:
        return self.sandbox_dir.exists()


class SandboxError(Exception):
    pass
```

### Settings

Add to `apps/backend/app/core/config.py`:

```python
sandbox_base_dir: str = "data/sandboxes"
sandbox_clone_timeout_seconds: float = 120.0
sandbox_command_timeout_seconds: float = 60.0
sandbox_max_output_bytes: int = 65536
```

### Tool registry entry

Add to `apps/backend/app/tools/registry.py`:

```python
"sandbox.run_command": ToolDefinition(
    name="sandbox.run_command",
    display_name="Sandbox Run Command",
    description="Execute a shell command inside an isolated sandbox directory for a task.",
    provider_name="sandbox",
    permission_category=ToolPermissionCategory.APPROVAL_REQUIRED,
    enabled=True,  # sandbox is always available (no external dependency)
    status_message="Sandbox execution is available.",
    missing_configuration=(),
    requires_network=False,
    timeout_seconds=self.settings.sandbox_command_timeout_seconds,
    retry_count=0,  # commands are not retried
    tags=("sandbox", "execution", "shell"),
),
```

### Gateway executor

Add to `apps/backend/app/tools/gateway.py`:

**Dispatcher entry in `_execute_tool_impl`:**
```python
if definition.name == "sandbox.run_command":
    return self._execute_sandbox_run_command(definition=definition, payload=payload)
```

**New method `_execute_sandbox_run_command`:**

```python
def _execute_sandbox_run_command(self, *, definition, payload) -> dict:
    task_id = payload.get("task_id", "")
    command = payload.get("command", "")
    cwd = payload.get("cwd")  # optional relative path within sandbox

    if not task_id or not command:
        raise ToolInvocationError(
            "sandbox.run_command requires 'task_id' and 'command' in payload.",
            retryable=False,
        )

    sandbox = ExecutionSandbox(
        task_id=task_id,
        base_dir=self.settings.sandbox_base_dir,
    )

    if not sandbox.exists():
        raise ToolInvocationError(
            f"No sandbox found for task {task_id}. Clone a repo first.",
            retryable=False,
        )

    result = sandbox.run(
        command,
        cwd=cwd,
        timeout_seconds=self.settings.sandbox_command_timeout_seconds,
        max_output_bytes=self.settings.sandbox_max_output_bytes,
    )

    return {
        "status": "executed",
        "tool_name": definition.name,
        "provider": definition.provider_name,
        **result,
    }
```

### Governance policy seeds

Add to `apps/backend/app/services/governance.py::DEFAULT_POLICY_RULES`:

```python
"sandbox.run_command.employee.approval.v1": {
    # subject_role: ActorRole.EMPLOYEE
    # decision: PolicyDecision.REQUIRE_APPROVAL
    # risk_level: RiskLevel.HIGH
    # risk_category: RiskCategory.PRODUCTION_WRITE
    # required_approver_role: ActorRole.TEAM_LEAD
    # priority: 40
},
"sandbox.run_command.team_lead.allow.v1": {
    # subject_role: ActorRole.TEAM_LEAD
    # decision: PolicyDecision.ALLOW_WITH_CONSTRAINTS
    # constraints_json: {"requires_audit_note": True, "shell_execution": True}
    # priority: 45
},
```

## Files to create

1. `apps/backend/app/services/sandbox.py` — `ExecutionSandbox` class and `SandboxError` exception.
2. `apps/backend/tests/services/test_sandbox.py` — unit tests (see below).

## Files to edit

3. `apps/backend/app/core/config.py` — add 4 sandbox settings.
4. `apps/backend/app/tools/registry.py` — add `sandbox.run_command` tool definition.
5. `apps/backend/app/tools/gateway.py` — add dispatcher entry + `_execute_sandbox_run_command` method. Import `ExecutionSandbox` and `SandboxError` from `app.services.sandbox`.
6. `apps/backend/app/services/governance.py` — add 2 `DEFAULT_POLICY_RULES` entries for `sandbox.run_command`.

## Tests

`apps/backend/tests/services/test_sandbox.py`:

1. **`test_run_command_success`** — Create a sandbox dir manually (mkdir), write a small file into it, run `cat <file>` (or `type <file>` on Windows — use `echo hello` piped, or a cross-platform approach). Assert `exit_code=0`, `stdout` contains expected output, `timed_out=False`.
2. **`test_run_command_nonzero_exit`** — Run `exit 1` (or `cmd /c exit 1`). Assert `exit_code=1`.
3. **`test_run_command_timeout`** — Run a long-running command with `timeout_seconds=0.5`. Assert `timed_out=True`.
4. **`test_run_command_path_traversal_blocked`** — Pass `cwd="../.."` and assert `SandboxError` is raised.
5. **`test_sandbox_teardown`** — Create sandbox, call `teardown()`, assert directory is gone.
6. **`test_run_command_output_truncation`** — Run a command that outputs more than `max_output_bytes`, assert stdout is truncated.

Use `tempfile.mkdtemp()` for sandbox base dirs in tests. Platform-aware commands (use `echo` which works on both Windows and Unix).

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- Tool registry lists `sandbox.run_command` as enabled with `APPROVAL_REQUIRED`.
- All 6 tests pass.
- `SandboxError` is raised for path traversal attempts.
- Output truncation works.
- `DEFAULT_POLICY_RULES` includes `sandbox.run_command` entries.
- Save test output to `docs/ai/runs/T-C1.log`.

## Out of scope

- `sandbox.apply_patch` tool — that's T-C2.
- `sandbox.clone_repo` as a separate tool — for now, cloning is done by the orchestrator or via `run_command` with `git clone`.
- Container isolation, network isolation.
- Scenario wiring (the orchestrator doesn't route to sandbox tools yet — that's a later task).

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/core/config.py`, `apps/backend/app/tools/registry.py`, `apps/backend/app/tools/gateway.py`, `apps/backend/app/services/governance.py`. Confirm the existing shape before editing.
2. Check whether `apps/backend/tests/services/` exists. If not, create it with `__init__.py`.
3. Implement in order: sandbox service → config settings → registry entry → gateway executor → governance seeds → tests.
4. Run `python -m compileall app` from `apps/backend/`.
5. Run the tests. Save output to `docs/ai/runs/T-C1.log`.
6. Do not touch any file outside the list above.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-C1-sandbox-service.md
```
