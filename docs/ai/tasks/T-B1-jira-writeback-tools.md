# T-B1 — Jira Writeback Tools (transition + comment)

## Goal

Add two new Jira tools to the existing tool gateway so the orchestrator can transition a Jira issue's status and post progress comments back to the ticket. This unblocks Phase B of the multi-agent MVP roadmap.

Read tools (`jira.get_issue`, `jira.create_issue`) already exist and must not be touched. This task only adds writeback and seeds governance policies for them.

## Background

Existing infrastructure to copy from:

- Tool registry: `apps/backend/app/tools/registry.py` — `jira_missing` guard, `self.settings.jira_*` fields, and the existing `"jira.get_issue"` / `"jira.create_issue"` `ToolDefinition` blocks. New tools must reuse the same `provider_name="jira"`, the same `enabled=not jira_missing` guard, the same timeouts/retries from settings, and the same `_status_message` helper.
- Tool gateway: `apps/backend/app/tools/gateway.py` — `_execute_jira_get_issue` and `_execute_jira_create_issue` show the auth pattern (`jira_bearer_token` first, then `jira_email + jira_api_token` basic auth), URL resolution via `_resolve_jira_site_root()`, and how `_request_json` is used with `retryable` error handling. Both new executors must follow the same shape.
- Dispatcher: the big `if definition.name == ...` ladder in `_execute_tool_impl` (around line 180). New entries must be added there.
- Governance seeds: `apps/backend/app/services/governance.py::DEFAULT_POLICY_RULES`. Copy the shape of the existing `jira.create_issue` / `jira.get_issue` entries to seed policy rules for the new tools. Writeback tools are gated on approval for `employee` and `team_lead` roles.
- `ToolPermissionCategory.APPROVAL_REQUIRED` already exists in `app/core/enums.py`.

Do NOT add new settings. Reuse `self.settings.jira_timeout_seconds` and `self.settings.jira_retry_count`. No new env vars.

## Jira REST endpoints

1. **Transition issue** — requires two HTTP calls:
   - `GET /rest/api/3/issue/{key}/transitions` → response has `transitions: [{id, name, to: {name, id, statusCategory}}]`. Find the transition whose `name` matches the requested transition name case-insensitively. If none matches, fail with `retryable=False` and include the list of available transition names in the error message so the user can retry with the right one.
   - `POST /rest/api/3/issue/{key}/transitions` with body `{"transition": {"id": <id>}}`. A successful transition returns `204 No Content`.
   - After a successful transition, fetch the new status by calling `GET /rest/api/3/issue/{key}?fields=status` (reusing the same helper that `jira.get_issue` uses for fields extraction) so the executor can return `from_status` and `to_status` in its result dict. `from_status` should be read BEFORE the transition POST so we have the original value.
2. **Add comment** — `POST /rest/api/3/issue/{key}/comment`. Body uses Atlassian Document Format (ADF). For plain text, the minimal envelope is:
   ```json
   {
     "body": {
       "type": "doc",
       "version": 1,
       "content": [
         {"type": "paragraph", "content": [{"type": "text", "text": "<the comment text>"}]}
       ]
     }
   }
   ```
   Successful response is `201` with a body that includes `id` and `created`. Return `comment_id`, `created` (as an ISO string), and a short `excerpt` (first 200 chars of the text) from the executor.

Both endpoints use the same auth pattern as the existing Jira tools.

## Files to edit

1. `apps/backend/app/tools/registry.py`
   - Add two `ToolDefinition` entries next to `jira.create_issue`:
     - `"jira.transition_issue"`:
       - `display_name="Jira Transition Issue"`
       - `description="Move a Jira issue to a new workflow status via the transitions API."`
       - `permission_category=ToolPermissionCategory.APPROVAL_REQUIRED`
       - `enabled=not jira_missing`, same `_status_message` + `missing_configuration` pattern
       - `requires_network=True`
       - `timeout_seconds=self.settings.jira_timeout_seconds`
       - `retry_count=max(0, self.settings.jira_retry_count)`
       - `tags=("jira", "workflow", "state-change")`
     - `"jira.add_comment"`:
       - `display_name="Jira Add Comment"`
       - `description="Post a comment to a Jira issue, visible to all watchers."`
       - Same flags except `tags=("jira", "workflow", "comment")`
       - `permission_category=ToolPermissionCategory.APPROVAL_REQUIRED`

2. `apps/backend/app/tools/gateway.py`
   - In the `_execute_tool_impl` dispatcher (around line 180), add:
     ```python
     if definition.name == "jira.transition_issue":
         return self._execute_jira_transition_issue(definition=definition, payload=payload)
     if definition.name == "jira.add_comment":
         return self._execute_jira_add_comment(definition=definition, payload=payload)
     ```
   - Add method `_execute_jira_transition_issue(self, *, definition, payload)`:
     - Resolve base URL and auth exactly like `_execute_jira_get_issue`.
     - Required payload: `issue_key: str`, `transition_name: str`. Normalize `issue_key` to upper-case, strip whitespace. Raise `ToolInvocationError` on missing fields.
     - Step 1: `GET /rest/api/3/issue/{key}?fields=status` → read `from_status`. If the fetch fails, raise with `retryable=True`.
     - Step 2: `GET /rest/api/3/issue/{key}/transitions` → parse `transitions` array, find case-insensitive name match. If none found, raise `ToolInvocationError` with a message that lists the available transition names and sets `retryable=False`.
     - Step 3: `POST /rest/api/3/issue/{key}/transitions` with `{"transition": {"id": transition_id}}`. Jira returns 204 on success; treat any 2xx as success.
     - Step 4: re-fetch `GET /rest/api/3/issue/{key}?fields=status` → read `to_status`.
     - Return:
       ```python
       {
           "status": "transitioned",
           "tool_name": definition.name,
           "provider": definition.provider_name,
           "issue_key": issue_key,
           "transition_id": transition_id,
           "transition_name": matched_name,
           "from_status": from_status,
           "to_status": to_status,
           "issue_url": f"{base_url.rstrip('/')}/browse/{issue_key}",
       }
       ```
   - Add method `_execute_jira_add_comment(self, *, definition, payload)`:
     - Same auth/base-url resolution.
     - Required payload: `issue_key: str`, `text: str`. Normalize; empty text → `ToolInvocationError`.
     - Build the ADF envelope around `text`.
     - `POST /rest/api/3/issue/{key}/comment` with the envelope body.
     - Parse `data.id`, `data.created` from the response.
     - Return:
       ```python
       {
           "status": "commented",
           "tool_name": definition.name,
           "provider": definition.provider_name,
           "issue_key": issue_key,
           "comment_id": comment_id,
           "created": created,
           "excerpt": text[:200],
           "issue_url": f"{base_url.rstrip('/')}/browse/{issue_key}",
       }
       ```
   - Both methods must reuse `self._request_json` (same signature used by the existing Jira executors — do not add a new HTTP helper).

3. `apps/backend/app/services/governance.py`
   - Add four `DEFAULT_POLICY_RULES` entries, mirroring the shape of the existing `jira.create_issue.employee.allow.v1` entry but routing writeback through approval:
     - `"jira.transition_issue.employee.approval.v1"`:
       - subject_role: `ActorRole.EMPLOYEE`
       - resource_type: `"jira"`, action_key: `"transition_issue"`, tool_name: `"jira.transition_issue"`
       - scope_selector: `"default"`
       - decision: `PolicyDecision.REQUIRE_APPROVAL`
       - risk_level: `RiskLevel.MEDIUM`
       - risk_category: `RiskCategory.CHANGE_MANAGEMENT`
       - required_approver_role: `ActorRole.TEAM_LEAD`
       - constraints_json: `{"writeback": True}`
       - metadata_json: `{"phase": "phase6"}`
       - priority: 40
       - is_active: True
     - `"jira.transition_issue.team_lead.allow.v1"`:
       - subject_role: `ActorRole.TEAM_LEAD`
       - same action/tool/resource
       - decision: `PolicyDecision.ALLOW_WITH_CONSTRAINTS`
       - risk_level: `RiskLevel.MEDIUM`
       - required_approver_role: `None`
       - constraints_json: `{"writeback": True, "requires_audit_note": True}`
       - priority: 45
     - `"jira.add_comment.employee.approval.v1"`:
       - subject_role: `ActorRole.EMPLOYEE`
       - resource_type/action/tool for `jira.add_comment`
       - decision: `PolicyDecision.REQUIRE_APPROVAL`
       - risk_level: `RiskLevel.LOW`
       - risk_category: `RiskCategory.EXTERNAL_BROADCAST`
       - required_approver_role: `ActorRole.TEAM_LEAD`
       - priority: 40
     - `"jira.add_comment.team_lead.allow.v1"`:
       - subject_role: `ActorRole.TEAM_LEAD`
       - decision: `PolicyDecision.ALLOW`
       - risk_level: `RiskLevel.LOW`
       - required_approver_role: `None`
       - priority: 45
   - The existing `seed_defaults` method is already idempotent and will upsert these on next startup.

## Files to create

4. `apps/backend/tests/tools/test_jira_writeback.py`
   - Unit tests using `unittest.mock` (stdlib). The existing test tree (if any) may already use pytest; if so, match its style. If there is no existing tests dir, create `apps/backend/tests/` with an empty `__init__.py` plus this test file and skip fixtures.
   - Stub `ToolGateway._request_json` to return canned responses. Tests:
     - `test_transition_issue_success` — happy path: 1st GET returns `{"fields": {"status": {"name": "To Do"}}}`, 2nd GET returns transitions with one named `"In Progress"` and id `"21"`, POST returns empty body, final GET returns `{"fields": {"status": {"name": "In Progress"}}}`. Assert the result dict has `status="transitioned"`, `from_status="To Do"`, `to_status="In Progress"`, `transition_id="21"`, `transition_name` matches (case-insensitive).
     - `test_transition_issue_unknown_transition` — transitions GET returns names `["Start Progress", "Done"]`, user requested `"In Progress"`. Assert `ToolInvocationError` with a message containing both available names, and `retryable=False`.
     - `test_transition_issue_missing_fields` — empty `issue_key` or empty `transition_name` raises `ToolInvocationError`.
     - `test_add_comment_success` — POST returns `{"id": "10101", "created": "2026-04-12T00:00:00.000+0000"}`. Assert the result has `comment_id="10101"`, `excerpt` equals first 200 chars.
     - `test_add_comment_missing_fields` — empty text raises.
   - Construct the gateway with a minimal mock `settings` object that exposes the Jira fields and timeouts. Do not spin up FastAPI.

## Acceptance criteria

- `python -m compileall app` from `apps/backend/` exits 0.
- The tool registry lists `jira.transition_issue` and `jira.add_comment` as enabled when Jira env vars are set; they're `APPROVAL_REQUIRED`.
- `GET /api/tools/registry` (the existing tool-registry endpoint) returns the two new tools in its payload.
- `python -m pytest apps/backend/tests/tools/test_jira_writeback.py -q` (or the stdlib `unittest` equivalent if pytest isn't available) passes all 5 tests.
- New `DEFAULT_POLICY_RULES` entries are present; after a fresh DB + backend startup, `GET /api/governance/policy-rules?tool_name=jira.transition_issue` returns 2 rules.
- No existing test or endpoint regresses (`/api/tools/registry` still returns the old 6 tools plus the 2 new ones).
- Save a smoke transcript of the pytest run and the `/api/tools/registry` response to `docs/ai/runs/T-B1.log`.

## Out of scope

- Chat orchestrator integration (wiring the scenario into `agents/service.py`) — that's T-B2 in the roadmap, not this task.
- Lifecycle event emission / Phase H templates — save for the chat rendering task.
- ADF rich-text (links, bold, headings). Plain-text paragraphs only.

## Workflow (for the executor, i.e. Codex)

1. Read `apps/backend/app/tools/registry.py`, `apps/backend/app/tools/gateway.py` (especially `_execute_jira_get_issue`, `_execute_jira_create_issue`, `_resolve_jira_site_root`, and `_request_json`), `apps/backend/app/services/governance.py` (especially `DEFAULT_POLICY_RULES` and `seed_defaults`), `apps/backend/app/core/enums.py` (for the Risk / PolicyDecision enums). Do NOT modify any of the existing executor methods.
2. Check whether `apps/backend/tests/` exists. If yes, inspect its layout and match it. If no, create the minimal structure described above.
3. Implement in order: registry → gateway dispatcher + two executors → governance policy seeds → tests.
4. Run `python -m compileall app` and the new tests. Save outputs to `docs/ai/runs/T-B1.log`.
5. Start the backend on a free port, `curl -s http://127.0.0.1:<port>/api/tools/registry | python -m json.tool` to confirm the new tools appear, append the response to `docs/ai/runs/T-B1.log`.
6. Do not touch any file outside the list above. Do not wire scenarios in `agents/service.py` — that's T-B2.

Invocation (from repo root):

```
codex exec --full-auto -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-B1-jira-writeback-tools.md
```
