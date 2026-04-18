# Playwright MCP Setup (for Claude browser-mode self-testing)

## Why

During the 2026-04-15 session the user asked the agent to "open a browser mode" and self-drive the React frontend to prove new backend changes actually reach the UI (not just pass pytest). Claude Code does not have a built-in browser; the Model Context Protocol (MCP) server `@playwright/mcp` provides one.

With this MCP enabled, the agent can:
- navigate to `http://127.0.0.1:5173`
- click, fill forms, read DOM
- screenshot the page into `docs/ai/evidence/`
- assert on network responses (watch `/api/tasks` return something other than `Failed to fetch`)

This is the mechanism that unblocks honest E2E verification going forward. Today's E2E relies on curl (see `scripts/e2e_develop_approval.py`), which is enough to exercise the API but not the actual React render path.

## Install

```powershell
# Inside the repo root (elevated PowerShell not required)
npm i -g @playwright/mcp
npx playwright install chromium
```

## Register the MCP server with Claude Code

Claude Code reads MCP servers from `.mcp.json` at the repo root (or from the user-global `~/.claude.json`). Add the block below:

```json
{
  "mcpServers": {
    "playwright": {
      "command": "npx",
      "args": ["-y", "@playwright/mcp@latest"]
    }
  }
}
```

After saving, restart the Claude Code session. `mcp` commands should list a `playwright` entry.

## Smoke test

Once registered, ask the agent:

> Open http://127.0.0.1:5173 in Playwright, wait until the chat input is visible, then take a screenshot.

The response should include a screenshot path under `docs/ai/evidence/` (or wherever Playwright's MCP stores it). If the screenshot still shows "Failed to fetch" that itself is evidence the backend contract broke — do not declare the task complete until the UI renders the expected state.

## Using it in T-039 E2E

Future E2E sessions should follow this order rather than curl-only:

1. Start backend + frontend via `scripts/start-backend.ps1` / `scripts/start-web.ps1 -Dev`.
2. Playwright-navigate to `http://127.0.0.1:5173`.
3. Login as admin, type a develop request into the chat input.
4. Wait for the task to hit AWAITING_APPROVAL. Screenshot the diff/summary panel.
5. Click either "Grant" or "Reject" in the approval queue.
6. Screenshot the final state. Assert:
   - grant → task shows "Completed" + "Jira transitioned: yes"
   - reject → task shows "Completed" + "Jira transition rejected; code kept"

Each screenshot goes into `docs/ai/evidence/T-039/` with a timestamped filename. Those files are the retrospective artifact — the visual proof that the flow works end-to-end, not just in pytest.

## Security notes

- `@playwright/mcp` launches a real Chromium with the agent driving it. Treat any credentials the agent fills as visible to the MCP process.
- Do not point Playwright at production URLs.
- The MCP server inherits network access from the host; firewalls/proxies apply normally.
