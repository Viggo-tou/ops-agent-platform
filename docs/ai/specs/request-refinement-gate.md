# Spec: Request Refinement Gate

## Problem

When users submit vague requests from the frontend (e.g. "请Jira上的P69-10"),
the pipeline fails because the codegen CLI cannot determine what specific code
changes to make. The same Jira issue succeeds in ~66 seconds when submitted
with a precise request like:

> "delete the array element with id 'master1' from src/data/mockUsers.js,
> and move the localStorage.getItem('currentUser') call into a useEffect
> hook in src/pages/Dashboard.js"

Root cause: `_augment_request_with_context` (service.py:828) mechanically
concatenates the user's raw text + JSON dumps.  The planner and codegen
CLI inherit the vagueness.  Codex CLI times out after 600s.

## What this node does (cognitive chain)

The user's input is typically a **reference**, not an **instruction**.
The actual task details live somewhere else — a Jira ticket, a GitHub
issue, a Slack thread, a Confluence doc, a URL.  The refinement node's
job is to **resolve the reference into concrete operations**.

### Generic pattern

```
User types anything:
  "完成P69-10"              → reference to a Jira ticket
  "fix issue #42"           → reference to a GitHub issue
  "implement the design doc"→ reference to a Confluence/Google doc
  "do what we discussed"    → reference to a Slack thread
  "按照这个链接改"            → reference to a URL
        ↓
Refinement LLM observes the input
        ↓
Decides what external context it needs
        ↓ calls Jira API? GitHub API? Slack MCP? URL fetch? multiple?
Fetches the actual task content via tools/APIs/MCPs
        ↓
Reads the fetched content and outputs precise code operation instructions
        ↓
This precise text replaces the user's original input
for the planner and all downstream stages.
```

The LLM's job is: **figure out what the user is referring to, go
read it, and translate what it says into specific file-level code
operations.**  It does not plan, does not generate code, does not
review.  It is an **intent resolution agent** with tool access.

### V1 scope: Jira (this spec)

In V1, the pipeline has already fetched the Jira issue context before
this node runs, so the refinement LLM receives it as input rather
than fetching it itself.  This is a simplification — future versions
will give the LLM direct tool access to resolve arbitrary references.

```
Step 1 — User input arrives:  "完成P69-10"
              (this is just a task reference, not a code instruction)

Step 2 — Pipeline has already fetched the Jira ticket P69-10:
              Title: "Data and role cleanup"
              Description: "Delete the master_admin mock user,
                move the localStorage.getItem into useEffect..."
              Acceptance criteria: [...]

Step 3 — Refinement LLM reads BOTH, and outputs:
              "In the handyman-admin-dashboard repository:
               1. Delete the array element with id 'master1' from
                  src/data/mockUsers.js
               2. In src/pages/Dashboard.js, move the
                  localStorage.getItem('currentUser') call at line 27
                  into a React useEffect hook with useState"

Step 4 — This precise text replaces the user's original input
              for the planner and all downstream stages.
```

### Future versions (V2+)

The refinement node becomes a proper agent with tool access:

| Version | Capability |
|---|---|
| V1 (this spec) | Reads pre-fetched Jira context, translates to operations |
| V2 | LLM gets MCP tools: Jira, GitHub, Slack — fetches context itself |
| V3 | LLM can chain tools (read Jira → follow link to Confluence → read design doc) |
| V4 | LLM asks the user clarifying questions when intent is truly ambiguous |

The architecture in this spec is designed so V1→V2 is a **parameter
change** (pass tool handles to the CLI prompt), not a rewrite.

## Solution

Add a **request refinement** step — a single CLI call positioned after
Jira context fetch and before planning.  The LLM reads the user's raw
input alongside the fetched Jira ticket content, then outputs a precise,
actionable request text that replaces `planning_request_text` for the
rest of the pipeline.

## Architecture

### New file: `apps/backend/app/services/request_refinement.py`

Single module, two public functions (API backend + CLI backend), same
dual-backend pattern as `semantic_review.py`.

```python
@dataclass(frozen=True)
class RefinedRequest:
    refined_text: str       # The precise, actionable request
    confidence: float       # 0.0-1.0
    raw_response: str       # Raw LLM response for debugging (truncated)
```

#### `refine_request_cli(...)` — primary backend

```python
def refine_request_cli(
    *,
    user_input: str,
    jira_context: dict | None,
    translation: dict | None,
    source_tree_summary: str | None = None,
    claude_command: str = "npx",
    claude_args: str = "--yes @anthropic-ai/claude-code",
    timeout_seconds: float = 60.0,
) -> RefinedRequest:
```

Uses `claude -p` via subprocess, same pattern as `run_semantic_review_cli`.
Timeout: **60 seconds** (this is a lightweight prompt, not codegen).

#### `refine_request_api(...)` — API backend (when api_key is set)

```python
def refine_request_api(
    *,
    user_input: str,
    jira_context: dict | None,
    translation: dict | None,
    source_tree_summary: str | None = None,
    api_key: str,
    base_url: str = "https://api.anthropic.com",
    model: str = "claude-sonnet-4-20250514",
    timeout_seconds: float = 30.0,
) -> RefinedRequest:
```

Uses httpx POST to Anthropic Messages API, same pattern as
`run_semantic_review`.

### System prompt

```
You are an intent resolution engine.  You sit between a user and a
code generation pipeline.

Your job: the user's input is a REFERENCE to a task, not the task
itself.  The actual task details have been fetched from an external
system (Jira, GitHub, Slack, etc.) and provided to you below.

Read the external context carefully, then produce a precise
engineering instruction that a code generator can execute without
ever seeing the original source.

You are NOT a planner, NOT a code generator, NOT a reviewer.
You are a TRANSLATOR: external task description → code operations.

INPUT you will receive:
- The user's raw message (short — often just a ticket/issue reference)
- External context: the fetched task content (Jira ticket, GitHub
  issue, Slack thread, etc.) with title, description, and criteria
- A semantic translation with intent and candidate modules
- (Optional) The repository file tree so you can name exact paths

OUTPUT rules — produce a SINGLE block of plain text that:

1. Is in ENGLISH (regardless of user's input language)
2. Starts with the repository or project name if known
3. Lists each required change as a numbered step
4. Each step MUST name:
   - The SPECIFIC file path (e.g. src/data/mockUsers.js)
   - The EXACT operation (delete, modify, add, move, rename, extract)
   - The specific code element (function name, variable, array element,
     CSS class, config key) being changed
5. Is self-contained — a developer reading ONLY your output should
   know exactly what to change, without seeing the original task

HOW to derive file paths:
- If the task description mentions file names → use them directly
- If it mentions component/feature names → match to the file tree
- If neither → state the operation and note "exact file TBD"

Do NOT:
- Add implementation suggestions beyond what the task description asks
- Wrap output in JSON or markdown fences
- Include pleasantries, preamble, or explanations
- Repeat the task description verbatim — TRANSLATE it into operations
- Invent requirements that are not in the task description
```

### Prompt construction

```python
def _build_refinement_prompt(
    *,
    user_input: str,
    jira_context: dict | None,
    translation: dict | None,
    source_tree_summary: str | None,
) -> str:
```

Build a user prompt with these sections.  **External Task Context is
the largest and most important section** — the LLM's primary job is
reading it and translating it into code operations:

```
## User Request (what the user typed — usually just a reference)
{user_input}

## External Task Context (the ACTUAL task — read this carefully)
Source: {source_type}            ← "jira" | "github" | "slack" | etc.
Key: {key}
Summary: {summary}
Status: {status}
Type: {issue_type}
Priority: {priority}

### Full Description
{description}                    ← full text, NOT truncated

### Acceptance Criteria          ← if present in the description
{acceptance_criteria}

## Semantic Translation (system's understanding of intent)
Objective: {objective}
Intent: {intent}
Candidate Modules: {candidate_modules}
Constraints: {constraints}

## Repository File Tree (use this to name exact file paths)
{source_tree_summary or "Not available — state operations without exact paths"}
```

**V1 implementation**: `source_type` is always `"jira"` and the context
fields map from the Jira issue dict (`issue_context`).  Future versions
will populate this section from GitHub, Slack, Confluence, etc.

**Critical**: pass the description in FULL (up to 4000 chars).
The current `_augment_request_with_context` truncates it to 1200 chars
which often cuts off acceptance criteria — that's part of why the
current approach fails.

The `source_tree_summary` is optional — when available, it helps the
LLM match Jira's feature/component references to actual file paths.
Generate it from the knowledge source path:

```python
# In the orchestrator, before calling refine:
source_path = self._resolve_knowledge_source_path()
if source_path:
    tree = _build_source_tree_summary(source_path, max_depth=3, max_entries=200)
    source_tree_summary = tree
```

### Response parsing

The response is plain text, not JSON.  Trim whitespace and use directly:

```python
refined_text = raw_response.strip()
# Basic sanity check
if len(refined_text) < 20:
    raise ValueError("Refinement response too short")
```

Confidence: not extracted from the LLM — set to `0.8` as default.  
The caller checks `len(refined_text) > 0` as the pass condition.

## Integration point in orchestrator

### File: `apps/backend/app/orchestrator/service.py`

#### Location: `_bootstrap_task_impl`, after line 191, before line 193

Currently:
```python
            planning_knowledge_context = self._prefetch_planning_repository_context(...)

            planning_request_text = self._augment_request_with_context(
                original_request=task.request_text,
                ...
            )
```

Change to:
```python
            planning_knowledge_context = self._prefetch_planning_repository_context(...)

            # --- Request refinement gate ---
            refined = self._refine_request(
                task=task,
                actor_name=actor_name,
                issue_context=issue_context,
                semantic_translation=semantic_translation,
            )
            if refined is not None:
                planning_request_text = refined
            else:
                # Fallback: use existing mechanical augmentation
                planning_request_text = self._augment_request_with_context(
                    original_request=task.request_text,
                    translation_document=task.translation_json,
                    issue_context=issue_context,
                    planning_knowledge_context=planning_knowledge_context,
                )
```

#### New method on `PrimaryOrchestrator`:

```python
def _refine_request(
    self,
    *,
    task: Task,
    actor_name: str,
    issue_context: dict | None,
    semantic_translation: GeneratedSemanticTranslation,
) -> str | None:
    """Refine a vague user request into precise code modification instructions.

    Returns the refined text, or None if refinement is skipped or fails
    (caller falls back to _augment_request_with_context).
    """
    # Skip refinement when user input is already precise enough
    # Heuristic: if the raw request mentions specific file paths or is
    # longer than 200 chars, it's probably precise already
    raw = (task.request_text or "").strip()
    if len(raw) > 200 or re.search(r'\.\w{2,4}\b', raw):  # has file extension
        record_event(...)  # TOOL_SKIPPED — request already precise
        return None

    # Build source tree summary
    source_tree_summary = None
    source_path = self._resolve_knowledge_source_path()
    if source_path and source_path.is_dir():
        source_tree_summary = _build_source_tree_summary(source_path)

    record_event(...)  # REQUEST_REFINEMENT_STARTED

    try:
        from app.services.request_refinement import refine_request_cli
        result = refine_request_cli(
            user_input=raw,
            jira_context=issue_context,
            translation=semantic_translation.model_dump(mode="json"),
            source_tree_summary=source_tree_summary,
        )
        record_event(...)  # REQUEST_REFINEMENT_COMPLETED
        return result.refined_text
    except Exception as exc:
        record_event(...)  # REQUEST_REFINEMENT_FAILED — log and fall back
        return None
```

#### Source tree helper (add to orchestrator or to request_refinement module):

```python
def _build_source_tree_summary(source_path: Path, max_depth: int = 3, max_entries: int = 200) -> str:
    """List source tree files up to max_depth, capped at max_entries."""
    entries = []
    for item in sorted(source_path.rglob("*")):
        if len(entries) >= max_entries:
            entries.append("... (truncated)")
            break
        try:
            rel = item.relative_to(source_path)
        except ValueError:
            continue
        if len(rel.parts) > max_depth:
            continue
        # Skip common noise dirs
        if any(p in {".git", "node_modules", "__pycache__", ".next", "dist", "build", "coverage"} for p in rel.parts):
            continue
        entries.append(str(rel).replace("\\", "/"))
    return "\n".join(entries)
```

## Event types

Use existing event types — no new enums needed:

| Event | Type |
|---|---|
| Start | `EventType.TOOL_CALL_REQUESTED` with tool_name=`"request_refinement"` |
| Success | `EventType.TOOL_SUCCEEDED` with payload containing `refined_text[:500]` |
| Skipped | `EventType.TOOL_SKIPPED` with message "Request already precise" |
| Failed | `EventType.TOOL_FAILED` with error detail |

## Skip conditions

Do NOT run refinement when:
1. User input already contains a file extension pattern (e.g. `.js`, `.py`)
   — user is already specifying files, no resolution needed
2. User input is longer than 200 characters (likely already detailed)
3. No external context was fetched (`issue_context` is None or `_synthetic`)
   — nothing to resolve against, refinement would be guessing

V1 additionally skips when `task.scenario != "jira_issue_develop"`
since Jira develop is the only pipeline that currently fetches external
context before this point.  Remove this check as other sources are added.

## Fallback behavior

If the CLI call fails, times out, or returns empty text:
- Log `TOOL_FAILED` event with error
- Return `None` — caller falls back to `_augment_request_with_context`
- Pipeline continues normally (refinement is **non-blocking**)

## Test plan

### Unit tests: `apps/backend/tests/services/test_request_refinement.py`

1. **test_build_refinement_prompt_with_jira** — verify prompt includes Jira summary, description, and file tree
2. **test_build_refinement_prompt_without_jira** — verify graceful handling when jira_context is None
3. **test_parse_response_plain_text** — verify plain text response is trimmed and returned
4. **test_parse_response_too_short** — verify ValueError when response < 20 chars
5. **test_skip_when_precise** — verify _refine_request returns None when input contains file paths
6. **test_skip_when_long** — verify _refine_request returns None when input > 200 chars
7. **test_skip_when_synthetic_context** — verify skip when issue_context has `_synthetic=True`

### Integration test: `apps/backend/tests/orchestrator/test_request_refinement_integration.py`

1. **test_vague_input_triggers_refinement** — mock CLI to return refined text, verify `planning_request_text` uses it
2. **test_precise_input_skips_refinement** — verify no CLI call when input has file paths
3. **test_cli_failure_falls_back** — mock CLI to raise, verify fallback to `_augment_request_with_context`

## File inventory

| Action | File |
|---|---|
| CREATE | `apps/backend/app/services/request_refinement.py` |
| MODIFY | `apps/backend/app/orchestrator/service.py` (add `_refine_request` method + call site) |
| CREATE | `apps/backend/tests/services/test_request_refinement.py` |
| CREATE | `apps/backend/tests/orchestrator/test_request_refinement_integration.py` |

## Non-goals

- Do NOT change the SemanticTranslator — it handles intent classification, not request precision
- Do NOT change `_augment_request_with_context` — it stays as the fallback
- Do NOT make this step blocking — always degrade gracefully
- Do NOT add new EventType enum values — reuse existing TOOL_* types
- Do NOT add new API endpoints — this is internal pipeline only
