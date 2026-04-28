from __future__ import annotations

import json

from app.services.failure_diagnosis import FailureContext


def _json_block(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def build_diagnostic_prompt(ctx: FailureContext) -> str:
    plan_summary = ctx.plan_json or {}
    events_table = "\n".join(
        (
            f"- {event.event_type}"
            f" | stage={event.stage or ''}"
            f" | role={event.role or ''}"
            f" | tool={event.tool_name or ''}"
            f" | message={event.message}"
            f" | payload={_json_block(event.payload) if event.payload else '{}'}"
        )
        for event in ctx.last_n_events
    )
    if not events_table:
        events_table = "(no recent events)"

    residual_errors = "\n".join(f"- {error}" for error in ctx.residual_errors)
    if not residual_errors:
        residual_errors = "(no raw tool errors captured)"

    sandbox_keyfiles_block = _json_block(ctx.sandbox_keyfiles) if ctx.sandbox_keyfiles else "{}"
    language = ctx.user_language

    return f"""You are a senior engineer doing a post-mortem on a failed automated coding task.

The system attempted: {ctx.user_request}

It ran this plan: {_json_block(plan_summary)}

The pipeline failed at: {ctx.failure_kind}

Last events:
{events_table}

Tool errors (raw):
{residual_errors}

Sandbox key files:
{sandbox_keyfiles_block}

Your job:
1. Identify the ACTUAL root cause, even if the gate's blamed file is wrong.
2. Distinguish "target file is broken" from "external/sandbox state is broken"
   from "LLM output was wrong" from "tool config is missing".
3. Write a 1-2 sentence summary in {language} that a non-engineer
   reviewer can act on.
4. List the files a human should LOOK AT (not necessarily edit).
5. State your confidence - "high" only if the root cause is unambiguous from
   the data; "low" if you're guessing.

Output JSON matching this schema:
{{
  "summary": "1-2 sentence user-facing summary",
  "root_cause": "2-4 technical sentences",
  "likely_fix": "2-4 concrete next-step sentences",
  "confidence": "high | medium | low",
  "related_files": ["path/to/file"]
}}

Do not output prose outside the JSON."""
