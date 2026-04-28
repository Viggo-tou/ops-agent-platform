from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import EventSource, EventType, RoleName, WorkflowStage
from app.models.approval import Approval
from app.models.event import Event
from app.models.task import Task
from app.services.codegen import CodeGenerator, CodegenError
from app.services.events import record_event

logger = logging.getLogger("failure_diagnosis")

FailureKind = Literal[
    "compile_repair_cap_exceeded",
    "runtime_validation_cap_exceeded",
    "tool_failed_terminal",
    "approval_rejected",
]


class DiagnosisError(Exception):
    """Raised when diagnosis output cannot be generated or parsed."""


class EventSummary(BaseModel):
    event_type: str
    source: str | None = None
    stage: str | None = None
    role: str | None = None
    tool_name: str | None = None
    message: str
    payload: dict[str, Any] | None = None


class FailureContext(BaseModel):
    task_id: str
    scenario: str
    failure_kind: FailureKind
    last_n_events: list[EventSummary]
    repair_summary: dict[str, Any] | None = None
    residual_errors: list[str] = Field(default_factory=list)
    sandbox_dir: str | None = None
    sandbox_keyfiles: dict[str, str] = Field(default_factory=dict)
    plan_json: dict[str, Any] | None = None
    user_request: str
    user_language: str = "English"


class DiagnosisOutput(BaseModel):
    summary: str
    root_cause: str
    likely_fix: str
    confidence: Literal["high", "medium", "low"]
    related_files: list[str] = Field(default_factory=list)


def parse_diagnosis_output(content: str) -> DiagnosisOutput:
    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if not text.startswith("{"):
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            text = match.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DiagnosisError(f"Diagnosis response was not valid JSON: {exc}") from exc
    try:
        return DiagnosisOutput.model_validate(data)
    except ValidationError as exc:
        raise DiagnosisError(f"Diagnosis response did not match schema: {exc}") from exc


def build_failure_context(
    *,
    task: Task,
    db: Session,
    settings: Settings,
    failure_kind: FailureKind,
) -> FailureContext:
    max_events = int(getattr(settings, "failure_diagnosis_max_events", 30) or 30)
    stmt = (
        select(Event)
        .where(Event.task_id == task.id)
        .order_by(Event.created_at.desc())
        .limit(max_events)
    )
    raw_events = list(db.scalars(stmt))
    if len(raw_events) > max_events:
        raw_events = raw_events[:max_events]
    events = list(reversed(raw_events))
    event_summaries = [_summarize_event(event) for event in events]

    latest_result = task.latest_result_json if isinstance(task.latest_result_json, dict) else {}
    repair_summary = latest_result.get("result") if isinstance(latest_result.get("result"), dict) else None
    residual_errors = _collect_residual_errors(latest_result=latest_result, events=events)
    sandbox_dir = _resolve_sandbox_dir(task=task, latest_result=latest_result, settings=settings)

    return FailureContext(
        task_id=task.id,
        scenario=str(task.scenario or ""),
        failure_kind=failure_kind,
        last_n_events=event_summaries,
        repair_summary=repair_summary,
        residual_errors=residual_errors,
        sandbox_dir=str(sandbox_dir) if sandbox_dir else None,
        sandbox_keyfiles=_read_sandbox_keyfiles(sandbox_dir, settings=settings) if sandbox_dir else {},
        plan_json=task.plan_json if isinstance(task.plan_json, dict) else None,
        user_request=task.request_text or "",
        user_language=_detect_prompt_language(task.request_text or ""),
    )


def run_diagnosis(
    *,
    task: Task,
    db: Session,
    settings: Settings,
    failure_kind: FailureKind,
) -> DiagnosisOutput | None:
    if not getattr(settings, "failure_diagnosis_enabled", True):
        return None

    timeout = float(getattr(settings, "failure_diagnosis_timeout_seconds", 30.0) or 30.0)
    ctx = build_failure_context(task=task, db=db, settings=settings, failure_kind=failure_kind)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(_run_diagnosis_sync, ctx=ctx, settings=settings)
    try:
        diagnosis = future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        logger.warning("failure diagnosis timed out", extra={"task_id": task.id, "timeout": timeout})
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("failure diagnosis failed", extra={"task_id": task.id, "error": str(exc)[:300]})
        return None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    payload = diagnosis.model_dump(mode="json")
    latest_result = dict(task.latest_result_json) if isinstance(task.latest_result_json, dict) else {}
    latest_result["failure_diagnosis"] = payload
    task.latest_result_json = latest_result
    _attach_diagnosis_to_pending_approvals(task=task, db=db, payload=payload)

    record_event(
        db,
        task_id=task.id,
        event_type=EventType.FAILURE_DIAGNOSIS_GENERATED,
        source=EventSource.ORCHESTRATOR,
        stage=WorkflowStage.REVIEW,
        role=RoleName.REVIEWER,
        tool_name="failure_diagnosis",
        message=f"failure_diagnosis: {diagnosis.confidence} - {diagnosis.summary[:80]}",
        payload=payload,
    )
    logger.info("failure_diagnosis: %s - %s", diagnosis.confidence, diagnosis.summary[:80])
    return diagnosis


def _attach_diagnosis_to_pending_approvals(*, task: Task, db: Session, payload: dict[str, Any]) -> None:
    stmt = select(Approval).where(Approval.task_id == task.id)
    for approval in db.scalars(stmt):
        status = approval.status.value if hasattr(approval.status, "value") else str(approval.status)
        if status != "pending":
            continue
        request_payload = dict(approval.request_payload_json) if isinstance(approval.request_payload_json, dict) else {}
        request_payload["failure_diagnosis"] = payload
        approval.request_payload_json = request_payload


def _run_diagnosis_sync(*, ctx: FailureContext, settings: Settings) -> DiagnosisOutput:
    from app.services.failure_diagnosis_prompts import build_diagnostic_prompt

    prompt = build_diagnostic_prompt(ctx)
    generator = CodeGenerator(settings)
    providers = generator._resolve_provider_chain()
    errors: list[str] = []
    for provider in providers:
        try:
            content = _call_diagnosis_provider(provider=provider, prompt=prompt, settings=settings, generator=generator)
            if provider in {"anthropic", "minimax", "openai", "deepseek"}:
                logger.warning("diagnosis fell back to API provider %s - check why CLI failed", provider)
            return parse_diagnosis_output(content)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{provider}: {exc}")
            continue
    raise DiagnosisError("; ".join(errors) or "no diagnosis provider available")


def _call_diagnosis_provider(
    *,
    provider: str,
    prompt: str,
    settings: Settings,
    generator: CodeGenerator,
) -> str:
    if provider == "mock":
        return json.dumps(
            {
                "summary": "The task failed in the automated tooling and needs review.",
                "root_cause": "The mock provider cannot inspect a real failure context.",
                "likely_fix": "Review the captured events and tool errors.",
                "confidence": "low",
                "related_files": [],
            }
        )
    if provider == "claude_code":
        return _call_claude_code_text(prompt=prompt, settings=settings, generator=generator)
    if provider == "codex":
        return _call_codex_text(prompt=prompt, settings=settings)
    if provider == "anthropic":
        return _call_anthropic_text(prompt=prompt, settings=settings)
    if provider == "minimax":
        return _call_minimax_text(prompt=prompt, settings=settings)
    if provider == "openai":
        return _call_openai_compatible_text(
            prompt=prompt,
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.primary_agent_model,
            timeout=settings.primary_agent_timeout_seconds,
        )
    if provider == "deepseek":
        return _call_openai_compatible_text(
            prompt=prompt,
            base_url=settings.deepseek_base_url,
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            timeout=settings.deepseek_timeout_seconds,
        )
    if provider == "ollama":
        return _call_ollama_text(prompt=prompt, settings=settings)
    raise DiagnosisError(f"Unsupported diagnosis provider: {provider}")


def _summarize_event(event: Event) -> EventSummary:
    return EventSummary(
        event_type=event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type),
        source=event.source.value if event.source and hasattr(event.source, "value") else str(event.source or ""),
        stage=event.stage.value if event.stage and hasattr(event.stage, "value") else None,
        role=event.role.value if event.role and hasattr(event.role, "value") else None,
        tool_name=event.tool_name,
        message=event.message or "",
        payload=event.payload_json if isinstance(event.payload_json, dict) else None,
    )


def _collect_residual_errors(*, latest_result: dict[str, Any], events: list[Event]) -> list[str]:
    found: list[str] = []

    def add(value: object) -> None:
        if value is None:
            return
        if isinstance(value, str):
            text = value.strip()
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
        if text and text not in found:
            found.append(text)

    result = latest_result.get("result") if isinstance(latest_result.get("result"), dict) else {}
    for key in ("message", "error", "stderr", "stdout"):
        add(latest_result.get(key))
        add(result.get(key))
    for key in ("residual_compile_errors", "residual_errors", "errors"):
        value = result.get(key)
        if isinstance(value, list):
            for item in value:
                add(item)
    for event in events:
        payload = event.payload_json if isinstance(event.payload_json, dict) else {}
        for key in ("error", "stderr", "message"):
            add(payload.get(key))
        errors = payload.get("errors")
        if isinstance(errors, list):
            for item in errors:
                add(item)
    return found[:50]


def _resolve_sandbox_dir(*, task: Task, latest_result: dict[str, Any], settings: Settings) -> Path | None:
    result = latest_result.get("result") if isinstance(latest_result.get("result"), dict) else {}
    for value in (
        result.get("diff_path"),
        latest_result.get("sandbox_dir"),
        result.get("sandbox_dir"),
    ):
        if isinstance(value, str) and value.strip():
            return Path(value)
    base = Path(getattr(settings, "sandbox_base_dir", "data/sandboxes"))
    candidate = base / str(task.id)
    return candidate if candidate.exists() else None


def _read_sandbox_keyfiles(sandbox_dir: Path | None, *, settings: Settings) -> dict[str, str]:
    if sandbox_dir is None or not sandbox_dir.exists():
        return {}
    max_chars = int(getattr(settings, "failure_diagnosis_keyfile_head_chars", 500) or 500)
    names = {"package.json", "tsconfig.json", "jsconfig.json", ".env"}
    result: dict[str, str] = {}
    candidates = [sandbox_dir]
    try:
        candidates.extend([p for p in sandbox_dir.iterdir() if p.is_dir()])
    except OSError:
        return result
    for directory in candidates:
        for name in names:
            path = directory / name
            if not path.is_file():
                continue
            rel = path.relative_to(sandbox_dir).as_posix()
            if name == ".env":
                result[rel] = "<redacted: filename only>"
                continue
            try:
                result[rel] = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
            except OSError as exc:
                result[rel] = f"<unreadable: {exc}>"
    return result


def _detect_prompt_language(text: str) -> str:
    if not text:
        return "English"
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", text))
    non_space = len(text.replace(" ", ""))
    return "zh-CN" if non_space and cjk / non_space > 0.1 else "English"


def _call_claude_code_text(*, prompt: str, settings: Settings, generator: CodeGenerator) -> str:
    claude_cmd = shutil.which(settings.claude_code_command)
    if not claude_cmd:
        raise CodegenError(f"Claude Code CLI not found: {settings.claude_code_command}")
    workdir = tempfile.mkdtemp(prefix="ops_failure_diagnosis_claude_")
    try:
        stdout, stderr, rc = generator._run_claude_cli(
            claude_cmd=claude_cmd,
            instruction=prompt,
            workdir=workdir,
            context_files={},
        )
        if rc != 0:
            raise CodegenError(f"Claude Code CLI diagnosis failed (rc={rc}): {(stderr or '').strip()[:500]}")
        return _extract_cli_text(stdout)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _call_codex_text(*, prompt: str, settings: Settings) -> str:
    codex_cmd = shutil.which(settings.codex_command)
    if not codex_cmd:
        raise CodegenError(f"Codex CLI not found: {settings.codex_command}")
    workdir = tempfile.mkdtemp(prefix="ops_failure_diagnosis_codex_")
    try:
        result = subprocess.run(
            [codex_cmd, "exec", "--full-auto", "-"],
            input=prompt,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(settings.failure_diagnosis_timeout_seconds),
        )
        if result.returncode != 0:
            raise CodegenError(f"Codex CLI diagnosis failed (rc={result.returncode}): {(result.stderr or '')[:500]}")
        return _extract_cli_text(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise CodegenError(f"Codex CLI diagnosis timed out after {settings.failure_diagnosis_timeout_seconds}s") from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _extract_cli_text(stdout: str) -> str:
    text = (stdout or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        for key in ("result", "output", "content"):
            if isinstance(data.get(key), str):
                return data[key]
    return text


def _call_anthropic_text(*, prompt: str, settings: Settings) -> str:
    if not settings.anthropic_api_key:
        raise CodegenError("OPS_AGENT_ANTHROPIC_API_KEY is not configured.")
    body = {
        "model": settings.anthropic_model,
        "max_tokens": 2048,
        "system": "Return only JSON for the requested failure diagnosis.",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    response = httpx.post(
        f"{settings.anthropic_base_url.rstrip('/')}/v1/messages",
        json=body,
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        timeout=settings.failure_diagnosis_timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


def _call_minimax_text(*, prompt: str, settings: Settings) -> str:
    if not settings.minimax_api_key:
        raise CodegenError("OPS_AGENT_MINIMAX_API_KEY is not configured.")
    response = httpx.post(
        f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
        json={
            "model": getattr(settings, "semantic_translator_model", "MiniMax-Text-01"),
            "messages": [
                {"role": "system", "content": "Return only JSON for the requested failure diagnosis."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 4096,
        },
        headers={"Authorization": f"Bearer {settings.minimax_api_key}", "Content-Type": "application/json"},
        timeout=settings.failure_diagnosis_timeout_seconds,
    )
    response.raise_for_status()
    return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")


def _call_openai_compatible_text(
    *,
    prompt: str,
    base_url: str,
    api_key: str | None,
    model: str,
    timeout: float,
) -> str:
    if not api_key:
        raise CodegenError("API key is not configured.")
    response = httpx.post(
        f"{base_url.rstrip('/')}/chat/completions",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": "Return only JSON for the requested failure diagnosis."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        },
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")


def _call_ollama_text(*, prompt: str, settings: Settings) -> str:
    response = httpx.post(
        f"{settings.ollama_base_url.rstrip('/')}/chat/completions",
        json={
            "model": settings.ollama_model,
            "messages": [
                {"role": "system", "content": "Return only JSON for the requested failure diagnosis."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
        },
        timeout=settings.failure_diagnosis_timeout_seconds,
    )
    response.raise_for_status()
    return response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
