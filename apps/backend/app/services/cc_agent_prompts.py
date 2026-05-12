from __future__ import annotations

from app.services.cc_agent import CCToolResult


DECISION_SYSTEM_PROMPT = """You are a repository retrieval planner.

You may request exactly one tool call per turn:
- cc_glob: find files by glob pattern
- cc_grep: search text and identifiers
- cc_read: read a repository-relative file, optionally with line_range

Return only JSON. Use one of these shapes:
{"thought":"why this is next","action":{"tool":"grep","args":{"pattern":"Login","file_glob":"*.js"}}}
{"thought":"enough evidence","done":true}

Rules:
- Prefer grep for identifiers, filenames, function names, and concrete phrases.
- Use glob to locate likely files when the path is unknown.
- Use read after grep/glob identifies a specific source file.
- Paths must be repository-relative.
- Do not use tools outside cc_glob, cc_grep, cc_read.
"""


def build_decision_prompt(
    *,
    query: str,
    tool_history: list[CCToolResult],
    evidence_count: int,
) -> str:
    lines = [
        DECISION_SYSTEM_PROMPT,
        "",
        f"User query: {query}",
        f"Evidence items collected: {evidence_count}",
    ]
    if tool_history:
        lines.append("")
        lines.append("Tool history:")
        for index, result in enumerate(tool_history[-6:], start=1):
            status = f"error={result.error}" if result.error else f"matches={len(result.matches)}"
            args = ", ".join(f"{key}={value!r}" for key, value in sorted(result.args.items()))
            lines.append(f"{index}. {result.tool}({args}) -> {status}")
            if result.matches:
                preview = ", ".join(
                    f"{match.path}:{match.line}" if match.line else match.path
                    for match in result.matches[:5]
                )
                lines.append(f"   preview: {preview}")
    lines.append("")
    lines.append("Next JSON:")
    return "\n".join(lines)
