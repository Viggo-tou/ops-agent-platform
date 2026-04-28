from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from app.core.config import get_settings
from app.schemas.evidence import EvidenceItem
from app.services import cc_agent as cc_tools
from app.services.cc_agent import CCToolResult
from app.services.cc_agent_prompts import build_decision_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CCAgentBudget:
    max_rounds: int = 3
    max_tool_calls: int = 8
    overall_timeout_s: float = 30.0
    per_call_timeout_s: float = 20.0


@dataclass(frozen=True)
class CCAgentResult:
    evidence_items: list[EvidenceItem]
    rounds_run: int
    tool_calls_made: int
    duration_ms: int
    decision_model: str
    fallback_reason: str | None = None


class CCDecisionError(RuntimeError):
    pass


def run_cc_agent(
    query: str,
    *,
    cwd: Path,
    budget: CCAgentBudget,
    provider_chain: list[str] | None = None,
) -> CCAgentResult:
    if not cwd.exists():
        raise FileNotFoundError(f"source repo path does not exist: {cwd}")
    start = time.perf_counter()
    providers = provider_chain or _default_provider_chain()
    if not providers:
        providers = ["claude_code", "codex", "minimax"]

    evidence: list[EvidenceItem] = []
    history: list[CCToolResult] = []
    selected_provider: str | None = None
    invalid_json_count = 0
    rounds_run = 0
    tool_calls = 0

    for round_index in range(budget.max_rounds):
        if _elapsed_s(start) >= budget.overall_timeout_s:
            return _result(evidence, rounds_run, tool_calls, start, selected_provider, "overall_timeout")
        rounds_run = round_index + 1
        prompt = build_decision_prompt(query=query, tool_history=history, evidence_count=len(evidence))
        try:
            decision_text, selected_provider = _call_first_available_provider(
                providers,
                prompt=prompt,
                cwd=cwd,
                timeout_s=min(budget.per_call_timeout_s, max(1.0, budget.overall_timeout_s - _elapsed_s(start))),
            )
        except CCDecisionError:
            logger.warning("cc_agent all providers failed; falling back to RAG")
            return _result(evidence, rounds_run, tool_calls, start, selected_provider, "all_providers_failed")

        decision = _parse_decision_json(decision_text)
        if decision is None:
            invalid_json_count += 1
            if invalid_json_count >= 2:
                return _result(evidence, rounds_run, tool_calls, start, selected_provider, "invalid_json")
            continue
        invalid_json_count = 0

        if decision.get("done") is True:
            return _result(evidence, rounds_run, tool_calls, start, selected_provider, None)

        action = decision.get("action")
        if not isinstance(action, dict):
            continue
        if tool_calls >= budget.max_tool_calls:
            return _result(evidence, rounds_run, tool_calls, start, selected_provider, "budget_exhausted")

        tool_result = _dispatch_tool(action, cwd=cwd, timeout_s=budget.per_call_timeout_s)
        tool_calls += 1
        history.append(tool_result)
        if not tool_result.error:
            evidence.extend(_tool_result_to_evidence(tool_result))

    fallback_reason = "budget_exhausted" if tool_calls >= budget.max_tool_calls or rounds_run >= budget.max_rounds else None
    return _result(evidence, rounds_run, tool_calls, start, selected_provider, fallback_reason)


def _default_provider_chain() -> list[str]:
    raw = getattr(get_settings(), "cc_agent_provider_chain", "claude_code,codex,minimax")
    if isinstance(raw, str):
        providers = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        providers = [str(item).strip() for item in raw if str(item).strip()]
    return [provider for provider in providers if provider != "anthropic"]


def _call_first_available_provider(
    providers: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout_s: float,
) -> tuple[str, str]:
    failures: list[str] = []
    for provider in providers:
        try:
            return _call_decision_provider(provider, prompt=prompt, cwd=cwd, timeout_s=timeout_s), provider
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{provider}: {exc}")
            logger.warning("cc_agent decision provider failed provider=%s error=%s", provider, str(exc)[:300])
    raise CCDecisionError("; ".join(failures))


def _call_decision_provider(provider: str, *, prompt: str, cwd: Path, timeout_s: float) -> str:
    settings = get_settings()
    if provider == "claude_code":
        command = shutil.which(str(settings.claude_code_command))
        if not command:
            raise CCDecisionError("Claude Code CLI not found")
        args = str(getattr(settings, "claude_code_args", "") or "").split()
        if "-p" not in args and "--print" not in args:
            args.append("--print")
        if "--dangerously-skip-permissions" not in args:
            args.append("--dangerously-skip-permissions")
        cmd = [command, *args, "-"]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        return _run_cli_for_text(cmd, prompt=prompt, cwd=cwd, timeout_s=timeout_s, env=env)
    if provider == "codex":
        command = shutil.which(str(settings.codex_command))
        if not command:
            raise CCDecisionError("Codex CLI not found")
        cmd = [command, "exec", "--full-auto", "-"]
        return _run_cli_for_text(cmd, prompt=prompt, cwd=cwd, timeout_s=timeout_s, env=dict(os.environ))
    if provider == "minimax":
        if not settings.minimax_api_key:
            raise CCDecisionError("MiniMax API key is not configured")
        url = f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2"
        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {settings.minimax_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": getattr(settings, "semantic_translator_model", "MiniMax-M2.7"),
                "messages": [
                    {"role": "system", "content": "Return only valid JSON for the repository retrieval planner."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 1000,
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("choices", [{}])[0].get("message", {}).get("content", "")
    raise CCDecisionError(f"unsupported provider: {provider}")


def _run_cli_for_text(
    cmd: list[str],
    *,
    prompt: str,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str],
) -> str:
    try:
        result = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise CCDecisionError(f"timeout after {timeout_s}s") from exc
    except OSError as exc:
        raise CCDecisionError(str(exc)) from exc
    if result.returncode != 0:
        raise CCDecisionError(f"rc={result.returncode}: {(result.stderr or '').strip()[:500]}")
    return result.stdout or ""


def _parse_decision_json(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _dispatch_tool(action: dict[str, Any], *, cwd: Path, timeout_s: float) -> CCToolResult:
    tool = str(action.get("tool", "")).replace("cc_", "")
    args = action.get("args") if isinstance(action.get("args"), dict) else {}
    if tool == "glob":
        return cc_tools.cc_glob(str(args.get("pattern", "*")), cwd=cwd, timeout_s=timeout_s)
    if tool == "grep":
        return cc_tools.cc_grep(
            str(args.get("pattern", "")),
            cwd=cwd,
            file_glob=args.get("file_glob") if isinstance(args.get("file_glob"), str) else None,
            case_insensitive=bool(args.get("case_insensitive", True)),
            timeout_s=timeout_s,
        )
    if tool == "read":
        line_range = _coerce_line_range(args.get("line_range"))
        return cc_tools.cc_read(str(args.get("path", "")), cwd=cwd, line_range=line_range, timeout_s=timeout_s)
    return CCToolResult("grep", {"action": action}, [], None, 0, f"unsupported tool: {tool}")


def _coerce_line_range(value: Any) -> tuple[int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            start, end = int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
        if start > 0 and end >= start:
            return start, end
    return None


def _tool_result_to_evidence(result: CCToolResult) -> list[EvidenceItem]:
    source = f"cc_{result.tool}"
    items: list[EvidenceItem] = []
    if result.tool == "read" and result.matches:
        line_range = result.args.get("line_range")
        line_start = line_range[0] if isinstance(line_range, tuple) else None
        line_end = line_range[1] if isinstance(line_range, tuple) else None
        raw_text = result.raw_text or ""
        items.append(
            EvidenceItem(
                id=str(uuid4()),
                source=source,
                file_path=result.matches[0].path,
                line_start=line_start,
                line_end=line_end,
                snippet=raw_text[:6000],
                chunk_kind="line_window" if line_range else "module",
                retrieval_channel="cc_agent",
                confidence=1.0,
                metadata={"tool_args": result.args, "duration_ms": result.duration_ms},
            )
        )
        return items
    for match in result.matches:
        line_start = match.line
        items.append(
            EvidenceItem(
                id=str(uuid4()),
                source=source,
                file_path=match.path,
                line_start=line_start,
                line_end=line_start,
                snippet="",
                chunk_kind="grep_hit" if result.tool == "grep" else "module",
                retrieval_channel="cc_agent",
                confidence=0.9 if result.tool == "grep" else 0.75,
                metadata={"tool_args": result.args, "duration_ms": result.duration_ms},
            )
        )
    return items


def _result(
    evidence: list[EvidenceItem],
    rounds_run: int,
    tool_calls: int,
    start: float,
    selected_provider: str | None,
    fallback_reason: str | None,
) -> CCAgentResult:
    return CCAgentResult(
        evidence_items=evidence,
        rounds_run=rounds_run,
        tool_calls_made=tool_calls,
        duration_ms=int((time.perf_counter() - start) * 1000),
        decision_model=selected_provider or "",
        fallback_reason=fallback_reason,
    )


def _elapsed_s(start: float) -> float:
    return time.perf_counter() - start
