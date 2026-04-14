from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.agents.schemas import (
    GeneratedSemanticTranslation,
    SemanticTranslationPayload,
    SemanticTranslationResult,
)
from app.core.config import Settings, get_settings
from app.core.jira import JiraIssueReference, extract_jira_issue_reference

STOPWORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "at",
    "by",
    "can",
    "check",
    "code",
    "debug",
    "do",
    "for",
    "from",
    "help",
    "how",
    "i",
    "if",
    "implement",
    "implementation",
    "in",
    "into",
    "is",
    "issue",
    "it",
    "look",
    "need",
    "of",
    "on",
    "or",
    "plan",
    "please",
    "project",
    "request",
    "should",
    "task",
    "that",
    "the",
    "this",
    "to",
    "up",
    "what",
    "where",
    "why",
    "with",
}
TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{2,}")
WORK_TYPES = {"bugfix", "feature", "investigation", "operations", "question", "unknown"}

LIST_FIELD_LIMITS = {
    "candidate_modules": 8,
    "search_queries": 6,
    "constraints": 8,
    "requested_outputs": 6,
    "grounding_terms": 10,
    "missing_information": 8,
}


def _short_request_summary(request_text: str) -> str:
    normalized = " ".join(request_text.strip().split())
    if len(normalized) <= 140:
        return normalized
    return f"{normalized[:137]}..."


def _materialize_translation(
    *,
    payload: SemanticTranslationPayload,
    task_id: str,
    provider: dict[str, Any],
) -> GeneratedSemanticTranslation:
    return GeneratedSemanticTranslation(
        task_id=task_id,
        provider=provider,
        **payload.model_dump(mode="python"),
    )


def _infer_work_type(request_text: str, *, scenario: str) -> str:
    lowered = request_text.lower()
    if scenario == "jira_issue_plan":
        return "feature" if any(word in lowered for word in ("feature", "story", "rollout")) else "investigation"
    if scenario == "jira_issue_writeback":
        return "operations"
    if any(word in lowered for word in ("bug", "fix", "debug", "error", "exception", "traceback", "crash", "fail")):
        return "bugfix"
    if any(word in lowered for word in ("plan", "investigate", "analyze", "root cause", "scope")):
        return "investigation"
    if any(word in lowered for word in ("notify", "slack", "announce", "access", "approval")):
        return "operations"
    if any(word in lowered for word in ("feature", "story", "build", "develop")):
        return "feature"
    if scenario == "process_question":
        return "question"
    return "unknown"


def _infer_intent(request_text: str, *, scenario: str) -> str:
    intent_map = {
        "jira_issue_plan": "plan_existing_jira_issue",
        "jira_issue_create": "create_jira_issue",
        "jira_issue_writeback": "writeback_jira_issue",
        "slack_message": "send_slack_message",
        "internal_api_request": "call_internal_api",
        "internal_db_query": "query_internal_database",
        "action_with_approval": "run_approval_gated_action",
        "process_question": "grounded_knowledge_lookup",
    }
    intent = intent_map.get(scenario)
    if intent:
        return intent

    lowered = request_text.lower()
    if "jira" in lowered:
        return "jira_workflow"
    if "slack" in lowered:
        return "slack_messaging"
    return "general_request"


def _extract_grounding_terms(*texts: str) -> list[str]:
    ranked: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for token in TOKEN_PATTERN.findall(text or ""):
            normalized = token.strip("._-").lower()
            if len(normalized) < 3 or normalized in STOPWORDS or normalized in seen:
                continue
            seen.add(normalized)
            ranked.append(token)
            if len(ranked) >= 10:
                return ranked
    return ranked


def _extract_candidate_modules(*texts: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for token in TOKEN_PATTERN.findall(text or ""):
            normalized = token.strip("._-").lower()
            if len(normalized) < 3 or normalized in STOPWORDS or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(token)
            if len(candidates) >= 6:
                return candidates
    return candidates


def _build_search_queries(
    *,
    request_text: str,
    scenario: str,
    issue_reference: JiraIssueReference | None,
    issue_context: dict[str, object] | None,
    candidate_modules: list[str],
) -> list[str]:
    queries: list[str] = []
    summary = ""
    description = ""
    if issue_context:
        summary = str(issue_context.get("summary") or "").strip()
        description = str(issue_context.get("description") or "").strip()

    base_query_parts = [summary, description, _short_request_summary(request_text)]
    base_query = " ".join(part for part in base_query_parts if part).strip()
    code_focused_query = " ".join(candidate_modules[:4]).strip()

    if scenario == "process_question" and code_focused_query:
        queries.append(code_focused_query[:220])

    if base_query:
        queries.append(base_query[:220])

    if issue_reference and issue_reference.issue_key and summary:
        queries.append(f"{issue_reference.issue_key} {summary}".strip()[:220])

    if scenario == "jira_issue_plan" and candidate_modules:
        queries.append(f"implementation plan {' '.join(candidate_modules[:4])}".strip()[:220])

    if not queries:
        queries.append(_short_request_summary(request_text))

    deduplicated: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = query.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(query)
        if len(deduplicated) >= 4:
            break
    return deduplicated


def build_fallback_semantic_translation_payload(
    request_text: str,
    *,
    scenario: str,
    issue_context: dict[str, object] | None = None,
) -> SemanticTranslationPayload:
    issue_reference = extract_jira_issue_reference(request_text)
    summary = str(issue_context.get("summary") or "").strip() if issue_context else ""
    description = str(issue_context.get("description") or "").strip() if issue_context else ""
    candidate_modules = _extract_candidate_modules(request_text, summary, description)
    grounding_terms = _extract_grounding_terms(request_text, summary, description)
    search_queries = _build_search_queries(
        request_text=request_text,
        scenario=scenario,
        issue_reference=issue_reference,
        issue_context=issue_context,
        candidate_modules=candidate_modules,
    )

    constraints: list[str] = []
    if issue_context:
        issue_status = str(issue_context.get("issue_status") or "").strip()
        priority = str(issue_context.get("priority") or "").strip()
        issue_type = str(issue_context.get("issue_type") or "").strip()
        if issue_status:
            constraints.append(f"Jira status: {issue_status}")
        if priority:
            constraints.append(f"Jira priority: {priority}")
        if issue_type:
            constraints.append(f"Issue type: {issue_type}")

    requested_outputs = {
        "jira_issue_plan": ["implementation_plan", "execution_steps", "risk_review"],
        "process_question": ["grounded_answer", "citations", "debug_focus_areas"],
        "jira_issue_create": ["structured_issue_payload"],
        "jira_issue_writeback": ["jira_writeback_payload"],
        "slack_message": ["channel_message_payload"],
        "internal_api_request": ["api_request_payload"],
        "internal_db_query": ["read_only_query_payload"],
        "action_with_approval": ["approval_gated_action_payload"],
    }.get(scenario, ["structured_request"])

    objective = summary or _short_request_summary(request_text)
    if scenario == "process_question":
        objective = f"Find the most relevant codebase evidence for: {_short_request_summary(request_text)}"
    elif scenario == "jira_issue_plan":
        objective = f"Build an implementation plan for {issue_reference.issue_key if issue_reference else 'the referenced Jira issue'}"

    confidence = 0.58
    if issue_reference:
        confidence += 0.1
    if issue_context:
        confidence += 0.12
    if candidate_modules:
        confidence += 0.08
    confidence = max(0.2, min(confidence, 0.92))

    return SemanticTranslationPayload(
        normalized_request=_short_request_summary(request_text),
        intent=_infer_intent(request_text, scenario=scenario),
        work_type=_infer_work_type(request_text, scenario=scenario),  # type: ignore[arg-type]
        objective=objective[:240],
        issue_key=issue_reference.issue_key if issue_reference else None,
        issue_url=issue_reference.issue_url if issue_reference else None,
        candidate_modules=candidate_modules,
        search_queries=search_queries,
        constraints=constraints,
        requested_outputs=requested_outputs,
        grounding_terms=grounding_terms,
        missing_information=[],
        confidence=round(confidence, 2),
    )


class SemanticTranslator:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def translate(
        self,
        *,
        task_id: str,
        request_text: str,
        scenario: str,
        actor_name: str,
        issue_context: dict[str, object] | None = None,
    ) -> SemanticTranslationResult:
        provider_mode = self.settings.semantic_translator_provider
        should_try_minimax = provider_mode == "minimax" or (
            provider_mode == "auto" and bool(self.settings.minimax_api_key)
        )

        if should_try_minimax:
            try:
                translation_payload = self._translate_with_minimax(
                    request_text=request_text,
                    scenario=scenario,
                    actor_name=actor_name,
                    issue_context=issue_context,
                )
                translation = _materialize_translation(
                    payload=translation_payload,
                    task_id=task_id,
                    provider={
                        "name": "minimax",
                        "mode": "chatcompletion_v2",
                        "model": self.settings.semantic_translator_model,
                    },
                )
                return SemanticTranslationResult(
                    translation=translation,
                    provider_name="minimax",
                    model_name=self.settings.semantic_translator_model,
                )
            except Exception as exc:
                fallback_translation = _materialize_translation(
                    payload=build_fallback_semantic_translation_payload(
                        request_text,
                        scenario=scenario,
                        issue_context=issue_context,
                    ),
                    task_id=task_id,
                    provider={
                        "name": "mock",
                        "mode": "fallback_after_minimax_error",
                        "requested_provider": "minimax",
                        "requested_model": self.settings.semantic_translator_model,
                        "error": str(exc),
                    },
                )
                return SemanticTranslationResult(
                    translation=fallback_translation,
                    provider_name="mock",
                    model_name=self.settings.semantic_translator_model,
                    used_fallback=True,
                    fallback_reason=str(exc),
                )

        fallback_translation = _materialize_translation(
            payload=build_fallback_semantic_translation_payload(
                request_text,
                scenario=scenario,
                issue_context=issue_context,
            ),
            task_id=task_id,
            provider={"name": "mock", "mode": "deterministic_semantic_translation"},
        )
        return SemanticTranslationResult(
            translation=fallback_translation,
            provider_name="mock",
            used_fallback=False,
        )

    def _translate_with_minimax(
        self,
        *,
        request_text: str,
        scenario: str,
        actor_name: str,
        issue_context: dict[str, object] | None,
    ) -> SemanticTranslationPayload:
        if not self.settings.minimax_api_key:
            raise RuntimeError("OPS_AGENT_MINIMAX_API_KEY is not configured")

        issue_context_lines: list[str] = []
        if issue_context:
            for field_name in ("issue_key", "summary", "description", "issue_status", "priority", "issue_type"):
                value = issue_context.get(field_name)
                if value:
                    issue_context_lines.append(f"{field_name}: {value}")

        payload = {
            "model": self.settings.semantic_translator_model,
            "messages": [
                {
                    "role": "system",
                    "name": "Ops Agent Semantic Translator",
                    "content": self._build_translation_instructions(),
                },
                {
                    "role": "user",
                    "name": actor_name,
                    "content": (
                        f"Scenario: {scenario}\n"
                        f"Request:\n{request_text}\n\n"
                        f"JiraContext:\n{chr(10).join(issue_context_lines) if issue_context_lines else 'N/A'}\n\n"
                        "Return only valid JSON that matches the requested schema."
                    ),
                },
            ],
        }

        headers = {
            "Authorization": f"Bearer {self.settings.minimax_api_key}",
            "Content-Type": "application/json",
        }

        with httpx.Client(timeout=self.settings.semantic_translator_timeout_seconds) as client:
            response = client.post(
                f"{self.settings.minimax_base_url.rstrip('/')}/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            response_payload = response.json()

        content = self._extract_minimax_content(response_payload)
        raw_translation = self._parse_json_content(content)
        return SemanticTranslationPayload.model_validate(raw_translation)

    @staticmethod
    def _build_translation_instructions() -> str:
        schema = {
            "normalized_request": "string",
            "intent": "string",
            "work_type": "bugfix|feature|investigation|operations|question|unknown",
            "objective": "string",
            "issue_key": "string|null",
            "issue_url": "string|null",
            "candidate_modules": ["string"],
            "search_queries": ["string"],
            "constraints": ["string"],
            "requested_outputs": ["string"],
            "grounding_terms": ["string"],
            "missing_information": ["string"],
            "confidence": "number_0_to_1",
        }
        return (
            "You normalize enterprise agent requests into a structured semantic translation. "
            "Your job is to convert natural language into concise machine-readable search and planning targets. "
            "Do not invent systems or files. Use Jira context when present. "
            "For code or debug requests, prefer search_queries that help a repository retrieval system find relevant code. "
            "Return compact JSON only with this shape: "
            f"{json.dumps(schema, ensure_ascii=True)}"
        )

    @staticmethod
    def _extract_minimax_content(response_payload: dict[str, Any]) -> str:
        choices = response_payload.get("choices")
        if not isinstance(choices, list):
            raise RuntimeError("MiniMax response did not include choices")
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content
        raise RuntimeError("MiniMax response did not include assistant content")

    @staticmethod
    def _parse_json_content(content: str) -> dict[str, Any]:
        normalized = content.strip()
        if normalized.startswith("```"):
            normalized = re.sub(r"^```(?:json)?\s*", "", normalized)
            normalized = re.sub(r"\s*```$", "", normalized)
        parsed = json.loads(normalized)
        if not isinstance(parsed, dict):
            raise RuntimeError("MiniMax response did not return a JSON object.")
        return SemanticTranslator._sanitize_translation_payload(parsed)

    @staticmethod
    def _sanitize_translation_payload(payload: dict[str, Any]) -> dict[str, Any]:
        sanitized = dict(payload)

        work_type = str(sanitized.get("work_type") or "").strip().lower()
        sanitized["work_type"] = work_type if work_type in WORK_TYPES else "unknown"

        for field_name, limit in LIST_FIELD_LIMITS.items():
            raw_value = sanitized.get(field_name)
            if not isinstance(raw_value, list):
                sanitized[field_name] = []
                continue

            cleaned_items: list[str] = []
            seen: set[str] = set()
            for item in raw_value:
                if not isinstance(item, str):
                    continue
                normalized_item = " ".join(item.strip().split())
                if not normalized_item:
                    continue
                dedupe_key = normalized_item.lower()
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                cleaned_items.append(normalized_item)
                if len(cleaned_items) >= limit:
                    break
            sanitized[field_name] = cleaned_items

        confidence = sanitized.get("confidence")
        if isinstance(confidence, (int, float)):
            sanitized["confidence"] = max(0.0, min(float(confidence), 1.0))
        else:
            sanitized["confidence"] = 0.5

        for optional_field in ("issue_key", "issue_url"):
            value = sanitized.get(optional_field)
            if not isinstance(value, str) or not value.strip():
                sanitized[optional_field] = None
            else:
                sanitized[optional_field] = value.strip()

        for required_field in ("normalized_request", "intent", "objective"):
            value = sanitized.get(required_field)
            if not isinstance(value, str) or not value.strip():
                raise RuntimeError(f"MiniMax response omitted required field {required_field}.")
            sanitized[required_field] = " ".join(value.strip().split())

        return sanitized
