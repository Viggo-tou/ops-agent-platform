"""Streaming chat endpoint with intent classification.

POST /api/chat/send (text/event-stream)

The primary model both:
  1. Answers conversational questions directly (streams tokens back).
  2. Decides whether the user message is actually a TASK (file changes,
     deployment, Jira issues). When yes, it emits a marker line:
         TASK_INTENT|<scenario>|<one-line summary>
     The server detects this AFTER the stream ends and kicks off a
     real Task via the existing TaskService.create_task() path.

For pure questions (no marker), we still persist a lightweight Task
(scenario=process_question, status=completed) so the chat history
remains queryable from /tasks.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Annotated, AsyncGenerator

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal, get_db
from app.core.enums import (
    ActorRole,
    EventSource,
    EventType,
    RiskCategory,
    RiskLevel,
    RoleName,
    TaskStatus,
    WorkflowStage,
)
from app.core.security import ActorContext, require_permission
from app.models.task import Task as TaskModel
from app.schemas.task import TaskCreateRequest
from app.services.events import record_event
from app.services.model_config import get_selected_model
from app.services.runtime_override import effective_provider
from app.services.tasks import TaskService
from app.models.model_config import ModelEntry as ModelEntryModel

router = APIRouter(prefix="/chat", tags=["chat"])
DbSession = Annotated[Session, Depends(get_db)]
ChatActorCtx = Annotated[ActorContext, Depends(require_permission("task:create"))]


class ChatSendRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_name: str | None = Field(default=None, min_length=1, max_length=64)
    actor_name: str | None = Field(default=None, max_length=100)
    previous_task_id: str | None = Field(default=None, min_length=1, max_length=64)


# Kept short so it streams predictably and the model rarely fakes the marker.
SYSTEM_PROMPT = """You are an Ops Agent. Read the user message and decide:

1. QUESTION (info / status / "what's available" / "how do I..."): just answer
   directly and conversationally. Be concise; use markdown when helpful.

2. TASK that requires real action (modify code, deploy, create or transition
   Jira issues, run CI, etc.): respond with EXACTLY one line of marker, then
   stop:

       TASK_INTENT|<scenario>|<short one-line summary>

   where <scenario> is one of:
     - jira_issue_develop  (write or modify code)
     - jira_issue_plan     (plan only, no code yet)
     - process_question    (ambiguous — let the user clarify)

   You may follow the marker line with at most 1-2 sentences of natural
   confirmation to the user (e.g. "I'll set up a task to do this").

When in doubt, treat as a question and answer normally.
"""


_DEFAULT_MAX_TOKENS = 1024


@router.post("/send")
async def chat_send(
    body: ChatSendRequest,
    actor: ChatActorCtx,
) -> StreamingResponse:
    settings = get_settings()
    chain = _resolve_provider_chain(settings)

    # ActorContext exposes actor_role + app_role; "name" isn't on it. Default
    # to body.actor_name (sent by ChatPage) and fall back to "operator".
    actor_name = (body.actor_name or "operator").strip() or "operator"
    actor_role_value = (getattr(actor, "actor_role", None) or "employee").lower()
    try:
        actor_role = ActorRole(actor_role_value)
    except ValueError:
        actor_role = ActorRole.EMPLOYEE
    session_id = (body.session_id or "").strip() or str(uuid.uuid4())

    async def stream() -> AsyncGenerator[bytes, None]:
        full_text = ""
        used_provider: str | None = None
        used_model: str | None = None
        last_error: str | None = None

        for idx, (provider, model_name) in enumerate(chain):
            full_text = ""
            try:
                yield _sse(
                    "session",
                    {
                        "session_id": session_id,
                        "provider": provider,
                        "model": model_name,
                        "fallback_attempt": idx,
                    },
                )
                async for chunk in _stream_from_provider(
                    provider, body.message, settings, model_name
                ):
                    if not chunk:
                        continue
                    full_text += chunk
                    yield _sse("token", {"text": chunk})
                used_provider = provider
                used_model = model_name
                break
            except Exception as exc:  # noqa: BLE001
                last_error = f"{provider}: {exc}"
                yield _sse(
                    "provider_failed",
                    {"provider": provider, "model": model_name, "error": str(exc)[:300]},
                )
                continue

        if used_provider is None:
            yield _sse(
                "error",
                {"message": f"all providers failed: {last_error or 'no providers configured'}"},
            )
            return
        # Mark which provider/model produced the answer.
        _ = used_model  # available for future "answered_by" SSE event

        intent = _parse_task_intent(full_text)

        # Persist a Task row either way so chat history is queryable.
        try:
            if intent is None:
                # Pure question: lightweight completed Task, no pipeline.
                task_id = await asyncio.to_thread(
                    _persist_question_task,
                    message=body.message,
                    answer_text=full_text,
                    session_id=session_id,
                    actor_name=actor_name,
                    actor_role=actor_role,
                    previous_task_id=body.previous_task_id,
                )
                yield _sse(
                    "task_created",
                    {
                        "task_id": task_id,
                        "scenario": "process_question",
                        "kicked_off_pipeline": False,
                    },
                )
            else:
                scenario, summary = intent
                # Real task: full pipeline via existing TaskService path.
                task_id = await asyncio.to_thread(
                    _persist_task_intent_task,
                    message=body.message,
                    summary=summary,
                    scenario=scenario,
                    session_id=session_id,
                    actor_name=actor_name,
                    actor_role=actor_role,
                    previous_task_id=body.previous_task_id,
                    source_name=body.source_name,
                )
                yield _sse(
                    "task_created",
                    {
                        "task_id": task_id,
                        "scenario": scenario,
                        "kicked_off_pipeline": True,
                        "summary": summary,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            yield _sse(
                "error",
                {"message": f"persistence failed: {exc}", "answer_kept": True},
            )

        yield _sse("end", {"length": len(full_text)})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------- provider streaming -------------------------------------------------


_STREAMABLE_PROVIDERS = {"anthropic", "openai", "minimax", "deepseek", "mock"}

# Default models per provider when the user hasn't picked one in /settings.
_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
    "minimax": "MiniMax-M2.7-highspeed",
    "deepseek": "deepseek-chat",
    "mock": "mock",
}


def _resolve_selected_model(settings) -> tuple[str | None, str | None]:
    """Read the user's selected (provider, model_name) from Settings page.

    Returns (None, None) if nothing selected or DB read fails — caller will
    then fall back to its own provider chain.
    """
    db: Session = SessionLocal()
    try:
        selected = get_selected_model(db)
        model_id = selected.model_id if selected else None
        if not model_id:
            return None, None
        entry = db.get(ModelEntryModel, model_id)
        if entry is None:
            return None, None
        return entry.provider_name, entry.id
    except Exception:  # noqa: BLE001 — defensive, never break chat over a DB blip
        return None, None
    finally:
        db.close()


def _resolve_provider_chain(settings) -> list[tuple[str, str]]:
    """Build a fallback chain so a single broken provider (out of credit /
    rate-limit / 5xx) doesn't kill chat.

    Returns a list of (provider, model_name) tuples in priority order.

    Order:
      1. User's currently-selected model in /settings (provider+model).
      2. Per-stage override for primary_agent (if streamable) with its default model.
      3. Other streamable providers whose credentials are set with default models.
      4. Mock (always last so we never 500).
    """
    chain: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(provider: str, model: str) -> None:
        key = (provider, model)
        if provider in _STREAMABLE_PROVIDERS and key not in seen:
            chain.append(key)
            seen.add(key)

    # 1. Settings → selected model (user's explicit pick wins).
    sel_provider, sel_model = _resolve_selected_model(settings)
    if sel_provider and sel_model:
        add(sel_provider, sel_model)

    # 2. runtime_overrides for primary_agent.
    raw = effective_provider("primary_agent", settings.primary_agent_provider) or "auto"
    if raw != "auto" and raw in _STREAMABLE_PROVIDERS:
        add(raw, _DEFAULT_MODEL.get(raw, raw))

    # 3. Any other streamable provider with credentials.
    if getattr(settings, "anthropic_api_key", None):
        add("anthropic", _DEFAULT_MODEL["anthropic"])
    if getattr(settings, "openai_api_key", None):
        add("openai", _DEFAULT_MODEL["openai"])
    if getattr(settings, "deepseek_api_key", None):
        add("deepseek", _DEFAULT_MODEL["deepseek"])
    if getattr(settings, "minimax_api_key", None):
        add("minimax", _DEFAULT_MODEL["minimax"])

    # 4. Mock as terminal fallback.
    add("mock", "mock")
    return chain


async def _stream_from_provider(
    provider: str, message: str, settings, model_name: str
) -> AsyncGenerator[str, None]:
    if provider == "mock":
        async for chunk in _stream_mock(message):
            yield chunk
        return
    if provider == "minimax":
        async for chunk in _stream_minimax(message, settings, model_name):
            yield chunk
        return
    if provider == "anthropic":
        async for chunk in _stream_anthropic(message, settings, model_name):
            yield chunk
        return
    if provider == "openai":
        async for chunk in _stream_openai(message, settings, model_name):
            yield chunk
        return
    if provider == "deepseek":
        async for chunk in _stream_deepseek(message, settings, model_name):
            yield chunk
        return
    # Unknown / not yet implemented (claude_code / codex / ollama).
    # Raise so the chain falls through to the next provider.
    raise RuntimeError(f"streaming not implemented for provider '{provider}'")


async def _stream_mock(message: str) -> AsyncGenerator[str, None]:
    text = f"(mock) you said: {message}"
    for ch in text:
        yield ch
        await asyncio.sleep(0.005)


async def _stream_minimax(message: str, settings, model_name: str) -> AsyncGenerator[str, None]:
    if not getattr(settings, "minimax_api_key", None):
        raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name or "MiniMax-M2.7-highspeed",
        "stream": True,
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    }
    url = f"{settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                raise RuntimeError(
                    f"minimax {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                )
            async for line in response.aiter_lines():
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].lstrip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content


async def _stream_anthropic(message: str, settings, model_name: str) -> AsyncGenerator[str, None]:
    if not getattr(settings, "anthropic_api_key", None):
        raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY not set")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name or "claude-sonnet-4-5",
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "stream": True,
        "messages": [{"role": "user", "content": message}],
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        async with client.stream(
            "POST", "https://api.anthropic.com/v1/messages", headers=headers, json=payload
        ) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                raise RuntimeError(
                    f"anthropic {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].lstrip()
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "content_block_delta":
                    delta = data.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield text


async def _stream_deepseek(message: str, settings, model_name: str) -> AsyncGenerator[str, None]:
    """DeepSeek's API is OpenAI-compatible at api.deepseek.com/v1."""
    if not getattr(settings, "deepseek_api_key", None):
        raise RuntimeError("OPS_AGENT_DEEPSEEK_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    base_url = getattr(settings, "deepseek_base_url", None) or "https://api.deepseek.com"
    payload = {
        "model": model_name or "deepseek-chat",
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    }
    # base_url may already include /v1 (e.g. https://api.deepseek.com/v1).
    # Only prepend /v1 when it doesn't.
    base = base_url.rstrip("/")
    url = f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                raise RuntimeError(
                    f"deepseek {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].lstrip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content


async def _stream_openai(message: str, settings, model_name: str) -> AsyncGenerator[str, None]:
    if not getattr(settings, "openai_api_key", None):
        raise RuntimeError("OPS_AGENT_OPENAI_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_name or "gpt-4o-mini",
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        async with client.stream(
            "POST",
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
        ) as response:
            if response.status_code >= 400:
                err_body = await response.aread()
                raise RuntimeError(
                    f"openai {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                )
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].lstrip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    yield content


# ---------- intent parsing + persistence ---------------------------------------


def _parse_task_intent(text: str) -> tuple[str, str] | None:
    """Detect the marker line. Tolerates leading whitespace / surrounding text."""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("TASK_INTENT|"):
            continue
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        scenario = parts[1].strip()
        summary = parts[2].strip() if len(parts) >= 3 else ""
        if scenario:
            return scenario, summary
    return None


_VALID_SCENARIOS = {"jira_issue_develop", "jira_issue_plan", "process_question"}


def _sse(event_name: str, data: dict) -> bytes:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event_name}\ndata: {payload}\n\n".encode("utf-8")


def _sse_strip_intent(text: str) -> str:
    """Strip the TASK_INTENT line from the visible answer (kept server-side only)."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.strip().startswith("TASK_INTENT|"):
            continue
        out.append(line)
    return "".join(out).strip()


def _persist_question_task(
    *,
    message: str,
    answer_text: str,
    session_id: str,
    actor_name: str,
    actor_role: ActorRole,
    previous_task_id: str | None,
) -> str:
    """Lightweight persistence: a single Task row marked completed.

    No pipeline kicked off. This keeps chat history queryable from
    /api/tasks (the chat sidebar already lists tasks by session_id).
    """
    visible_answer = _sse_strip_intent(answer_text)
    db: Session = SessionLocal()
    try:
        task = TaskModel(
            session_id=session_id,
            actor_name=actor_name,
            actor_role=actor_role,
            title=_truncate(message.splitlines()[0] if message else "chat", 80),
            request_text=message,
            scenario="process_question",
            status=TaskStatus.COMPLETED,
            workflow_stage=WorkflowStage.DONE if hasattr(WorkflowStage, "DONE") else WorkflowStage.REVIEW,
            current_role=RoleName.PRIMARY,
            risk_level=RiskLevel.LOW,
            risk_category=RiskCategory.KNOWLEDGE_LOOKUP,
            governance_json={
                "actor": {"name": actor_name, "role": actor_role.value},
                "risk": {"level": RiskLevel.LOW.value, "category": RiskCategory.KNOWLEDGE_LOOKUP.value},
                "policy_state": "skipped_question",
                "continuation": (
                    {"previous_task_id": previous_task_id} if previous_task_id else None
                ),
            },
            latest_result_json={
                "kind": "chat_answer",
                "answer": visible_answer,
                "raw_with_marker": answer_text,
            },
            pending_approval=False,
        )
        db.add(task)
        db.flush()
        # Intentionally do NOT record an EXECUTION_COMPLETED event for chat
        # answers — the Task row itself (status=completed + latest_result_json)
        # is the audit trail. Adding events makes the chat UI render a
        # "执行完成" badge between every Q-A turn, which the user explicitly
        # rejected as visual noise.
        db.commit()
        return task.id
    finally:
        db.close()


def _persist_task_intent_task(
    *,
    message: str,
    summary: str,
    scenario: str,
    session_id: str,
    actor_name: str,
    actor_role: ActorRole,
    previous_task_id: str | None,
    source_name: str | None,
) -> str:
    """Real task: route through TaskService so the pipeline kicks off."""
    if scenario not in _VALID_SCENARIOS:
        scenario = "jira_issue_develop"  # safe default for "do work"
    db: Session = SessionLocal()
    try:
        service = TaskService(db)
        payload = TaskCreateRequest(
            title=_truncate(summary or message, 200) or None,
            request=message,
            actor_name=actor_name,
            actor_role=actor_role,
            session_id=session_id,
            previous_task_id=previous_task_id,
            source_name=source_name,
        )
        task = service.create_task(payload)
        return task.id
    finally:
        db.close()


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s[: n - 1] + "…" if len(s) > n else s
