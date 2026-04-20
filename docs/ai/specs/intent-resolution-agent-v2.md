# Spec: Intent Resolution Agent V2

## Context

V1 (request-refinement-gate.md) implements a single-shot LLM call that
reads pre-fetched Jira context and translates it into code operation
instructions.  It works, but is limited:

- Only handles Jira — can't resolve GitHub issues, Slack threads,
  Confluence docs, or arbitrary URLs
- Can't chain lookups (Jira ticket mentions a design doc → need to
  fetch that too)
- Can't ask the user for clarification when intent is ambiguous

V2 turns the refinement node into a **proper agent** with tool access.

## What makes this an agent (not a pipeline step)

A pipeline step receives fixed input and produces fixed output.
An agent has an **observe-decide-act loop**:

```
observe: read the user's input
decide:  "this references a Jira ticket, I need to read it"
act:     call jira.get_issue("P69-10")
observe: read the Jira response — it mentions a Confluence design doc
decide:  "I should also read the design doc to understand the full scope"
act:     call confluence.get_page("DOC-123")
observe: now I have everything I need
output:  precise code operation instructions
```

The LLM decides **which tools to call, in what order, and when to stop**.
This is the core difference from V1's fixed prompt-in → text-out pattern.

## Architecture

### Agent runtime: Claude Code CLI with MCP tools

We don't build our own agent loop.  Claude Code CLI IS an agent runtime:
- It receives a prompt
- It has access to tools (via `--allowedTools` or MCP servers)
- It runs an internal observe-think-act loop
- It outputs a final result

```
subprocess: claude -p --allowedTools mcp__jira,mcp__github,mcp__web_fetch
  stdin: system prompt + user input
  stdout: refined request text
```

### MCP tool inventory

| Tool | Source | Purpose |
|---|---|---|
| `jira.get_issue` | Jira MCP server | Read Jira ticket by key |
| `jira.search` | Jira MCP server | Search Jira by JQL |
| `github.get_issue` | GitHub MCP server | Read GitHub issue |
| `github.search_issues` | GitHub MCP server | Search GitHub issues |
| `web.fetch` | Web fetch MCP | Read any URL (design docs, wikis) |
| `slack.search` | Slack MCP server | Search Slack messages |
| `confluence.get_page` | Confluence MCP | Read Confluence page |
| `file.read` | Built-in | Read local file (for specs, READMEs) |

V2 starts with: **jira.get_issue + web.fetch + file.read**
(the tools we already have configured or can add quickly)

### System prompt

```
You are an intent resolution agent.  Your job: figure out what the
user wants done, gather enough context to write a precise instruction,
and output that instruction.

WORKFLOW:
1. Read the user's message.  It is usually a short reference like
   "完成P69-10" or "fix #42" or "implement the design at [url]".
2. Determine what external sources you need to read.
   - Jira ticket reference → call jira.get_issue
   - GitHub issue reference → call github.get_issue
   - URL → call web.fetch
   - File path → call file.read
3. Read the external source.
4. If the source references OTHER sources you need (e.g. a Jira ticket
   links to a Confluence design doc), fetch those too.
5. Once you have enough context, output a SINGLE block of plain text:
   the precise code operation instruction.

OUTPUT RULES:
- Plain text, no JSON, no markdown fences
- In ENGLISH (regardless of input language)
- List each required change as a numbered step
- Each step names: file path, operation, code element
- Self-contained — readable without seeing the original sources

CONSTRAINTS:
- Maximum 3 tool calls (stop gathering and produce best-effort output)
- Do NOT generate code — only describe what to change
- Do NOT add requirements beyond what the sources describe
- If the user's intent is genuinely ambiguous after reading all
  available sources, say what IS clear and note what is ambiguous
```

### Integration in orchestrator

Replace the V1 `_refine_request` method:

```python
def _refine_request(self, *, task, actor_name, issue_context, semantic_translation) -> str | None:
    # Skip conditions remain the same (precise input, long input, etc.)
    ...

    # V2: use Claude CLI with MCP tools instead of plain prompt
    from app.services.intent_resolution import resolve_intent

    result = resolve_intent(
        user_input=raw,
        pre_fetched_context=issue_context,  # V1 compat: pass what we already have
        translation=task.translation_json,
        source_tree_summary=source_tree_summary,
        allowed_tools=["jira.get_issue", "web.fetch", "file.read"],
        max_tool_calls=3,
        timeout_seconds=90,
    )
    return result.refined_text
```

### New service: `apps/backend/app/services/intent_resolution.py`

```python
def resolve_intent(
    *,
    user_input: str,
    pre_fetched_context: dict | None,
    translation: dict | None,
    source_tree_summary: str | None,
    allowed_tools: list[str],
    max_tool_calls: int = 3,
    timeout_seconds: float = 90.0,
) -> RefinedRequest:
    """Run the intent resolution agent via Claude Code CLI.

    Unlike V1's refine_request_cli which sends a single prompt and
    reads a single response, this function gives the CLI access to
    MCP tools so it can fetch additional context autonomously.
    """
```

Key differences from V1:
- `--allowedTools` flag in CLI args
- Timeout bumped to 90s (tool calls take time)
- Pre-fetched context is passed as "initial context" in the prompt,
  but the agent CAN fetch more
- `max_tool_calls` is enforced by the system prompt, not code

### Backward compatibility

- V1 `refine_request_cli` stays as-is — it becomes the **fallback**
  when Claude CLI doesn't have MCP tools configured
- `_refine_request` tries V2 first, falls back to V1 if MCP tools
  are not available, falls back to `_augment_request_with_context`
  if V1 also fails

```python
def _refine_request(self, ...) -> str | None:
    # Try V2 (agent with tools)
    try:
        return self._resolve_intent_v2(...)
    except MCPNotConfiguredError:
        pass

    # Fallback to V1 (single-shot prompt)
    try:
        return self._refine_request_v1(...)
    except Exception:
        pass

    # Final fallback: mechanical augmentation
    return None
```

## MCP server configuration

### Jira MCP server

Already used by the platform (tool gateway has `jira.get_issue`).
Need to expose it as an MCP server that Claude CLI can access.

Option A: Run a thin MCP server wrapper around the existing Jira client
Option B: Use an existing Jira MCP server package

### Web fetch

Claude Code has built-in web fetch capability or we can use the
`@anthropic-ai/mcp-server-fetch` package.

### File read

Claude Code has built-in file read.  Restrict to the knowledge source
path via `--allowedTools` filtering.

## Cost and latency budget

| Step | V1 | V2 |
|---|---|---|
| Refinement LLM call | 1 call, ~15s | 1 call with up to 3 tool uses, ~30-60s |
| Total pipeline | ~5 min | ~5.5 min |
| Token cost per task | ~2K input + 500 output | ~4K input + 1K output (tool results add tokens) |

Acceptable: +30s and +2K tokens for significantly higher success rate
on complex multi-source tasks.

## Milestone plan

| Phase | What | Effort |
|---|---|---|
| V2.0 | Agent loop with jira.get_issue only (replace V1 hardcoded Jira read) | S |
| V2.1 | Add web.fetch (resolve URLs in Jira descriptions) | S |
| V2.2 | Add file.read (read README, package.json for repo structure) | S |
| V2.3 | Add github.get_issue + slack.search | M |
| V2.4 | Add ask_user for ambiguous intent (requires async pipeline) | L |

V2.0 is the minimum viable change: same capability as V1 but now the
LLM **controls** the Jira fetch instead of receiving it passively.
This unblocks V2.1+ without further architectural changes.

## Test plan

1. **test_agent_fetches_jira_autonomously** — mock MCP, verify agent
   calls jira.get_issue and produces refined text
2. **test_agent_chains_jira_to_url** — Jira description contains a
   URL, verify agent calls web.fetch after jira.get_issue
3. **test_agent_respects_max_tool_calls** — verify agent stops after
   3 tool calls even if more context is available
4. **test_fallback_to_v1** — MCP not configured, verify V1 is used
5. **test_fallback_to_augment** — both V2 and V1 fail, verify
   mechanical augmentation is used
6. **test_timeout_handling** — agent takes too long, verify graceful
   fallback

## Non-goals

- Do NOT build a custom agent loop — use Claude CLI as the runtime
- Do NOT give the agent code generation tools — it only resolves intent
- Do NOT let the agent modify files — read-only tool access
- Do NOT remove V1 — keep it as fallback
