# T-IR-V2 — Intent Resolution Agent V2.0

## Goal

Replace V1's single-shot `refine_request_cli()` with an agent-based
intent resolution service that can autonomously fetch context via MCP
tools before producing a refined request.

V2.0 scope: **Jira-only via Claude CLI with MCP tools**.  The agent
controls the Jira fetch instead of receiving it passively.

See architecture spec: `docs/ai/specs/intent-resolution-agent-v2.md`

## Background

V1 (`apps/backend/app/services/request_refinement.py`) works:
- Pre-fetches Jira context, builds a prompt, sends to `claude -p`
- Claude reads everything in one shot and outputs a refined request
- 9-second latency, good quality

V1's limitation: only handles pre-fetched Jira context. Can't resolve
URLs, GitHub issues, Slack threads, or chain lookups (Jira ticket
mentions a design doc → need to fetch that too).

V2.0 changes the paradigm: instead of pre-fetching all context and
stuffing it into the prompt, the Claude CLI subprocess gets access to
MCP tools and decides what to fetch on its own.

## Design

### New service: `apps/backend/app/services/intent_resolution.py`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ResolvedIntent:
    refined_text: str
    tool_calls_made: int       # how many MCP tool calls the agent made
    sources_consulted: list[str]  # e.g. ["jira:P69-10", "url:https://..."]
    elapsed_seconds: float

def resolve_intent(
    *,
    user_input: str,
    pre_fetched_context: dict | None = None,
    translation: dict | None = None,
    source_tree_summary: str | None = None,
    allowed_tools: list[str] | None = None,
    max_tool_calls: int = 3,
    timeout_seconds: float = 90.0,
    claude_model: str | None = None,
) -> ResolvedIntent:
    """Run the intent resolution agent via Claude Code CLI with MCP tools.

    Falls back to V1 refine_request_cli if MCP tools are not available.

    The function:
    1. Checks if MCP tools are configured (via settings or .mcp.json)
    2. If yes: runs claude -p --allowedTools <tools> with the agent prompt
    3. If no: falls back to V1 refine_request_cli
    4. Parses the output and returns ResolvedIntent
    """
```

### Agent system prompt

The system prompt positions the LLM as an intent resolution agent with
an observe-decide-act loop.  Key rules:

- Read user input, determine what external sources need to be read
- Call MCP tools to fetch context (jira.get_issue, web.fetch, etc.)
- Chain lookups if a source references another source
- Maximum `max_tool_calls` tool calls, then produce best-effort output
- Output plain text refined request in English
- List each required change as a numbered step
- Self-contained — readable without seeing original sources

Full system prompt is in the architecture spec.

### MCP tool configuration

V2.0 requires at minimum a Jira MCP server.  The service should:

1. Check `settings.MCP_JIRA_ENABLED` (new setting, default False)
2. If enabled, check that the Jira MCP server is running/configured
3. Build `--allowedTools` list from available MCP tools
4. If no MCP tools are available, raise `MCPNotConfiguredError`

For V2.0, the allowed tools list is:
- `mcp__jira__get_issue` — read a Jira ticket by key
- Any `file.read` / built-in tools the CLI already has

### Fallback chain in orchestrator

Modify `_refine_request` in `apps/backend/app/orchestrator/service.py`:

```python
def _refine_request(self, *, task, actor_name, issue_context, semantic_translation) -> str | None:
    # Skip conditions remain the same

    # Try V2 (agent with MCP tools)
    try:
        result = resolve_intent(
            user_input=raw,
            pre_fetched_context=issue_context,
            translation=task.translation_json,
            source_tree_summary=source_tree_summary,
            allowed_tools=["mcp__jira__get_issue"],
            max_tool_calls=3,
            timeout_seconds=90,
        )
        self.event_service.emit(task.id, "TOOL_SUCCEEDED", ...)
        return result.refined_text
    except MCPNotConfiguredError:
        # Fall through to V1
        pass
    except Exception as exc:
        self.event_service.emit(task.id, "TOOL_FAILED", ...)
        # Fall through to V1

    # Try V1 (single-shot prompt with pre-fetched context)
    try:
        from app.services.request_refinement import refine_request_cli
        refined = refine_request_cli(...)
        return refined.refined_text
    except Exception:
        pass

    # Final fallback: mechanical augmentation
    return None
```

### Settings

Add to `apps/backend/app/core/config.py`:

```python
# Intent Resolution V2
MCP_JIRA_ENABLED: bool = False  # Enable Jira MCP server for intent resolution
MCP_JIRA_SERVER_URL: str = ""   # URL of the Jira MCP server (if external)
INTENT_RESOLUTION_VERSION: str = "auto"  # "v1", "v2", or "auto" (try v2, fall back to v1)
INTENT_RESOLUTION_TIMEOUT: float = 90.0
INTENT_RESOLUTION_MAX_TOOLS: int = 3
```

## Files to edit

| File | Action | Description |
|---|---|---|
| `apps/backend/app/services/intent_resolution.py` | CREATE | New service module |
| `apps/backend/app/orchestrator/service.py` | MODIFY | Update `_refine_request` with V2 → V1 fallback chain |
| `apps/backend/app/core/config.py` | MODIFY | Add MCP/intent resolution settings |
| `apps/backend/tests/services/test_intent_resolution.py` | CREATE | Unit tests for the new service |
| `apps/backend/tests/orchestrator/test_intent_resolution_integration.py` | CREATE | Integration tests for the fallback chain |

## Acceptance criteria

1. `resolve_intent()` function exists and can be imported
2. When `MCP_JIRA_ENABLED=True` and Jira MCP server is available, `resolve_intent` runs `claude -p --allowedTools mcp__jira__*` and returns a `ResolvedIntent`
3. When MCP is not configured, `resolve_intent` raises `MCPNotConfiguredError`
4. Orchestrator's `_refine_request` tries V2 first, falls back to V1, falls back to None
5. All existing tests still pass (318/318)
6. New unit tests cover: agent prompt construction, response parsing, MCP not configured error, timeout handling
7. New integration tests cover: V2→V1 fallback, V2 success path (mocked MCP), V1 still works when V2 disabled
8. `python -m compileall apps/backend/app` passes with no errors

## Workflow (for the executor)

**Executor: codex**

1. Read the architecture spec at `docs/ai/specs/intent-resolution-agent-v2.md`
2. Read the existing V1 at `apps/backend/app/services/request_refinement.py`
3. Read the current orchestrator `_refine_request` method at `apps/backend/app/orchestrator/service.py`
4. Create `intent_resolution.py` with the service
5. Update orchestrator with the fallback chain
6. Add settings to config.py
7. Write tests
8. Verify: `python -m compileall apps/backend/app` and `python -m pytest apps/backend/tests/ -x`
