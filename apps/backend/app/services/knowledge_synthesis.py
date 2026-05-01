from __future__ import annotations

import json
import hashlib
import re
import time

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.schemas.knowledge import KnowledgeCitation
from app.services.llm_telemetry import LlmCall, record_llm_call


# Bumped when the synthesis system-prompt is materially revised. Recorded
# in KnowledgeAnswerTrace.synthesis_prompt_version so benchmark runs can
# attribute score changes to specific prompt revisions instead of guessing.
# History:
#   v1-baseline: original prompt, citations + concrete-answer constraint.
#   v2-claim-binding: answer prose plus structured claim-to-citation bindings.
#   v3-multientity-coverage: require coverage for explicitly mentioned entities.
SYNTHESIS_PROMPT_VERSION = "v3-multientity-coverage"

ENTITY_PATTERN = re.compile(
    r"`[^`]{3,80}`"
    r"|(?<![A-Za-z0-9_])("
    r"[A-Z][A-Za-z0-9]+(?:Fragment|Activity|Adapter|ViewModel|Screen|Service|Controller|Component|Page)"
    r"|[A-Z][A-Za-z0-9]+\.(?:kt|js|tsx|ts|jsx|py|java|go)"
    r"|[a-z][a-z0-9_]*\.(?:kt|js|tsx|ts|jsx|py|java|go|xml|json|yml|yaml|gradle)"
    r")(?![A-Za-z0-9_])"
)
ENTITY_EXTENSIONS = (
    ".kt",
    ".js",
    ".tsx",
    ".ts",
    ".jsx",
    ".py",
    ".java",
    ".go",
    ".xml",
    ".json",
    ".yml",
    ".yaml",
    ".gradle",
)
COMMON_ENTITY_WORDS = {
    "the",
    "and",
    "or",
    "for",
    "with",
    "from",
    "into",
    "about",
    "class",
    "file",
    "files",
}


def _normalize_entities(raw_entities: list[str]) -> list[str]:
    entities: list[str] = []
    seen: set[str] = set()
    for raw_entity in raw_entities:
        entity = raw_entity.strip().strip("`").strip()
        if not entity:
            continue
        if entity.lower() in COMMON_ENTITY_WORDS:
            continue
        key = entity.lower()
        if key in seen:
            continue
        seen.add(key)
        entities.append(entity)
    return entities


def extract_question_entities(question: str) -> list[str]:
    """Return explicitly mentioned code entities in question order."""
    return _normalize_entities([match.group(0) for match in ENTITY_PATTERN.finditer(question or "")])


def _entity_search_terms(entity: str) -> list[str]:
    clean = entity.strip().strip("`").strip()
    if not clean:
        return []
    normalized_path = clean.replace("\\", "/")
    basename = normalized_path.rsplit("/", 1)[-1]
    core = basename
    for extension in ENTITY_EXTENSIONS:
        if core.lower().endswith(extension):
            core = core[: -len(extension)]
            break
    terms: list[str] = []
    for term in (clean, normalized_path, basename, core):
        if term and term.lower() not in {item.lower() for item in terms}:
            terms.append(term)
    return terms


def _entity_appears_in_answer(entity: str, answer: str) -> bool:
    """Return True when the entity or its extensionless file/class token appears."""
    if not answer:
        return False
    clean = entity.strip().strip("`").strip()
    not_covered_pattern = re.compile(
        rf"`?{re.escape(clean)}`?\s*:\s*not covered by retrieved evidence\.?",
        re.IGNORECASE,
    )
    if not_covered_pattern.search(answer):
        return False
    normalized_answer = answer.replace("\\", "/").lower()
    return any(term.lower() in normalized_answer for term in _entity_search_terms(clean))


def compute_question_entity_coverage(question: str, answer: str) -> dict[str, object]:
    mentioned_entities = extract_question_entities(question)
    covered_entities = [
        entity for entity in mentioned_entities if _entity_appears_in_answer(entity, answer)
    ]
    covered = set(covered_entities)
    omitted_entities = [entity for entity in mentioned_entities if entity not in covered]
    coverage_rate = (
        len(covered_entities) / len(mentioned_entities)
        if mentioned_entities
        else 1.0
    )
    return {
        "mentioned_entities": mentioned_entities,
        "covered_entities": covered_entities,
        "omitted_entities": omitted_entities,
        "multifile_mode_active": len(mentioned_entities) >= 2,
        "coverage_rate": coverage_rate,
    }


def _build_multi_entity_coverage_block(entities: list[str] | None) -> str:
    if not entities or len(entities) < 2:
        return ""
    entity_lines = "\n".join(f"  {index}. {entity}" for index, entity in enumerate(entities, start=1))
    return (
        "The user's question explicitly mentions the following code entities, in order:\n"
        f"{entity_lines}\n\n"
        "Your answer MUST include at least one specific factual claim about each\n"
        "of these entities. A \"specific factual claim\" is a sentence that references\n"
        "a concrete API, method, field, file path, data path, routing decision, or\n"
        "behavior of that entity. A generic mention like \"X is also part of this\n"
        "flow\" does NOT count.\n\n"
        "If the retrieved evidence does not contain enough information to make a\n"
        "specific claim about a mentioned entity, write exactly: \"<entity_name>:\n"
        "not covered by retrieved evidence.\" Do not omit it silently. Do not\n"
        "fabricate behavior.\n\n"
        "Structure: when answering a flow / comparison / multi-file question, use\n"
        "either ordered steps (for flow) or per-entity bullets (for comparison /\n"
        "listing). Avoid prose-only structure when >=3 entities are listed."
    )


class KnowledgeSynthesisError(RuntimeError):
    """Raised when MiniMax synthesis fails; caller must fall back."""


class KnowledgeSynthesizer:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        db: Session | None = None,
        task_id: str | None = None,
        actor_name: str | None = None,
    ):
        self.settings = settings or get_settings()
        self.db = db
        self.task_id = task_id
        self.actor_name = actor_name

    def synthesize(
        self,
        *,
        query: str,
        citations: list[KnowledgeCitation],
        hallucination_risk: str,
        route_kind: str,
        language: str | None,
    ) -> str:
        """Return LLM-synthesized answer or raise so callers can fall back."""
        if not self.settings.knowledge_synthesis_enabled:
            raise KnowledgeSynthesisError("synthesis disabled by config")
        if not self.settings.minimax_api_key:
            raise KnowledgeSynthesisError("OPS_AGENT_MINIMAX_API_KEY not configured")
        if not citations:
            raise KnowledgeSynthesisError("no citations to synthesize over")

        use_chinese = self._use_chinese(query=query, language=language)
        mentioned_entities = extract_question_entities(query)
        evidence_block = self._format_evidence(citations)
        system_prompt = self._build_system_prompt(
            use_chinese=use_chinese,
            mentioned_entities=mentioned_entities,
        )
        user_prompt = self._build_user_prompt(
            query=query,
            evidence_block=evidence_block,
            hallucination_risk=hallucination_risk,
            route_kind=route_kind,
            use_chinese=use_chinese,
        )

        started = time.perf_counter()
        prompt_fingerprint = hashlib.sha256(f"{system_prompt}\n{user_prompt}".encode("utf-8")).hexdigest()[:10]
        try:
            response_text, input_tokens, output_tokens = self._call_minimax(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:
            self._record_call(
                input_tokens=0,
                output_tokens=0,
                latency_ms=int((time.perf_counter() - started) * 1000),
                success=False,
                error_type=type(exc).__name__,
                prompt_fingerprint=prompt_fingerprint,
            )
            raise
        cleaned = response_text.strip()
        if not cleaned:
            error = KnowledgeSynthesisError("MiniMax returned empty answer")
            self._record_call(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                success=False,
                error_type=type(error).__name__,
                prompt_fingerprint=prompt_fingerprint,
            )
            raise error
        self._record_call(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            success=True,
            prompt_fingerprint=prompt_fingerprint,
        )
        return cleaned

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
                snippet = snippet[:limit] + "\n(truncated)"
            card_block = ""
            if citation.card_text:
                card = citation.card_text.strip()
                card_block = f"[CARD]\n{card}\n"
            parts.append(
                f"[{index}] {citation.source_name}:{citation.relative_path} "
                f"(lines {citation.line_start}-{citation.line_end}, score={citation.score})\n"
                f"{card_block}[CONTENT]\n{snippet}"
            )
        return "\n\n".join(parts)

    @staticmethod
    def _build_system_prompt(
        *,
        use_chinese: bool,
        mentioned_entities: list[str] | None = None,
    ) -> str:
        language_rule = (
            "Answer prose and claim restatements must be in Chinese."
            if use_chinese
            else "Answer prose and claim restatements must be in English."
        )
        claim_structure = (
            "Output ONLY this structure:\n\n"
            "<answer>\n"
            "[1-3 paragraph natural-language answer with inline <claim id=\"N\">...</claim> "
            "tags around each factual statement, where N is the 1-indexed claim number. "
            "Every factual claim about code MUST be inside a <claim> tag. Connective "
            "prose (\"here is how it works\", \"the relevant flow is...\") does not need a tag.]\n"
            "</answer>\n\n"
            "<claims>\n"
            "1. cite=[1,3] confidence=high - Brief restatement of claim 1.\n"
            "2. cite=[2] confidence=medium - Brief restatement of claim 2.\n"
            "...\n"
            "</claims>\n\n"
            "Rules:\n"
            "- Citation indices are 1-indexed and must reference numbered evidence snippets in the user prompt.\n"
            "- A claim with no supporting evidence: cite=[] confidence=low. Do NOT invent citation numbers.\n"
            "- 5-15 claims for a substantive question; 1-3 for a simple lookup.\n"
            "- confidence=high: the snippet contains the literal facts.\n"
            "- confidence=medium: the snippet contains supporting facts but inference is required.\n"
            "- confidence=low: the claim is informed inference, not directly in the snippet.\n"
            "- Do not output JSON or markdown code fences."
        )
        if use_chinese:
            prompt = (
                "You are an enterprise codebase Q&A assistant. Answer in Chinese. "
                "The retrieval system has already selected candidate files and snippets as evidence.\n\n"
                f"{language_rule}\n"
                "Give a concrete, actionable answer grounded in the evidence snippets. "
                "If evidence is insufficient, say what is missing and what to add next. "
                "Do not invent file names, class names, methods, or code not present in evidence. "
                "Keep the tone natural and direct, like a senior engineer explaining to a teammate.\n\n"
                f"{claim_structure}"
            )
            coverage_block = _build_multi_entity_coverage_block(mentioned_entities)
            return f"{prompt}\n\n{coverage_block}" if coverage_block else prompt
        prompt = (
            "You are an enterprise codebase Q&A assistant. The user asked about the codebase; "
            "the retrieval system has already selected candidate files and snippets as evidence.\n\n"
            f"{language_rule}\n"
            "Give a concrete, actionable answer grounded in the evidence snippets. "
            "If evidence is insufficient, say so honestly and tell the user what to add for the next query. "
            "Do not invent file names, class names, or methods not present in the evidence. "
            "Keep the tone natural and direct, like a senior engineer explaining to a teammate. "
            "Do not emit a rigid template.\n\n"
            f"{claim_structure}"
        )
        coverage_block = _build_multi_entity_coverage_block(mentioned_entities)
        return f"{prompt}\n\n{coverage_block}" if coverage_block else prompt

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
                f"User question:\n{query}\n\n"
                f"Retrieval route: {route_kind}\n"
                f"Confidence tag: {hallucination_risk}\n\n"
                f"Evidence snippets, ranked by relevance:\n{evidence_block}\n\n"
                "Produce the structured response exactly as specified. Use Chinese prose inside the tags."
            )
        return (
            f"User question:\n{query}\n\n"
            f"Retrieval route: {route_kind}\n"
            f"Confidence tag: {hallucination_risk}\n\n"
            f"Evidence snippets (ranked by relevance):\n{evidence_block}\n\n"
            "Produce the structured response exactly as specified."
        )

    def _call_minimax(self, *, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
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
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        return (
            content,
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
        )

    def _record_call(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        success: bool,
        prompt_fingerprint: str,
        error_type: str | None = None,
    ) -> None:
        if self.db is None:
            return
        record_llm_call(
            self.db,
            LlmCall(
                purpose="synthesis",
                provider="minimax",
                model=self.settings.knowledge_synthesis_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                success=success,
                error_type=error_type,
                prompt_fingerprint=prompt_fingerprint,
                task_id=self.task_id,
                actor_name=self.actor_name,
            ),
        )
