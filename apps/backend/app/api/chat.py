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
from app.services.chat_intent import IntentResult, classify_intent
from app.services.docs_router import (
    RouterMatch,
    find_matching as find_matching_playbooks,
    format_playbooks_for_prompt,
    record_miss as record_playbook_miss,
)
from app.services.events import record_event
from app.services.knowledge import KnowledgeService
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


_BASE_SYSTEM_PROMPT = """你是 **运维代理** (Ops Agent),一个多阶段代码变更平台的对话入口。除非用户先切英文,默认中文回答。

# 何时**立刻**输出 TASK_INTENT(不要再问)

只要满足以下**任意一条**,就直接输出 marker,不要再追问:

1. 用户提到具体 **Jira 单号**(任何符合 `[A-Z]+-\d+` 的 token)+ 动作词(完成 / 实现 / 修 / fix / develop / implement)→ `jira_issue_develop`
2. 用户给出**具体文件路径**或**报错信息**让你处理 → `jira_issue_develop`
3. 用户已经在前面说过任务内容,这一轮只是补全细节(如 "开发任务" / "对" / "yes" / "go") → 用前文确定的 scenario,**不要重新问**

只有当**完全没有目标**(没单号、没文件、没问题)时,才用 `process_question` 让用户澄清。

**禁止**(常见 bug,逐条避免):
- ❌ 用户已经给了 Jira 单号 + 动作词,你还问"是开发还是规划" — 默认开发
- ❌ 用户回答"开发任务"后,你还继续追问"想做什么" — 这是在循环,**直接 emit marker**
- ❌ 把已说过的内容当没说过 — 对话历史就在 messages 里,**要回头看 user/assistant 历史 turn 才下一句的判断**
- ❌ **不要把这份 system 里的示例占位符当成用户真实说过的内容**(这是 prompt,不是用户输入)


# 回答风格(硬要求,逐条对照)

1. **结构化优先**:超过 2 个要点必须用 `1.` `2.` `3.` 或 `- ` 列表,不要堆成长句子。
2. **重点加粗**:产品名、命令、文件名、关键概念一律 `**粗体**`,不要全句加粗。
3. **段落短**:每段 1-2 句,段落之间空行分开。**先给答案,再解释**,不要绕弯。
4. **代码块**:命令 / 路径 / 配置 / 代码片段用 ``` ``` 或 `inline`,不要用裸文本。
5. **口语化但专业**:像同事聊天。**不要**用 "您可以" / "请知悉" / "希望对您有帮助" 这类公文腔。
6. **不要 emoji**,除非用户先用了。
7. **中文标点**(,。!?),英文回答时用英文标点。

# 你能直接回答(从下面"当前平台状态"取真实数据,不要装看不见)

- 已注册的代码库、最近添加、同步时间
- 任务列表、待审批数、运行中数、最近活动
- 工具就绪度 / 集成连接状态
- 解释概念、查文档、复述对话上下文里的代码

# 你能通过 TASK_INTENT 让后端 pipeline 执行

- 修代码 / 实现功能 / 修编译错误 → `jira_issue_develop`
- 只规划不写码 → `jira_issue_plan`
- 不明确,需澄清 → `process_question`

Pipeline 在沙箱里有 `codegen.generate_patch` / `sandbox.apply_patch` / `sandbox.run_command` / `diff_reviewer.review` / `knowledge.search` / `test_pipeline.run` / `jira.*` 等工具。

**关键:你本人没有终端,但 pipeline 有。** 用户问 "能不能帮我 clone 仓库 / 改代码 / 跑测试" 时,**不要**说"我没权限",改说 "**会创建任务交给 pipeline 执行**",然后输出 TASK_INTENT。

# TASK_INTENT 输出格式

要委托任务时,**单独一行**输出:

```
TASK_INTENT|<scenario>|<一句话总结>
```

之后可以再写 1-2 句对用户的确认。

纯问答时,**不要**输出 marker。
"""


def _build_system_prompt(db: Session) -> str:
    """Inject live platform state into the system prompt so the model can
    answer factual 'what's currently available?' questions without hallucinating
    'I can't see your environment'.
    """
    out = [_BASE_SYSTEM_PROMPT]

    # 1. Registered repositories.
    try:
        from app.services.repository_registry import list_all_sources_for_api
        sources = list_all_sources_for_api() or []
        if sources:
            lines = ["", "## 当前平台状态", "", f"### 已注册代码库 ({len(sources)} 个)"]
            for s in sources:
                desc = (s.get("description") or "").strip()
                origin = s.get("origin") or ""
                name = s.get("name") or "(unnamed)"
                lines.append(f"- **{name}** [{origin}]" + (f" — {desc}" if desc else ""))
            out.append("\n".join(lines))
        else:
            out.append("\n## 当前平台状态\n\n### 已注册代码库\n(暂无)")
    except Exception:  # noqa: BLE001 — never let a stale state break chat
        pass

    # 2. Tasks: counts only — full list goes via the /tasks page.
    try:
        from app.models.task import Task as _Task
        from sqlalchemy import func, select
        running_count = db.scalar(
            select(func.count(_Task.id)).where(
                _Task.status.in_([TaskStatus.RUNNING, TaskStatus.EXECUTING])
            )
        ) or 0
        pending_count = db.scalar(
            select(func.count(_Task.id)).where(_Task.pending_approval == True)  # noqa: E712
        ) or 0
        total_count = db.scalar(select(func.count(_Task.id))) or 0
        out.append(
            f"\n### 任务概况\n"
            f"- 总任务: {total_count}\n"
            f"- 运行中: {running_count}\n"
            f"- 待审批: {pending_count}"
        )
    except Exception:  # noqa: BLE001
        pass

    # 3. Tool readiness.
    try:
        from app.tools.gateway import ToolGateway
        gateway = ToolGateway(db)
        entries = gateway.list_registry_entries()
        ready = sum(1 for e in entries if getattr(e, "enabled", False) and not getattr(e, "missing_configuration", []))
        partial = sum(
            1 for e in entries
            if getattr(e, "enabled", False) and getattr(e, "missing_configuration", [])
        )
        unavailable = sum(1 for e in entries if not getattr(e, "enabled", True))
        out.append(
            f"\n### 工具就绪度 (总 {len(entries)} 个)\n"
            f"- 就绪: {ready}\n"
            f"- 部分可用 (缺配置): {partial}\n"
            f"- 不可用: {unavailable}"
        )
    except Exception:  # noqa: BLE001
        pass

    return "\n".join(out)


# Backwards-compat: some helper code references the bare SYSTEM_PROMPT name.
SYSTEM_PROMPT = _BASE_SYSTEM_PROMPT


# ---- routing helpers ------------------------------------------------------


def _light_fts_retrieve(query: str, top_k: int = 5) -> list[dict[str, str]]:
    """Lightweight FTS over knowledge_document_fts — no LLM, no rewrite,
    no synthesis. Aimed at <100ms for chat fast-path.

    Returns list of {source_name, relative_path, title, snippet}.
    Empty list on any error or no match.
    """
    import re as _re
    db = SessionLocal()
    try:
        # Tokenize the query into FTS5-safe terms. Each gets quoted and
        # joined by OR so partial matches work for symbol-style queries.
        tokens = [t for t in _re.findall(r"[\w一-鿿]{2,}", query or "")][:8]
        if not tokens:
            return []
        safe = " OR ".join('"{}"'.format(t.replace('"', '""')) for t in tokens)
        match_expr = (
            f"(relative_path:({safe}) OR title:({safe}) "
            f"OR content:({safe}) OR card_text:({safe}))"
        )
        from sqlalchemy import text as _text
        rows = db.execute(
            _text(
                """
                SELECT document_id, source_name, relative_path, title,
                       snippet(knowledge_document_fts, 4, '<mark>', '</mark>', '...', 32) AS snip
                FROM knowledge_document_fts
                WHERE knowledge_document_fts MATCH :match
                ORDER BY rank
                LIMIT :limit
                """
            ),
            {"match": match_expr, "limit": top_k},
        ).fetchall()
        out: list[dict[str, str]] = []
        for r in rows:
            out.append(
                {
                    "document_id": str(r[0] or ""),
                    "source_name": str(r[1] or ""),
                    "relative_path": str(r[2] or ""),
                    "title": str(r[3] or ""),
                    "snippet": str(r[4] or "").replace("<mark>", "**").replace("</mark>", "**"),
                }
            )
        return out
    except Exception:  # noqa: BLE001 — never break chat over an FTS hiccup
        return []
    finally:
        db.close()


def _format_lite_knowledge_for_prompt(rows: list[dict[str, str]], max_chars: int = 3500) -> str:
    if not rows:
        return ""
    parts: list[str] = ["", "## 检索到的代码 / 文档片段(优先依据这些回答,无证据的事实不要说)"]
    used = 0
    for i, r in enumerate(rows):
        block = (
            f"\n[{i + 1}] `{r.get('source_name','')}:{r.get('relative_path','')}` — "
            f"{r.get('title','')}\n```\n{r.get('snippet','')[:500]}\n```"
        )
        if used + len(block) > max_chars and parts:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _augment_system_prompt(
    base: str,
    intent: IntentResult,
    playbook_matches: list[RouterMatch],
    knowledge_block: str,
) -> str:
    """Layer-on routing context onto the base system prompt.

    Order matters — playbooks first (they tell the model HOW to think),
    then knowledge snippets (the actual evidence).
    """
    layers: list[str] = [base]
    layers.append(
        f"\n## 当前消息意图判定\n"
        f"intent={intent.intent}, confidence={intent.confidence}, "
        f"signals={','.join(intent.signals[:6])}"
    )
    if playbook_matches:
        layers.append(format_playbooks_for_prompt(playbook_matches))
    if knowledge_block:
        layers.append(knowledge_block)
    return "\n".join(layers)


# Caps prior conversation context. Last N user/assistant pairs, total under
# ~6KB so we don't blow up the context window on long chats.
_MAX_HISTORY_PAIRS = 6
_MAX_HISTORY_CHARS = 6000


def _load_session_history(db: Session, session_id: str | None) -> list[dict[str, str]]:
    """Reconstruct prior chat turns in this session as OpenAI-style messages.

    Each prior Task with the same session_id contributes:
      { role: "user", content: <task.request_text> }
    plus, when an answer exists,
      { role: "assistant", content: <task.latest_result_json.answer or summary> }

    Returned in chronological order (oldest first), capped at the most recent
    _MAX_HISTORY_PAIRS pairs and ~_MAX_HISTORY_CHARS total. The current
    incoming message is NOT included — caller appends it.
    """
    if not session_id:
        return []

    rows = (
        db.query(TaskModel)
        .filter(TaskModel.session_id == session_id)
        .order_by(TaskModel.created_at.asc())
        .all()
    )
    if not rows:
        return []

    pairs: list[tuple[str, str | None]] = []
    for t in rows:
        user_text = (t.request_text or "").strip()
        if not user_text:
            continue
        # Strip the long CONTINUATION FROM PARENT preamble from iterated
        # tasks so the assistant's history view shows the user's actual
        # follow-up and not the auto-generated context block.
        if user_text.startswith("[CONTINUATION FROM PARENT TASK"):
            marker = "[USER FOLLOWUP / NEW REQUEST]:"
            idx = user_text.find(marker)
            if idx >= 0:
                user_text = user_text[idx + len(marker):].strip()

        assistant_text: str | None = None
        result = t.latest_result_json
        if isinstance(result, dict):
            if result.get("kind") == "chat_answer":
                ans = result.get("answer")
                if isinstance(ans, str) and ans.strip():
                    assistant_text = ans.strip()
            elif "message" in result and isinstance(result.get("message"), str):
                # Heavy task: summarize as a one-line confirmation rather
                # than dumping the full plan/diff into context.
                assistant_text = f"(已创建任务 {t.id[:8]}, 状态: {t.status.value if hasattr(t.status, 'value') else t.status})"
        pairs.append((user_text, assistant_text))

    pairs = pairs[-_MAX_HISTORY_PAIRS:]

    # Trim from the FRONT until total chars is under the cap.
    def total_chars(ps: list[tuple[str, str | None]]) -> int:
        return sum(len(u) + len(a or "") for u, a in ps)

    while pairs and total_chars(pairs) > _MAX_HISTORY_CHARS:
        pairs.pop(0)

    messages: list[dict[str, str]] = []
    for user_text, assistant_text in pairs:
        messages.append({"role": "user", "content": user_text})
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
    return messages


_DEFAULT_MAX_TOKENS = 1024


@router.post("/send")
async def chat_send(
    body: ChatSendRequest,
    actor: ChatActorCtx,
) -> StreamingResponse:
    settings = get_settings()
    chain = _resolve_provider_chain(settings)

    # Build the system prompt once with live platform state. Uses a short-lived
    # DB session because the request handler doesn't have one (Depends would
    # close before the streaming response finishes).
    _state_db = SessionLocal()
    try:
        base_system_prompt = _build_system_prompt(_state_db)
        # Load prior turns from the same session so the model can resolve
        # follow-up references like "对" / "开发任务" without forgetting
        # what the user already said.
        history = _load_session_history(_state_db, body.session_id)
    finally:
        _state_db.close()

    # ---- Routing pipeline (rule-based, no extra LLM call) -----------------
    intent = classify_intent(body.message, history=history)

    # Docs router: any project-execution intent benefits from a playbook.
    playbook_matches: list[RouterMatch] = []
    if intent.intent in ("develop_task", "find_in_docs", "repo_question"):
        playbook_matches = find_matching_playbooks(body.message, top_k=2)
        if not playbook_matches and intent.intent == "find_in_docs":
            # Cold-start signal — log so we know what playbooks to add next.
            record_playbook_miss(body.message, intent.intent, signals=intent.signals)

    # Knowledge search: only for repo_question (file/symbol/structure lookups).
    # We use a LIGHT FTS-only path (no LLM rewrite, no synthesis) so it adds
    # well under 100ms. The full KnowledgeService.search_repositories pipeline
    # — with rewrite + cc-agent + synthesis — is too slow for an interactive
    # chat reply.
    knowledge_block = ""
    if intent.intent == "repo_question":
        try:
            rows = await asyncio.to_thread(_light_fts_retrieve, body.message, 5)
            knowledge_block = _format_lite_knowledge_for_prompt(rows)
        except Exception:  # noqa: BLE001
            knowledge_block = ""

    system_prompt = _augment_system_prompt(
        base_system_prompt, intent, playbook_matches, knowledge_block
    )

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
                    provider, body.message, settings, model_name, system_prompt, history
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

    # 3. Any other streamable provider with credentials. Order is the auto
    # priority — DeepSeek goes first (per user preference), then the other
    # API providers, then MiniMax as last resort.
    if getattr(settings, "deepseek_api_key", None):
        add("deepseek", _DEFAULT_MODEL["deepseek"])
    if getattr(settings, "anthropic_api_key", None):
        add("anthropic", _DEFAULT_MODEL["anthropic"])
    if getattr(settings, "openai_api_key", None):
        add("openai", _DEFAULT_MODEL["openai"])
    if getattr(settings, "minimax_api_key", None):
        add("minimax", _DEFAULT_MODEL["minimax"])

    # 4. Mock as terminal fallback.
    add("mock", "mock")
    return chain


async def _stream_from_provider(
    provider: str,
    message: str,
    settings,
    model_name: str,
    system_prompt: str,
    history: list[dict[str, str]] | None = None,
) -> AsyncGenerator[str, None]:
    history = history or []
    if provider == "mock":
        async for chunk in _stream_mock(message):
            yield chunk
        return
    if provider == "minimax":
        async for chunk in _stream_minimax(message, settings, model_name, system_prompt, history):
            yield chunk
        return
    if provider == "anthropic":
        async for chunk in _stream_anthropic(message, settings, model_name, system_prompt, history):
            yield chunk
        return
    if provider == "openai":
        async for chunk in _stream_openai(message, settings, model_name, system_prompt, history):
            yield chunk
        return
    if provider == "deepseek":
        async for chunk in _stream_deepseek(message, settings, model_name, system_prompt, history):
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


async def _stream_minimax(message: str, settings, model_name: str, system_prompt: str, history: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    if not getattr(settings, "minimax_api_key", None):
        raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    payload = {
        "model": model_name or "MiniMax-M2.7-highspeed",
        "stream": True,
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "messages": messages,
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


async def _stream_anthropic(message: str, settings, model_name: str, system_prompt: str, history: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    if not getattr(settings, "anthropic_api_key", None):
        raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY not set")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    # Anthropic's API rejects two consecutive same-role messages and requires
    # the conversation alternates user/assistant. The history loader returns
    # tasks in chronological order; if a task didn't produce an assistant
    # answer we collapse adjacent user turns.
    msgs: list[dict[str, str]] = []
    for entry in history:
        if msgs and msgs[-1]["role"] == entry["role"]:
            msgs[-1]["content"] = msgs[-1]["content"] + "\n\n" + entry["content"]
        else:
            msgs.append(dict(entry))
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = msgs[-1]["content"] + "\n\n" + message
    else:
        msgs.append({"role": "user", "content": message})
    payload = {
        "model": model_name or "claude-sonnet-4-5",
        "max_tokens": _DEFAULT_MAX_TOKENS,
        "system": system_prompt,
        "stream": True,
        "messages": msgs,
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


async def _stream_deepseek(message: str, settings, model_name: str, system_prompt: str, history: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    """DeepSeek's OpenAI-compatible streaming endpoint.

    NOTE: We hardcode the URL to api.deepseek.com/v1/chat/completions instead
    of using settings.deepseek_base_url. The codebase's existing config sets
    OPS_AGENT_DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic for the
    Anthropic-compatible codegen path; that endpoint returns 404 for
    OpenAI-style requests. Chat needs the OpenAI-compat URL.
    """
    if not getattr(settings, "deepseek_api_key", None):
        raise RuntimeError("OPS_AGENT_DEEPSEEK_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.deepseek_api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    payload = {
        "model": model_name or "deepseek-chat",
        "stream": True,
        "messages": messages,
    }
    url = "https://api.deepseek.com/v1/chat/completions"
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


async def _stream_openai(message: str, settings, model_name: str, system_prompt: str, history: list[dict[str, str]]) -> AsyncGenerator[str, None]:
    if not getattr(settings, "openai_api_key", None):
        raise RuntimeError("OPS_AGENT_OPENAI_API_KEY not set")
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})
    payload = {
        "model": model_name or "gpt-4o-mini",
        "stream": True,
        "messages": messages,
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

    Retries on 'database is locked' — chat persistence racing the heavy
    orchestrator's bulk writes is the most common source of dropped chat
    messages. We try up to 5 times with exponential backoff (50ms, 100ms,
    200ms, 400ms, 800ms) before giving up. The longest possible wait is
    ~1.5s which is acceptable for an interactive chat reply.
    """
    import time as _time
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            return _persist_question_task_once(
                message=message,
                answer_text=answer_text,
                session_id=session_id,
                actor_name=actor_name,
                actor_role=actor_role,
                previous_task_id=previous_task_id,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc).lower()
            if "database is locked" not in msg and "operationalerror" not in msg:
                raise
            _time.sleep(0.05 * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def _persist_question_task_once(
    *,
    message: str,
    answer_text: str,
    session_id: str,
    actor_name: str,
    actor_role: ActorRole,
    previous_task_id: str | None,
) -> str:
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
    """Real task: route through TaskService so the pipeline kicks off.

    Same retry-on-locked treatment as the question path.
    """
    import time as _time
    if scenario not in _VALID_SCENARIOS:
        scenario = "jira_issue_develop"
    last_exc: Exception | None = None
    for attempt in range(5):
        try:
            return _persist_task_intent_task_once(
                message=message,
                summary=summary,
                scenario=scenario,
                session_id=session_id,
                actor_name=actor_name,
                actor_role=actor_role,
                previous_task_id=previous_task_id,
                source_name=source_name,
            )
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            msg = str(exc).lower()
            if "database is locked" not in msg and "operationalerror" not in msg:
                raise
            _time.sleep(0.05 * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


def _persist_task_intent_task_once(
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
