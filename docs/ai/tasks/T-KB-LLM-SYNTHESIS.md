# T-KB-LLM-SYNTHESIS — MiniMax-backed synthesis for knowledge Q&A

**Status:** Ready to dispatch
**Priority:** P0（前端体验最大的障碍）
**Created:** 2026-04-22
**Target commit:** branch from `main` @ `6a35bdc`

## 问题

`apps/backend/app/services/knowledge.py::KnowledgeService._build_answer`（line 812–875）用 Python 字符串拼接生成最终回答，没有调用任何 LLM。结果：

- 回答永远是四段模板："我建议你先看 X / 这样判断的原因是 / 如果第一处没暴露 / 建议按这个顺序排查 / 当前把握度…"
- `_next_steps`（line 753）硬编码 Android 词汇（ViewModel、Activity、Fragment、Repository），用户问 React 或后端代码时完全错位。
- `_route_label`（line 716）只有 4 类：test_failure / android_resource_debug / build_config / code_debug。
- 用户反馈："回答的问题没有解决且都是固定模板？"、"不像 LLM"。

pipeline 里已经跑了 4 次 LLM 调用（translator、planner、reviewer、[codegen]），但最终呈现给用户的那一段文字反而是模板。

## 目标

把 `_build_answer` 的**输出层**替换成 MiniMax LLM 综合生成，保留现有的 retrieval + scoring + citations 不变。citations 作为 evidence 喂给 LLM，LLM 负责产出自然语言答案。

## 约束（严格执行）

1. **不动 retrieval**：`_route_query` / `_score_document` / `_build_citation` / `_assess_risk` 保持原样。这些是"证据收集层"。
2. **不破坏 API 返回结构**：`KnowledgeSearchResult.answer` 仍然是 `str`，`answer_trace` 字段全部保留。只是 `answer` 的内容从模板变成 LLM 输出。
3. **失败必须可降级**：MiniMax 调用失败、超时、没配 API key 时，回退到现有模板逻辑。当前 `_build_answer` 改名保留作为 fallback，不要删除。
4. **不加新依赖**：只用已有的 `httpx`。
5. **沿用 translation.py 风格**：MiniMax 调用模式复用 `apps/backend/app/agents/translation.py::SemanticTranslator._translate_with_minimax`（line 369-424）的结构——httpx.Client + POST chatcompletion_v2 + raise_for_status + 显式 exception 捕获。
6. **不引入 async**：整个 KnowledgeService 是同步的，保持同步。

## 实施步骤

### Step 1：加配置字段

`apps/backend/app/core/config.py`：

```python
# 在 minimax_planner_timeout_seconds 附近加
knowledge_synthesis_enabled: bool = True
knowledge_synthesis_model: str = "minimax-text-01"
knowledge_synthesis_timeout_seconds: float = 45.0
knowledge_synthesis_max_snippet_chars: int = 1200
```

（model 名参考 `minimax_planner_model` 字段的写法；如果 config 里已有 `semantic_translator_model` 默认值，采用同一个 default。）

### Step 2：新建合成器

新文件 `apps/backend/app/services/knowledge_synthesis.py`：

```python
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.schemas.knowledge import KnowledgeCitation


class KnowledgeSynthesisError(RuntimeError):
    """Raised when MiniMax synthesis fails; caller must fall back."""


class KnowledgeSynthesizer:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def synthesize(
        self,
        *,
        query: str,
        citations: list[KnowledgeCitation],
        hallucination_risk: str,
        route_kind: str,
        language: str | None,
    ) -> str:
        """Return LLM-synthesized answer. Raise KnowledgeSynthesisError on any
        provider failure so caller can fall back to the deterministic template."""
        if not self.settings.knowledge_synthesis_enabled:
            raise KnowledgeSynthesisError("synthesis disabled by config")
        if not self.settings.minimax_api_key:
            raise KnowledgeSynthesisError("OPS_AGENT_MINIMAX_API_KEY not configured")
        if not citations:
            raise KnowledgeSynthesisError("no citations to synthesize over")

        use_chinese = self._use_chinese(query=query, language=language)
        evidence_block = self._format_evidence(citations)
        system_prompt = self._build_system_prompt(use_chinese=use_chinese)
        user_prompt = self._build_user_prompt(
            query=query,
            evidence_block=evidence_block,
            hallucination_risk=hallucination_risk,
            route_kind=route_kind,
            use_chinese=use_chinese,
        )

        response_text = self._call_minimax(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        cleaned = response_text.strip()
        if not cleaned:
            raise KnowledgeSynthesisError("MiniMax returned empty answer")
        return cleaned

    # --- helpers ---

    @staticmethod
    def _use_chinese(*, query: str, language: str | None) -> bool:
        if language:
            return language == "zh"
        return bool(re.search(r"[\u4e00-\u9fff]", query))

    def _format_evidence(self, citations: list[KnowledgeCitation]) -> str:
        limit = self.settings.knowledge_synthesis_max_snippet_chars
        parts: list[str] = []
        for index, citation in enumerate(citations, start=1):
            snippet = citation.snippet or ""
            if len(snippet) > limit:
                snippet = snippet[:limit] + "\n…(truncated)"
            parts.append(
                f"[{index}] {citation.source_name}:{citation.relative_path} "
                f"(lines {citation.line_start}-{citation.line_end}, score={citation.score})\n"
                f"{snippet}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _build_system_prompt(*, use_chinese: bool) -> str:
        if use_chinese:
            return (
                "你是企业代码库问答助手。用户问了一个关于代码的问题，"
                "检索系统已经找到若干候选文件和片段作为证据。\n\n"
                "你的任务：\n"
                "1. 基于证据片段给出具体、可操作的回答。\n"
                "2. 回答必须引用具体文件路径和行号（格式：`source:path (lines X-Y)`）。\n"
                "3. 如果证据不足，诚实说明不足在哪里，并建议用户补充什么信息再问。\n"
                "4. 不要凭空编造文件名、类名、方法名，也不要假设证据里没有的代码。\n"
                "5. 回答保持简洁、自然，像资深工程师和同事解释一样；不要输出机械模板。\n"
                "6. 不要输出 JSON，不要输出 markdown 代码块标记，直接用自然段落。"
            )
        return (
            "You are an enterprise codebase Q&A assistant. The user asked about the codebase; "
            "the retrieval system has already selected candidate files and snippets as evidence.\n\n"
            "Your job:\n"
            "1. Give a concrete, actionable answer grounded in the evidence snippets.\n"
            "2. Cite specific files and line ranges (format: `source:path (lines X-Y)`).\n"
            "3. If evidence is insufficient, say so honestly and tell the user what to add for the next query.\n"
            "4. Do not invent file names, class names, or methods not present in the evidence.\n"
            "5. Keep the tone natural and direct, like a senior engineer explaining to a teammate. "
            "Do not emit a rigid template.\n"
            "6. Do not output JSON or markdown code fences. Plain prose only."
        )

    def _build_user_prompt(
        self,
        *,
        query: str,
        evidence_block: str,
        hallucination_risk: str,
        route_kind: str,
        use_chinese: bool,
    ) -> str:
        if use_chinese:
            return (
                f"用户问题：\n{query}\n\n"
                f"检索分类：{route_kind}\n"
                f"置信度标签：{hallucination_risk}\n\n"
                f"证据片段（按相关度排序）：\n{evidence_block}\n\n"
                f"请基于上述证据回答用户问题。"
            )
        return (
            f"User question:\n{query}\n\n"
            f"Retrieval route: {route_kind}\n"
            f"Confidence tag: {hallucination_risk}\n\n"
            f"Evidence snippets (ranked by relevance):\n{evidence_block}\n\n"
            f"Answer the user's question based on the evidence above."
        )

    def _call_minimax(self, *, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.settings.knowledge_synthesis_model,
            "messages": [
                {"role": "system", "name": "Ops Agent Knowledge Synthesizer", "content": system_prompt},
                {"role": "user", "name": "user", "content": user_prompt},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=self.settings.knowledge_synthesis_timeout_seconds) as client:
                response = client.post(
                    f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise KnowledgeSynthesisError(f"MiniMax call failed: {exc}") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise KnowledgeSynthesisError("MiniMax response missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise KnowledgeSynthesisError("MiniMax response missing content")
        return content
```

### Step 3：在 search_repositories 里接线

`apps/backend/app/services/knowledge.py::search_repositories`（line 236 附近），把：

```python
answer = self._build_answer(
    query=query,
    citations=citations,
    hallucination_risk=hallucination_risk,
    route_kind=route.kind,
    language=language,
)
```

改成：

```python
answer, answer_provider = self._synthesize_or_template(
    query=query,
    citations=citations,
    hallucination_risk=hallucination_risk,
    route_kind=route.kind,
    language=language,
)
```

新方法（放在 `_build_answer` 同级，仍然是 `KnowledgeService` 的 classmethod 或 staticmethod 都行，但由于需要访问 settings，做成 instance method）：

```python
def _synthesize_or_template(
    self,
    *,
    query: str,
    citations: list[KnowledgeCitation],
    hallucination_risk: str,
    route_kind: str,
    language: str | None,
) -> tuple[str, str]:
    """Return (answer_text, provider_label). Falls back to the deterministic
    template when MiniMax is unavailable or errors out."""
    from app.services.knowledge_synthesis import (
        KnowledgeSynthesisError,
        KnowledgeSynthesizer,
    )

    if citations and self.settings.knowledge_synthesis_enabled:
        synthesizer = KnowledgeSynthesizer(self.settings)
        try:
            answer = synthesizer.synthesize(
                query=query,
                citations=citations,
                hallucination_risk=hallucination_risk,
                route_kind=route_kind,
                language=language,
            )
            return answer, "minimax"
        except KnowledgeSynthesisError:
            # fall through to template
            pass

    template_answer = self._build_answer(
        query=query,
        citations=citations,
        hallucination_risk=hallucination_risk,
        route_kind=route_kind,
        language=language,
    )
    return template_answer, "template"
```

### Step 4：把 provider 写进 answer_trace

`KnowledgeAnswerTrace` schema（`apps/backend/app/schemas/knowledge.py`）加一个可选字段：

```python
answer_provider: str = "template"
```

然后在 `search_repositories` 里把 `answer_provider=answer_provider` 传进去，便于前端观测到底走的是 LLM 还是模板。

### Step 5：测试

新建 `apps/backend/tests/services/test_knowledge_synthesis.py`：

必须覆盖：
1. `synthesize_success_returns_llm_text` — mock httpx.Client，返回正常 choices，断言返回的就是 message.content。
2. `synthesize_no_api_key_raises` — settings 里把 minimax_api_key 清空，断言 `KnowledgeSynthesisError`。
3. `synthesize_http_error_raises` — mock `raise_for_status()` 抛 `httpx.HTTPStatusError`，断言 `KnowledgeSynthesisError`。
4. `synthesize_empty_citations_raises` — 传空 citations，断言 `KnowledgeSynthesisError`。
5. `synthesize_respects_max_snippet_chars` — 证据片段长度超限时应截断到 `max_snippet_chars + 少量 marker`，断言截断 marker 存在。
6. `search_repositories_falls_back_to_template_on_synth_error` — 在 `KnowledgeService` 层面，把 synthesizer 打桩成抛异常，验证答案仍然是模板（包含"我建议你先看"或"I would start with"其中之一）。
7. `search_repositories_uses_minimax_when_configured` — 打桩 synthesize 返回固定文案，断言 `answer == 固定文案` 且 `answer_trace.answer_provider == "minimax"`。

测试必须不碰真实网络：全部用 `monkeypatch` 或 `unittest.mock.patch`。

### Step 6：兼容性验证

运行：

```bash
cd apps/backend
pytest -n 2 --dist loadfile tests/services/test_knowledge_synthesis.py tests/api/test_knowledge.py -q
```

确认：
- 新测试全绿
- 原有 knowledge api 测试不回归
- 如果 `tests/api/test_knowledge.py` 里有断言 `answer` 具体文案的，改成断言"非空 + 存在引用"

## 验收标准

1. 所有新测试通过。
2. 原有 `tests/` 下任何 `knowledge` 相关测试不回归（允许更新因 answer_provider 字段新增而需要调整的 schema 断言）。
3. 启动后端，前端输入一个非英文问题（例如"登录失败怎么查"），期望：
   - 返回的 `answer` **不是**"我建议你先看 X" 开头的四段模板。
   - 回答里引用了具体文件路径和行号。
   - `answer_trace.answer_provider == "minimax"`。
4. 关闭 MiniMax（临时把 `OPS_AGENT_MINIMAX_API_KEY` 设空）后重跑，回答自动回落到原模板，`answer_provider == "template"`，系统不崩。

## 非目标（后续 ticket 覆盖）

- 流式返回（T-STREAMING-SSE）
- 跳过 plan/review pipeline（T-SKIP-PIPELINE-FOR-QA）
- 其他 LLM provider（Claude API）做同样合成——先把 MiniMax 通路跑通。

## 参考

- MiniMax 客户端模式：`apps/backend/app/agents/translation.py` line 369-466
- Settings 已有字段：`apps/backend/app/core/config.py` line 23, 54-55
- 回答模板的现状：`apps/backend/app/services/knowledge.py::_build_answer` line 812-875
- 调用点：`apps/backend/app/api/knowledge.py:45` 和 `apps/backend/app/tools/gateway.py:403`
