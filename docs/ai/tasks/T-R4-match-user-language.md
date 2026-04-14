# T-R4 — Reply in the Same Language as User Input

<!-- SPEC TEMPLATE v2 — keep this header block stable for prompt cache hits -->
<!-- Effort: low -->
<!-- Executor: codex -->

## Context (shared prefix — do not edit per task)

Repository: Ops_agent_platform (FastAPI backend + React frontend).
Backend root: `apps/backend/`. Run from there.
Module map: `docs/ai/context/repo-index.md`.
Test command: `python -m unittest discover -s tests -v`.
Compile check: `python -m compileall app`.
Python: use `C:\Users\Tomonkyo\AppData\Local\Python\bin\python.exe` if `python` fails (WindowsApps alias issue).

## Goal

The system should reply in the same language as the user's input. If the user types in Chinese, all responses (pipeline summary, knowledge answers, status messages) should be in Chinese. If in English, respond in English.

## Design

### 1. Language detection utility

Add a simple helper in `app/orchestrator/service.py` (or a small utility):

```python
import re

def detect_user_language(text: str) -> str:
    """Detect if the user's input is Chinese or English.
    
    Returns 'zh' if text contains CJK characters, 'en' otherwise.
    """
    cjk_count = len(re.findall(r'[\u4e00-\u9fff\u3400-\u4dbf]', text))
    # If more than 10% of non-space chars are CJK, treat as Chinese
    non_space = len(text.replace(' ', ''))
    if non_space > 0 and cjk_count / non_space > 0.1:
        return "zh"
    return "en"
```

### 2. Store detected language in task/pipeline state

In the orchestrator's `handle_task()` or `_execute_develop_pipeline()`, detect language from `task.request_text` and store it:

```python
user_lang = detect_user_language(task.request_text or "")
pipeline_state["user_lang"] = user_lang
```

### 3. Localize `_build_develop_summary()`

The develop pipeline summary (from T-R2) should use Chinese or English labels based on `user_lang`:

```python
def _build_develop_summary(self, pipeline_state: dict) -> str:
    lang = pipeline_state.get("user_lang", "en")
    issue_key = pipeline_state.get("issue_key", "unknown")
    files = pipeline_state.get("files_changed", [])
    
    if lang == "zh":
        parts = [f"## {issue_key} 开发完成\n"]
        if files:
            parts.append(f"**修改了 {len(files)} 个文件：**")
            for f in files[:10]:
                parts.append(f"- `{f}`")
            parts.append("")
        diff = pipeline_state.get("diff", "")
        if diff:
            parts.append("**代码变更：**")
            parts.append(f"```diff\n{diff}\n```\n")
        parts.append("**流水线执行：**")
        parts.append(f"- 代码生成：{pipeline_state.get('codegen_provider', 'unknown')}")
        method = pipeline_state.get("patch_method", "")
        if method:
            parts.append(f"- 补丁应用方式：{method}")
        if pipeline_state.get("test_skipped"):
            parts.append("- 测试：已跳过（无测试配置）")
        parts.append(f"- 审查：{pipeline_state.get('review_verdict', '无')}")
        parts.append(f"- Jira：已添加评论并转换状态")
    else:
        # English version (existing)
        parts = [f"## {issue_key} Development Complete\n"]
        if files:
            parts.append(f"**Modified {len(files)} file(s):**")
            for f in files[:10]:
                parts.append(f"- `{f}`")
            parts.append("")
        diff = pipeline_state.get("diff", "")
        if diff:
            parts.append("**Changes:**")
            parts.append(f"```diff\n{diff}\n```\n")
        parts.append("**Pipeline:**")
        parts.append(f"- Code generation: {pipeline_state.get('codegen_provider', 'unknown')}")
        method = pipeline_state.get("patch_method", "")
        if method:
            parts.append(f"- Patch applied via: {method}")
        if pipeline_state.get("test_skipped"):
            parts.append("- Tests: skipped (no test config)")
        parts.append(f"- Review: {pipeline_state.get('review_verdict', 'N/A')}")
        parts.append(f"- Jira: commented and transitioned")
    
    return "\n".join(parts)
```

### 4. Localize knowledge answer template

In `app/services/knowledge.py`, the knowledge answer builder constructs responses. Add the user language to the answer prompt so the LLM (or template) responds in the correct language.

Find where the knowledge answer/response text is built (look for "Where I would look first" or "Answer the question" template text) and add language-aware versions:

For Chinese users, the template should say things like:
- "我会从以下文件开始查看：" instead of "Where I would look first:"
- "建议的下一步：" instead of "Suggested next steps:"
- "相关文件：" instead of "Related files:"

The simplest approach: if the answer is template-generated (not LLM-generated), have zh/en versions of the template strings. If LLM-generated, add "Reply in Chinese." or "Reply in English." to the system prompt.

### 5. Pass language to knowledge service

The orchestrator should pass `user_lang` when calling the knowledge service. Add an optional `language` parameter to the knowledge search/answer methods.

### 6. Localize status event messages

The event messages shown during pipeline execution (like "Task entered Jira issue development pipeline") should also respect language. These are emitted via `_emit_event()` with `message=` parameter. For the most visible ones:

- "Jira issue development pipeline started." → "Jira 开发流水线已启动。"
- "Test pipeline skipped" → "测试流水线已跳过"
- Pipeline completion message

Focus on the messages that appear in the chat UI. Internal debug events can stay in English.

## Files to edit

1. `apps/backend/app/orchestrator/service.py` — add `detect_user_language()`, store `user_lang` in pipeline_state, localize `_build_develop_summary()`, pass language to knowledge service, localize key event messages.
2. `apps/backend/app/services/knowledge.py` — accept optional `language` parameter, localize template text in answer builder.

## Tests

1. **`test_detect_user_language_chinese`** — Assert `detect_user_language("把 P69-10 做了")` returns `"zh"`.
2. **`test_detect_user_language_english`** — Assert `detect_user_language("implement P69-10")` returns `"en"`.
3. **`test_develop_summary_chinese`** — Set `user_lang="zh"` in pipeline_state. Assert summary contains "开发完成" and "修改了".
4. **`test_develop_summary_english`** — Set `user_lang="en"` in pipeline_state. Assert summary contains "Development Complete" and "Modified".

## Acceptance criteria

- `python -m compileall app` exits 0.
- New tests pass. Full suite still green.
- Chinese input → Chinese response (pipeline summary, knowledge answer labels).
- English input → English response.
- Code identifiers (file paths, method names) stay in English regardless of language.
- Mixed input (e.g., "implement P69-10") → English (default).

## Workflow (for the executor)

1. Read `app/orchestrator/service.py` — focus on `_build_develop_summary()`, `_execute_develop_pipeline()`, and where `pipeline_state` is initialized.
2. Read `app/services/knowledge.py` — focus on answer/response template generation.
3. Add `detect_user_language()`.
4. Localize `_build_develop_summary()` with zh/en branches.
5. Localize knowledge answer templates.
6. Localize key event messages.
7. Add tests.
8. Run `python -m compileall app && python -m unittest discover -s tests -v`.

```
codex exec --full-auto -c model_reasoning_effort="medium" -C "d:/项目/Ops_agent_platform" - < docs/ai/tasks/T-R4-match-user-language.md
```
