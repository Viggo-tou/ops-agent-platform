"""Failure diagnosis service.

When a task ends in FAILED / REJECTED / AWAITING_APPROVAL with rejected
diffs, this service reads the structured failure context (compile errors,
rejected patches, review verdict + findings) and asks the primary model to
produce a one-paragraph natural-language explanation:

  {
    "summary": "<one short Chinese sentence>",
    "root_cause": "<2-3 sentence technical explanation>",
    "likely_fix": "<2-3 sentence repair suggestion>",
    "confidence": "high" | "medium" | "low",
    "related_files": ["path/to/File.kt", ...]
  }

Result is persisted on task.latest_result_json.failure_diagnosis so the
existing AwaitingApprovalBlock React component picks it up automatically.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.task import Task as TaskModel

_DIAG_TIMEOUT_S = 60.0
_MAX_DIFF_CHARS = 4000
_MAX_COMPILE_ERRS = 8


def _backend_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _gather_failure_context(task_id: str) -> dict[str, Any]:
    """Pull whatever workspace artifacts exist for the failed task."""
    workspace_dir = _backend_root() / "data" / "agent_workspace" / str(task_id)
    attempts_dir = workspace_dir / "attempts"

    compile_errors: list[dict[str, Any]] = []
    rejected_diff_text: str | None = None
    rejected_reason: str | None = None

    if attempts_dir.is_dir():
        # Latest numeric attempt for compile.json.
        numeric_attempts = sorted(
            (d for d in attempts_dir.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda p: p.name,
        )
        if numeric_attempts:
            latest = numeric_attempts[-1]
            compile_path = latest / "compile.json"
            if compile_path.is_file():
                try:
                    cdata = json.loads(compile_path.read_text(encoding="utf-8"))
                    errs = cdata.get("errors") or []
                    for e in errs[:_MAX_COMPILE_ERRS]:
                        if isinstance(e, dict):
                            compile_errors.append(
                                {
                                    "file": str(e.get("file") or "")[:200],
                                    "line": e.get("line"),
                                    "message": str(e.get("error") or e.get("message") or "")[:300],
                                }
                            )
                except (json.JSONDecodeError, OSError):
                    pass

        rejected_dir = attempts_dir / "rejected"
        if rejected_dir.is_dir():
            rejected_files = sorted(rejected_dir.glob("rejected_*.patch"), key=lambda p: p.stat().st_mtime)
            if rejected_files:
                latest_rej = rejected_files[-1]
                try:
                    text = latest_rej.read_text(encoding="utf-8", errors="replace")
                    body_lines: list[str] = []
                    in_body = False
                    for line in text.splitlines():
                        if not in_body:
                            if line.startswith("# reason:"):
                                rejected_reason = line[len("# reason:"):].strip()[:300]
                            if line.startswith("# ---- raw diff"):
                                in_body = True
                                continue
                        else:
                            body_lines.append(line)
                    diff = "\n".join(body_lines).strip() or text
                    rejected_diff_text = diff[:_MAX_DIFF_CHARS]
                except OSError:
                    pass

    return {
        "compile_errors": compile_errors,
        "rejected_diff": rejected_diff_text,
        "rejected_reason": rejected_reason,
    }


def _format_context_for_prompt(task: TaskModel, context: dict[str, Any]) -> str:
    sections: list[str] = []
    sections.append(f"Task title: {task.title}")
    sections.append(f"Task scenario: {task.scenario}")
    sections.append(f"Task status: {task.status.value if hasattr(task.status, 'value') else task.status}")

    review = task.review_json or {}
    if isinstance(review, dict):
        verdict = review.get("verdict") or task.review_verdict
        summary = review.get("summary") or task.review_summary
        findings = review.get("findings")
        if verdict:
            sections.append(f"Review verdict: {verdict}")
        if summary:
            sections.append(f"Review summary: {str(summary)[:500]}")
        if isinstance(findings, list) and findings:
            sections.append("Review findings (first 5):")
            for f in findings[:5]:
                if isinstance(f, dict):
                    sev = f.get("severity") or "info"
                    msg = str(f.get("message") or f.get("description") or "")[:240]
                    sections.append(f"  - [{sev}] {msg}")

    if context["compile_errors"]:
        sections.append("\nCompile errors:")
        for e in context["compile_errors"]:
            file = e["file"] or "?"
            line = e.get("line", "?")
            sections.append(f"  - {file}:{line}: {e['message']}")

    if context["rejected_reason"]:
        sections.append(f"\nRejected diff reason: {context['rejected_reason']}")

    if context["rejected_diff"]:
        sections.append("\nRejected diff (truncated):\n```diff")
        sections.append(context["rejected_diff"])
        sections.append("```")

    request_text = (task.request_text or "")[:600]
    if request_text:
        sections.append(f"\nOriginal user request: {request_text}")

    return "\n".join(sections)


_DIAG_SYSTEM_PROMPT = """你是 Ops Agent 的失败诊断助手。

输入是一个失败的开发任务的 metadata 与工件 (review verdict / 编译错误 / 被拒绝的 diff / 用户原始请求)。
你要返回**严格的 JSON**(无任何额外文字、无 markdown 代码块包裹),格式:

{
  "summary": "一句话(20-50 字)说清楚这个任务为什么没成,中文",
  "root_cause": "2-3 句技术解释,中文,具体到 文件:行号 / 哪类编译错误 / 哪个 review finding",
  "likely_fix": "2-3 句修复建议,中文,告诉用户下一步怎么办(如:补充某 import / 改某文件 / 给 planner 更明确指令)",
  "confidence": "high | medium | low",
  "related_files": ["app/src/main/.../File.kt", ...]
}

confidence 规则:
- compile_errors 明确指向某文件某行 → high
- review verdict 是 reject/needs_info 但无具体定位 → medium
- 仅有模糊原因(如 stream timeout) → low

只返回 JSON。不要 markdown 包裹,不要解释,不要前缀。
"""


def _resolve_diag_provider() -> tuple[str, str]:
    """Pick the highest-priority streamable provider with credentials.

    Mirrors chat.py's chain except limited to non-streaming (we want a
    single JSON response). Order: DeepSeek → Anthropic → OpenAI → MiniMax → mock.
    """
    settings = get_settings()
    if getattr(settings, "deepseek_api_key", None):
        return "deepseek", "deepseek-chat"
    if getattr(settings, "anthropic_api_key", None):
        return "anthropic", "claude-sonnet-4-5"
    if getattr(settings, "openai_api_key", None):
        return "openai", "gpt-4o-mini"
    if getattr(settings, "minimax_api_key", None):
        return "minimax", "MiniMax-M2.7-highspeed"
    return "mock", "mock"


def _call_llm_for_diag(provider: str, model: str, context_str: str) -> str:
    settings = get_settings()
    if provider == "mock":
        return json.dumps(
            {
                "summary": "(mock) 任务因模拟失败而中止。",
                "root_cause": "Mock provider — no LLM call was made; this is a placeholder diagnosis.",
                "likely_fix": "Configure OPS_AGENT_DEEPSEEK_API_KEY (or another supported key) and retry.",
                "confidence": "low",
                "related_files": [],
            },
            ensure_ascii=False,
        )

    if provider == "minimax":
        url = f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2"
        headers = {
            "Authorization": f"Bearer {settings.minimax_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _DIAG_SYSTEM_PROMPT},
                {"role": "user", "content": context_str},
            ],
            "max_tokens": 800,
        }
        with httpx.Client(timeout=_DIAG_TIMEOUT_S) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""

    if provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "max_tokens": 800,
            "system": _DIAG_SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": context_str}],
        }
        with httpx.Client(timeout=_DIAG_TIMEOUT_S) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        blocks = data.get("content", []) or []
        return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))

    if provider in ("openai", "deepseek"):
        if provider == "openai":
            base = "https://api.openai.com/v1"
            api_key = settings.openai_api_key
        else:
            # Hardcode the OpenAI-compat URL — settings.deepseek_base_url is
            # set to api.deepseek.com/anthropic in this codebase for the
            # Anthropic-compat codegen path, which 404s on OpenAI-style calls.
            base = "https://api.deepseek.com/v1"
            api_key = settings.deepseek_api_key
        url = f"{base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": _DIAG_SYSTEM_PROMPT},
                {"role": "user", "content": context_str},
            ],
            "max_tokens": 800,
        }
        with httpx.Client(timeout=_DIAG_TIMEOUT_S) as client:
            r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""

    raise RuntimeError(f"unsupported diag provider {provider}")


def _parse_diag_json(raw: str) -> dict[str, Any]:
    """Extract the JSON object even when the model wraps it in ``` blocks."""
    text = (raw or "").strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present.
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Last-resort: greedy extract first {...} block.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("diagnosis JSON was not an object")

    confidence = str(obj.get("confidence") or "").lower().strip()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    related = obj.get("related_files") or []
    if not isinstance(related, list):
        related = []
    related = [str(x) for x in related if isinstance(x, str) and x.strip()][:8]

    return {
        "summary": str(obj.get("summary") or "").strip()[:240] or "未能产生诊断摘要。",
        "root_cause": str(obj.get("root_cause") or "").strip()[:600],
        "likely_fix": str(obj.get("likely_fix") or "").strip()[:600],
        "confidence": confidence,
        "related_files": related,
    }


class DiagnosticError(RuntimeError):
    """Raised when diagnosis can't be produced (no context, LLM error, parse fail)."""


def run_diagnostic(task_id: str) -> dict[str, Any]:
    """End-to-end: gather context, ask LLM, parse, persist on the task.

    Returns the parsed diagnosis object. Raises DiagnosticError on failure
    so the API layer can surface a clear error to the user.
    """
    db = SessionLocal()
    try:
        task = db.get(TaskModel, task_id)
        if task is None:
            raise DiagnosticError(f"task {task_id} not found")

        context = _gather_failure_context(task_id)
        has_signal = (
            bool(context["compile_errors"])
            or bool(context["rejected_diff"])
            or bool(task.review_verdict)
            or bool(task.review_summary)
        )
        if not has_signal:
            raise DiagnosticError(
                "no failure artifacts found (no compile errors, rejected diffs, or review findings)"
            )

        prompt_context = _format_context_for_prompt(task, context)
        provider, model = _resolve_diag_provider()
        try:
            raw = _call_llm_for_diag(provider, model, prompt_context)
        except httpx.HTTPStatusError as exc:
            raise DiagnosticError(f"{provider} {exc.response.status_code}: {exc.response.text[:200]}") from exc
        except httpx.RequestError as exc:
            raise DiagnosticError(f"{provider} network error: {exc}") from exc

        try:
            diag = _parse_diag_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DiagnosticError(f"could not parse {provider} response as JSON: {exc}") from exc

        # Persist on task.latest_result_json.failure_diagnosis (additive).
        existing = task.latest_result_json or {}
        if not isinstance(existing, dict):
            existing = {}
        existing["failure_diagnosis"] = {
            **diag,
            "_meta": {
                "provider": provider,
                "model": model,
            },
        }
        task.latest_result_json = existing
        db.commit()
        return diag
    finally:
        db.close()
