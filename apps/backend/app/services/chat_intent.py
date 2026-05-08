"""Rule-based intent classifier for chat fast-path.

Goal: zero extra LLM call for the common cases (Jira refs, file paths,
README questions). Falls back to ``general_chat`` when no signal is clear,
which the chat endpoint then handles with the unified system prompt.

The classifier is deliberately permissive — it returns the BEST guess plus
a confidence so the caller can decide how aggressively to route. Examples:

  "完成 P69-19"           → develop_task   high   (Jira id + verb)
  "P69-19"                → develop_task   medium (Jira id alone)
  "README 写了啥"         → repo_question  high   (doc keyword)
  "SessionManager 在哪"   → repo_question  high   (symbol-style query)
  "怎么处理审批"          → find_in_docs   medium (process verb + abstract)
  "你是谁"                → general_chat   high   (chitchat)
  "对" / "yes" / "go"     → continuation   high   (carry prior intent)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Intent = Literal[
    "general_chat",      # generic Q (model identity / what is X)
    "platform_query",    # "有几个库" / 任务数 — answered from injected state
    "repo_question",     # README / file / symbol / structure — needs knowledge.search
    "find_in_docs",      # "how do I X in this project" — needs docs router
    "develop_task",      # Jira id / "fix" / "实现" — emit TASK_INTENT
    "continuation",      # short ack / yes / go — inherit prior intent
    "settings_help",     # how do I configure / connect / login
    "unknown",           # safety fallback
]


@dataclass
class IntentResult:
    intent: Intent
    confidence: Literal["high", "medium", "low"]
    signals: list[str]            # which rules fired (for telemetry)
    jira_ids: list[str]           # extracted Jira ids if any
    file_paths: list[str]         # extracted file paths if any


# ---- regex patterns --------------------------------------------------------

# Jira keys: PROJ-123, ABC-1234. 2+ uppercase letters, hyphen, digits.
_JIRA_RE = re.compile(r"\b([A-Z]{2,}-\d+)\b")

# File paths with common code/doc extensions, e.g. app/Foo.kt or README.md.
_FILE_PATH_RE = re.compile(
    r"\b([\w\-./]+\.(?:kt|kts|java|ts|tsx|js|jsx|py|md|json|yml|yaml|sql|toml|sh|ps1))\b",
    re.IGNORECASE,
)

# Short pure acknowledgements that should inherit prior turn's intent.
_ACK_PHRASES = {
    "对", "好", "好的", "嗯", "可以", "go", "yes", "y", "ok", "okay", "对的",
    "继续", "继续做", "执行", "开发任务", "develop", "implement", "fix",
}

# Action verbs implying "do code work" — combine with a target to upgrade.
_DEVELOP_VERBS_CN = ("修", "实现", "完成", "做", "写", "改", "加", "添加", "重构", "fix")
_DEVELOP_VERBS_EN_RE = re.compile(
    r"\b(implement|fix|build|create|add|write|refactor|patch|develop)\b", re.IGNORECASE
)

# Repo / structure / symbol questions.
_REPO_KEYWORDS_CN = (
    "项目结构", "目录结构", "文件结构", "代码结构",
    "在哪", "在哪里", "哪个文件", "哪里有",
    "readme", "README", "文档里",
)
_REPO_KEYWORDS_EN_RE = re.compile(
    r"\b(readme|repo structure|project structure|where is|file tree|directory tree|class\s+\w+|function\s+\w+)\b",
    re.IGNORECASE,
)
# Symbol-ish identifier: CamelCase or snake_case identifier of length >= 4.
_SYMBOL_RE = re.compile(r"\b([A-Z][a-zA-Z0-9]{3,}[A-Z][a-zA-Z0-9]+|[a-z][a-z0-9_]{4,})\b")

# Doc / playbook lookup intent.
_DOCS_PHRASES_CN = (
    "怎么处理", "如何处理", "怎么做", "playbook", "流程是", "规则是",
    "应该怎么", "应该如何", "审批", "gate", "diff", "patch",
)

# Settings / integration / login.
_SETTINGS_PHRASES = (
    "登录", "登入", "连接", "配置", "切换模型", "用什么模型", "选模型", "API key",
    "Anthropic key", "DeepSeek key", "OpenAI key", "Jira 怎么", "GitHub 怎么",
    "怎么换", "怎么登录", "怎么登入",
)

# Platform-state queries that the injected system prompt already covers.
_PLATFORM_PHRASES = (
    "有几个库", "几个仓库", "已注册", "已连接", "工具就绪", "运行中",
    "待审批数", "任务总数", "用了多少 token", "用了多少 tokens",
)

# Truly chitchat / model-identity questions.
_CHITCHAT_PHRASES = (
    "你是谁", "你叫什么", "who are you", "what are you", "hello", "hi", "在吗",
    "你好",
)


def classify_intent(
    message: str,
    history: list[dict[str, str]] | None = None,
    *,
    prior_intent: Intent | None = None,
) -> IntentResult:
    """Pure-Python intent classifier. Returns immediately, no I/O, no LLM."""
    history = history or []
    text = (message or "").strip()
    lower = text.lower()
    signals: list[str] = []

    if not text:
        return IntentResult("unknown", "low", ["empty"], [], [])

    # Pure short acknowledgement → inherit prior intent if any.
    if lower in _ACK_PHRASES or text in _ACK_PHRASES:
        signals.append("ack_phrase")
        if prior_intent and prior_intent not in ("continuation", "unknown"):
            return IntentResult(prior_intent, "high", signals + ["inherit"], [], [])
        # No prior context — guess develop_task because "go / 开发任务" usually
        # follows a clarification request from the model.
        return IntentResult("continuation", "medium", signals, [], [])

    # Extract structured signals.
    jira_ids = _JIRA_RE.findall(text)
    file_paths = _FILE_PATH_RE.findall(text)

    # Jira id + (verb OR file path OR no other signal) → develop_task.
    if jira_ids:
        signals.append(f"jira_id={jira_ids[0]}")
        verb_present = any(v in text for v in _DEVELOP_VERBS_CN) or _DEVELOP_VERBS_EN_RE.search(text)
        if verb_present:
            return IntentResult("develop_task", "high", signals + ["develop_verb"], jira_ids, file_paths)
        # Jira id alone is still very likely a task request.
        return IntentResult("develop_task", "medium", signals + ["jira_alone"], jira_ids, file_paths)

    # File path + verb → develop_task. File path alone → repo_question.
    if file_paths:
        signals.append(f"file_path={file_paths[0]}")
        verb_present = any(v in text for v in _DEVELOP_VERBS_CN) or _DEVELOP_VERBS_EN_RE.search(text)
        if verb_present:
            return IntentResult("develop_task", "high", signals + ["develop_verb"], jira_ids, file_paths)
        return IntentResult("repo_question", "high", signals + ["file_lookup"], jira_ids, file_paths)

    # Settings / config / login.
    for phrase in _SETTINGS_PHRASES:
        if phrase.lower() in lower:
            return IntentResult("settings_help", "high", signals + [f"settings={phrase}"], [], [])

    # Platform state queries.
    for phrase in _PLATFORM_PHRASES:
        if phrase in text:
            return IntentResult("platform_query", "high", signals + [f"platform={phrase}"], [], [])

    # Repo / structure / file / symbol questions.
    for phrase in _REPO_KEYWORDS_CN:
        if phrase in text:
            return IntentResult("repo_question", "high", signals + [f"repo_kw={phrase}"], [], [])
    if _REPO_KEYWORDS_EN_RE.search(text):
        return IntentResult("repo_question", "high", signals + ["repo_kw_en"], [], [])

    # Docs / playbook lookup.
    for phrase in _DOCS_PHRASES_CN:
        if phrase in text:
            return IntentResult("find_in_docs", "medium", signals + [f"docs={phrase}"], [], [])

    # Chitchat.
    for phrase in _CHITCHAT_PHRASES:
        if phrase.lower() in lower:
            return IntentResult("general_chat", "high", signals + ["chitchat"], [], [])

    # Develop verb without Jira / file ref → low confidence develop_task.
    verb_present = any(v in text for v in _DEVELOP_VERBS_CN) or _DEVELOP_VERBS_EN_RE.search(text)
    if verb_present:
        return IntentResult("develop_task", "low", signals + ["bare_verb"], [], [])

    # Symbol-shaped tokens hint at code lookup.
    if _SYMBOL_RE.search(text):
        return IntentResult("repo_question", "medium", signals + ["symbol_shape"], [], [])

    return IntentResult("general_chat", "low", signals + ["fallback"], [], [])
