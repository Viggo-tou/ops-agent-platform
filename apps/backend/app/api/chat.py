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

之后可以再写 1-2 句**意图描述**,**不能**说成事实。区别:

- ✅ "我**会**去创建一个开发任务" / "我**正在**为你委托给 pipeline" / "**接下来**会让 pipeline 执行"
- ❌ "我**已经**创建了任务" / "**已**提交 pipeline" / "任务**已经**开始执行"
- ❌ "Jira 单 P69-XX 的开发**已经**启动"

为什么:你输出 marker 之后,系统才尝试 INSERT Task。可能因 DB 繁忙、参数无效等失败。**真正的成功/失败由系统状态条告诉用户**(前端会渲染绿/红块)。你提前断言"已创建"会让用户被骗。

**用现在进行时 / 将来时,不要用完成时**。

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

    # 4. Recent agent memories — high-confidence cross-scope items so the
    #    chat LLM benefits from past gate failures / fixes / patterns the
    #    pipeline learned. Without this, every conversation starts from a
    #    blank slate and re-suggests fixes that previously broke.
    try:
        from sqlalchemy import select as _select
        from app.models.memory import AgentMemory
        stmt = (
            _select(AgentMemory)
            .where(AgentMemory.confidence >= 0.6)
            .order_by(
                AgentMemory.last_used_at.desc().nullslast(),
                AgentMemory.confidence.desc(),
                AgentMemory.usage_count.desc(),
                AgentMemory.created_at.desc(),
            )
            .limit(8)
        )
        memories = list(db.scalars(stmt))
        if memories:
            lines = ["", "### 历史经验 (近期高置信记忆)"]
            for m in memories:
                obs = (m.observation or "").strip().replace("\n", " ")
                res = (m.resolution or "").strip().replace("\n", " ")
                if len(obs) > 200:
                    obs = obs[:200] + "…"
                if len(res) > 200:
                    res = res[:200] + "…"
                scope = m.scope or "general"
                lines.append(f"- **[{scope}]** 观察: {obs} → 处理: {res}")
            lines.append("")
            lines.append("引用上述经验时,先确认它仍然适用当前上下文(代码可能已变),不要照搬。")
            out.append("\n".join(lines))
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


_ACTIVE_PIPELINE_STATUSES = (
    TaskStatus.PLANNING,
    TaskStatus.REVIEWING,
    TaskStatus.EXECUTING,
    TaskStatus.RUNNING,
    TaskStatus.QUEUED,
    TaskStatus.AWAITING_APPROVAL,
)


def _find_active_session_task(db: Session, session_id: str | None) -> dict | None:
    """If this session has a pipeline task currently in flight (or paused
    for approval), return a small dict {id, scenario, status, title}.

    Used by the chat endpoint to:
      1. Tell the model 'a task is already running, don't create another'.
      2. Suggest the user open the existing task / use /iterate instead.
    """
    if not session_id:
        return None
    row = (
        db.query(TaskModel)
        .filter(TaskModel.session_id == session_id)
        .filter(TaskModel.status.in_(_ACTIVE_PIPELINE_STATUSES))
        .filter(TaskModel.scenario.in_(("jira_issue_develop", "jira_issue_plan", "jira_issue_writeback")))
        .order_by(TaskModel.updated_at.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "scenario": row.scenario,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "title": row.title,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


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
        # Detect any in-flight pipeline task in the same session. If there is
        # one, the user's "continue" / "继续哇" / "go" most likely means
        # 'check on that task', not 'create a brand-new task'. We surface
        # this to the model via the system prompt so it points the user at
        # the existing task instead of emitting TASK_INTENT.
        active_session_task = _find_active_session_task(_state_db, body.session_id)
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

    # Continuation intent: never let the model emit TASK_INTENT. The user's
    # message is an ack ('对', '继续', '继续哇', 'go') that should NOT spawn
    # a new heavy pipeline task. If a pipeline task is already running in
    # this session, surface its id / status / title so the model can point
    # the user there.
    if intent.intent == "continuation":
        guard_block = "\n\n## 重要 — 当前是 continuation 意图\n"
        guard_block += "用户这一轮只是确认/续连(短词:对、好、继续、go 等),**绝对不要**输出 `TASK_INTENT|...` marker — 不要开新任务。\n\n"
        if active_session_task:
            guard_block += (
                f"本会话里**已经**有一个 pipeline 任务在跑:\n"
                f"- 任务 ID: `{active_session_task['id']}`\n"
                f"- 状态: {active_session_task['status']}\n"
                f"- 标题: {active_session_task['title']}\n\n"
                "应当回答(用中文,简短):**那个任务还在跑**,告诉用户去任务详情页 "
                f"(`/tasks/{active_session_task['id']}`) 看进度;如果想改方向,在那边底部的 "
                "「继续改动」输入框继续,会自动接住前一个任务的上下文。"
            )
        else:
            guard_block += (
                "本会话没有进行中的 pipeline 任务。礼貌问用户**具体想做什么**(改哪个文件 / "
                "查什么 / 哪个 Jira 单),引导他给出可路由的具体内容。"
            )
        system_prompt = system_prompt + guard_block

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
                async for kind, payload in _stream_from_provider(
                    provider, body.message, settings, model_name, system_prompt, history
                ):
                    if kind == "text":
                        if not payload:
                            continue
                        full_text += str(payload)
                        yield _sse("token", {"text": payload})
                    elif kind == "tool_call":
                        yield _sse("tool_call", payload)
                    elif kind == "tool_result":
                        yield _sse("tool_result", payload)
                    elif kind == "done":
                        break
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

        intent_marker = _parse_task_intent(full_text)
        # Defensive guard: if classifier said continuation, ignore any
        # TASK_INTENT marker the model emitted anyway. Continuation NEVER
        # spawns a new heavy task — the user is acking a previous turn.
        # `intent` here refers to the outer-scope IntentResult.
        if intent.intent == "continuation":
            persistence_intent: tuple[str, str] | None = None
        else:
            persistence_intent = intent_marker

        # Persist a Task row either way so chat history is queryable.
        try:
            if persistence_intent is None:
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
                scenario, summary = persistence_intent
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
            # Differentiate persistence failure from a generic stream error.
            # The frontend renders task_create_failed as an explicit red status
            # block ("✗ 任务未创建") so the user sees the truth instead of
            # trusting the model's preceding "已提交" claim. The model's
            # answer text is preserved (`answer_kept`) — we just couldn't
            # write the Task row.
            err_str = str(exc)
            is_locked = "database is locked" in err_str.lower() or "operationalerror" in err_str.lower()
            yield _sse(
                "task_create_failed",
                {
                    "reason": err_str[:300],
                    "reason_kind": "db_locked" if is_locked else "persistence_error",
                    "answer_kept": True,
                    "scenario_intended": (persistence_intent[0] if persistence_intent else "process_question"),
                    "user_advice": (
                        "数据库繁忙(有别的任务正在写),稍后重试一下。"
                        if is_locked
                        else "持久化失败,请重试或检查后端日志。"
                    ),
                },
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
) -> AsyncGenerator[tuple[str, object], None]:
    """Yields (kind, payload) events. Kind ∈ {text, tool_call, tool_result, done}.

    Older callers expected raw text chunks. We changed the contract here
    (commit B.1/2) so the outer stream() can also surface MCP tool calls
    to the UI. All text-only provider streams are wrapped via
    _text_stream_to_events to preserve the tuple shape.
    """
    history = history or []
    mcp_tools = _collect_mcp_tools_openai_format()

    if provider == "mock":
        async for ev in _text_stream_to_events(_stream_mock(message)):
            yield ev
        yield ("done", "stop")
        return
    if provider == "minimax":
        async for ev in _text_stream_to_events(
            _stream_minimax(message, settings, model_name, system_prompt, history)
        ):
            yield ev
        yield ("done", "stop")
        return
    if provider == "anthropic":
        async for ev in _text_stream_to_events(
            _stream_anthropic(message, settings, model_name, system_prompt, history)
        ):
            yield ev
        yield ("done", "stop")
        return
    if provider == "openai":
        if mcp_tools:
            async for ev in _stream_openai_compat_with_tools(
                "openai", message, settings, model_name, system_prompt, history, mcp_tools
            ):
                yield ev
            return
        async for ev in _text_stream_to_events(
            _stream_openai(message, settings, model_name, system_prompt, history)
        ):
            yield ev
        yield ("done", "stop")
        return
    if provider == "deepseek":
        if mcp_tools:
            async for ev in _stream_openai_compat_with_tools(
                "deepseek", message, settings, model_name, system_prompt, history, mcp_tools
            ):
                yield ev
            return
        async for ev in _text_stream_to_events(
            _stream_deepseek(message, settings, model_name, system_prompt, history)
        ):
            yield ev
        yield ("done", "stop")
        return
    # Unknown / not yet implemented (claude_code / codex / ollama).
    # Raise so the chain falls through to the next provider.
    raise RuntimeError(f"streaming not implemented for provider '{provider}'")


async def _text_stream_to_events(
    text_iter: AsyncGenerator[str, None],
) -> AsyncGenerator[tuple[str, object], None]:
    async for chunk in text_iter:
        if chunk:
            yield ("text", chunk)


def _wire_name_to_tool_name(wire_name: str) -> str:
    """Translate the on-the-wire function name back to the registry name.

    OpenAI (and Anthropic) restrict tool names to ``^[a-zA-Z0-9_-]+$``,
    so dots in ``mcp.<server>.<tool>`` aren't allowed in the tools array.
    On the wire we use ``mcp__<server>__<tool>``; the gateway / registry
    still keeps the canonical dotted form.
    """
    if not wire_name.startswith("mcp__"):
        return wire_name
    parts = wire_name.split("__", 2)
    if len(parts) != 3:
        return wire_name
    _, server, tool = parts
    return f"mcp.{server}.{tool}"


def _collect_mcp_tools_openai_format() -> list[dict]:
    try:
        from app.services.mcp_client import get_mcp_client

        out: list[dict] = []
        for server_name, tools in (get_mcp_client().list_tools() or {}).items():
            if not isinstance(tools, list):
                continue
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_name = str(tool.get("name") or "").strip()
                if not tool_name:
                    continue
                schema = tool.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{server_name}__{tool_name}",
                            "description": tool.get("description") or "",
                            "parameters": schema,
                        },
                    }
                )
        return out
    except Exception:  # noqa: BLE001
        return []


def _execute_chat_tool_blocking_legacy(tool_name: str, arguments: dict) -> str:
    """Sync helper that calls an MCP tool and returns a string for the LLM.

    Bypasses ToolGateway.execute (which requires a Task row for its
    ToolExecution audit trail — chat tool calls happen before the chat
    Task is persisted). Direct mcp_client.call_tool covers the same
    physical dispatch; chat-side audit is a follow-up if needed.
    """
    if not tool_name.startswith("mcp."):
        return f"ERROR: only mcp.* tools are callable from chat; got {tool_name}"
    parts = tool_name.split(".", 2)
    if len(parts) != 3:
        return f"ERROR: malformed mcp tool name: {tool_name}"
    _, server, name = parts
    try:
        from app.services.mcp_client import get_mcp_client

        result = get_mcp_client().call_tool(
            server=server, tool_name=name, arguments=arguments or {}
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    is_error = bool(result.get("is_error"))
    text_parts = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict):
            t = item.get("text")
            if isinstance(t, str):
                text_parts.append(t)
                continue
            text_parts.append(json.dumps(item, ensure_ascii=False))
        else:
            text_parts.append(str(item))
    body = "\n".join(text_parts) or ("(empty result)" if not is_error else "(error, no detail)")
    return f"ERROR: {body}" if is_error else body


_CHAT_TOOL_USE_MAX_ITERATIONS = 5


def _execute_chat_tool_blocking(tool_name: str, arguments: dict) -> str:
    from app.tools.gateway import ToolGateway

    # The model received tools advertised with the wire-safe form
    # (mcp__<server>__<tool>) because OpenAI/Anthropic reject dots in
    # function names. The registry / gateway use the canonical dotted
    # form, so translate here before dispatch.
    tool_name = _wire_name_to_tool_name(tool_name)

    task_id = uuid.uuid4().hex
    db: Session = SessionLocal()
    try:
        synthetic_task = TaskModel(
            id=task_id,
            session_id=None,
            actor_name="chat",
            actor_role=ActorRole.SYSTEM,
            title=(f"chat tool {tool_name}")[:255],
            request_text=json.dumps(arguments or {}, ensure_ascii=False, default=str),
            scenario="chat_tool_call",
            status=TaskStatus.RUNNING,
            workflow_stage=WorkflowStage.ACTION,
            current_role=RoleName.SYSTEM,
            risk_level=RiskLevel.LOW,
            risk_category=RiskCategory.GENERAL,
            governance_json={"actor": {"name": "chat", "role": ActorRole.SYSTEM.value}},
            latest_result_json={"kind": "chat_tool_call", "tool_name": tool_name},
            pending_approval=False,
        )
        db.add(synthetic_task)
        db.flush()

        gateway = ToolGateway(db)
        try:
            result = gateway.execute(
                task_id=task_id,
                tool_name=tool_name,
                payload=arguments or {},
                actor_context={"actor_name": "chat", "task_id": task_id},
            )
        except Exception as exc:  # noqa: BLE001
            synthetic_task.status = TaskStatus.FAILED
            synthetic_task.workflow_stage = WorkflowStage.DONE
            synthetic_task.latest_result_json = {
                "kind": "chat_tool_call",
                "tool_name": tool_name,
                "error": f"{type(exc).__name__}: {exc}",
            }
            try:
                db.commit()
            except Exception:
                db.rollback()
            return f"ERROR: {type(exc).__name__}: {exc}"

        text_parts: list[str] = []
        for item in result.get("content") or []:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        content = "\n".join(text_parts)
        is_error = bool(result.get("is_error"))
        synthetic_task.status = TaskStatus.FAILED if is_error else TaskStatus.COMPLETED
        synthetic_task.workflow_stage = WorkflowStage.DONE
        synthetic_task.latest_result_json = {
            "kind": "chat_tool_call",
            "tool_name": tool_name,
            "is_error": is_error,
            "content": content,
        }
        try:
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            return f"ERROR: {type(exc).__name__}: {exc}"
        if is_error:
            return f"ERROR: {content}"
        return content
    finally:
        db.close()


async def _stream_openai_compat_with_tools(
    provider_id: str,
    message: str,
    settings,
    model_name: str,
    system_prompt: str,
    history: list[dict[str, str]],
    mcp_tools: list[dict],
) -> AsyncGenerator[tuple[str, object], None]:
    """OpenAI-format streaming with tool-use loop. Used for openai + deepseek."""
    if provider_id == "openai":
        if not getattr(settings, "openai_api_key", None):
            raise RuntimeError("OPS_AGENT_OPENAI_API_KEY not set")
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        default_model = "gpt-4o-mini"
    elif provider_id == "deepseek":
        if not getattr(settings, "deepseek_api_key", None):
            raise RuntimeError("OPS_AGENT_DEEPSEEK_API_KEY not set")
        url = "https://api.deepseek.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        default_model = "deepseek-chat"
    else:
        raise RuntimeError(f"unsupported openai-compat provider: {provider_id}")

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    for _iteration in range(_CHAT_TOOL_USE_MAX_ITERATIONS):
        payload = {
            "model": model_name or default_model,
            "stream": True,
            "messages": messages,
            "tools": mcp_tools,
            "tool_choice": "auto",
        }
        # Tool execution can take time; longer body timeout than text-only.
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code >= 400:
                    err_body = await response.aread()
                    raise RuntimeError(
                        f"{provider_id} {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                    )

                # Per-stream accumulators, indexed by tool_call.index from delta.
                pending_calls: dict[int, dict[str, str]] = {}
                finish_reason: str | None = None
                assistant_text_parts: list[str] = []

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].lstrip()
                    if not data_str:
                        continue
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    finish_reason = choice.get("finish_reason")

                    text = delta.get("content")
                    if isinstance(text, str) and text:
                        assistant_text_parts.append(text)
                        yield ("text", text)

                    for tc in delta.get("tool_calls") or []:
                        if not isinstance(tc, dict):
                            continue
                        idx = tc.get("index")
                        if not isinstance(idx, int):
                            continue
                        slot = pending_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if not isinstance(fn, dict):
                            continue
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

        if finish_reason == "tool_calls" and pending_calls:
            # Append the assistant turn that requested these tool calls.
            assistant_tool_calls = []
            for idx in sorted(pending_calls):
                slot = pending_calls[idx]
                assistant_tool_calls.append(
                    {
                        "id": slot["id"] or f"call_{idx}",
                        "type": "function",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    }
                )
            assistant_message: dict[str, object] = {
                "role": "assistant",
                "tool_calls": assistant_tool_calls,
            }
            assistant_text = "".join(assistant_text_parts)
            if assistant_text:
                assistant_message["content"] = assistant_text
            messages.append(assistant_message)

            for idx in sorted(pending_calls):
                slot = pending_calls[idx]
                tc_id = slot["id"] or f"call_{idx}"
                name = slot["name"]
                try:
                    parsed = json.loads(slot["arguments"]) if slot["arguments"] else {}
                except json.JSONDecodeError:
                    parsed = {}
                args = parsed if isinstance(parsed, dict) else {}
                # UI sees the canonical mcp.<server>.<tool> form even
                # though the model received the wire-safe mcp__ form.
                display_name = _wire_name_to_tool_name(name)
                yield ("tool_call", {"id": tc_id, "name": display_name, "arguments": args})
                result_str = await asyncio.to_thread(
                    _execute_chat_tool_blocking, name, args
                )
                is_error = result_str.startswith("ERROR: ")
                yield (
                    "tool_result",
                    {"tool_call_id": tc_id, "content": result_str, "is_error": is_error},
                )
                messages.append(
                    {"role": "tool", "tool_call_id": tc_id, "content": result_str}
                )
            continue

        if finish_reason in (None, "stop"):
            yield ("done", "stop")
            return
        if finish_reason == "tool_calls":
            yield ("done", "unknown")
            return
        yield ("done", finish_reason or "unknown")
        return

    yield ("done", "max_iterations")


def _convert_openai_tools_to_anthropic(openai_tools: list[dict]) -> list[dict]:
    """Anthropic uses {name, description, input_schema} flat shape."""
    out: list[dict] = []
    for t in openai_tools:
        fn = t.get("function") or {}
        out.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


async def _stream_anthropic_with_tools(
    message: str,
    settings,
    model_name: str,
    system_prompt: str,
    history: list[dict[str, str]],
    anthropic_tools: list[dict],
) -> AsyncGenerator[tuple[str, object], None]:
    """Anthropic Messages API streaming with tool-use loop.

    Anthropic emits tool calls as content blocks (type=tool_use) interleaved
    with text blocks. We accumulate input_json_delta strings per block index,
    and on stop_reason=tool_use we execute each, append role=user with
    tool_result blocks, and re-stream.
    """
    if not getattr(settings, "anthropic_api_key", None):
        raise RuntimeError("OPS_AGENT_ANTHROPIC_API_KEY not set")
    headers = {
        "x-api-key": settings.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    # Anthropic rejects two consecutive same-role messages and requires the
    # conversation to alternate. Existing _stream_anthropic does this; we
    # reapply the same collapse.
    msgs: list[dict] = []
    for entry in history:
        if msgs and msgs[-1]["role"] == entry["role"]:
            msgs[-1]["content"] = msgs[-1]["content"] + "\n\n" + entry["content"]
        else:
            msgs.append(dict(entry))
    if msgs and msgs[-1]["role"] == "user":
        msgs[-1]["content"] = msgs[-1]["content"] + "\n\n" + message
    else:
        msgs.append({"role": "user", "content": message})

    for _iteration in range(_CHAT_TOOL_USE_MAX_ITERATIONS):
        payload = {
            "model": model_name or "claude-sonnet-4-5",
            "max_tokens": _DEFAULT_MAX_TOKENS,
            "system": system_prompt,
            "stream": True,
            "messages": msgs,
            "tools": anthropic_tools,
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
            async with client.stream(
                "POST", "https://api.anthropic.com/v1/messages", headers=headers, json=payload
            ) as response:
                if response.status_code >= 400:
                    err_body = await response.aread()
                    raise RuntimeError(
                        f"anthropic {response.status_code}: {err_body.decode('utf-8', 'replace')[:300]}"
                    )

                # Per-stream block accumulators, keyed by content-block index.
                blocks: dict[int, dict] = {}
                stop_reason: str | None = None

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].lstrip()
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    ev_type = data.get("type")
                    if ev_type == "content_block_start":
                        idx = data.get("index", 0)
                        block = data.get("content_block") or {}
                        blocks[idx] = {
                            "type": block.get("type") or "",
                            "id": block.get("id") or "",
                            "name": block.get("name") or "",
                            "text": "",
                            "input_json": "",
                        }
                    elif ev_type == "content_block_delta":
                        idx = data.get("index", 0)
                        delta = data.get("delta") or {}
                        slot = blocks.setdefault(
                            idx,
                            {"type": "", "id": "", "name": "", "text": "", "input_json": ""},
                        )
                        if delta.get("type") == "text_delta":
                            chunk = delta.get("text") or ""
                            if chunk:
                                slot["text"] += chunk
                                yield ("text", chunk)
                        elif delta.get("type") == "input_json_delta":
                            slot["input_json"] += delta.get("partial_json") or ""
                    elif ev_type == "content_block_stop":
                        # Nothing to yield — the per-block content is already
                        # collected via the deltas above.
                        pass
                    elif ev_type == "message_delta":
                        delta = data.get("delta") or {}
                        sr = delta.get("stop_reason")
                        if sr:
                            stop_reason = sr
                    elif ev_type == "message_stop":
                        break

        tool_use_blocks = [
            (idx, blocks[idx]) for idx in sorted(blocks) if blocks[idx]["type"] == "tool_use"
        ]
        if stop_reason == "tool_use" and tool_use_blocks:
            # Re-build the assistant turn as a list of content blocks so the
            # next request can reference each tool_use by id.
            assistant_content: list[dict] = []
            for idx in sorted(blocks):
                slot = blocks[idx]
                if slot["type"] == "text":
                    if slot["text"]:
                        assistant_content.append({"type": "text", "text": slot["text"]})
                elif slot["type"] == "tool_use":
                    try:
                        parsed = json.loads(slot["input_json"]) if slot["input_json"] else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": slot["id"],
                            "name": slot["name"],
                            "input": parsed,
                        }
                    )
            msgs.append({"role": "assistant", "content": assistant_content})

            tool_result_blocks: list[dict] = []
            for _idx, slot in tool_use_blocks:
                try:
                    args = json.loads(slot["input_json"]) if slot["input_json"] else {}
                except json.JSONDecodeError:
                    args = {}
                yield (
                    "tool_call",
                    {"id": slot["id"], "name": slot["name"], "arguments": args},
                )
                result_str = await asyncio.to_thread(
                    _execute_chat_tool_blocking, slot["name"], args
                )
                is_error = result_str.startswith("ERROR: ")
                yield (
                    "tool_result",
                    {
                        "tool_call_id": slot["id"],
                        "content": result_str,
                        "is_error": is_error,
                    },
                )
                tool_result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": slot["id"],
                        "content": result_str,
                        "is_error": is_error,
                    }
                )
            msgs.append({"role": "user", "content": tool_result_blocks})
            continue

        if stop_reason in (None, "end_turn", "stop_sequence"):
            yield ("done", "stop")
            return
        yield ("done", stop_reason or "unknown")
        return

    yield ("done", "max_iterations")


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

    No retry. The previous 5x exponential-backoff retry was a band-aid
    that hid failures from the user — when DB was locked, the request
    spent up to 1.5s silently retrying with no UI signal. After the
    record_event auto-commit fix landed (5810188), the orchestrator no
    longer holds the write lock for the full pipeline duration, so the
    common contention case is gone. The remaining locked-failures are
    real and should propagate as task_create_failed events to the UI
    immediately, not be papered over.
    """
    return _persist_question_task_once(
        message=message,
        answer_text=answer_text,
        session_id=session_id,
        actor_name=actor_name,
        actor_role=actor_role,
        previous_task_id=previous_task_id,
    )


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

    Single attempt — if the INSERT fails (locked DB or otherwise), let it
    propagate so the chat endpoint can emit task_create_failed instead of
    silently retrying.
    """
    if scenario not in _VALID_SCENARIOS:
        scenario = "jira_issue_develop"
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
