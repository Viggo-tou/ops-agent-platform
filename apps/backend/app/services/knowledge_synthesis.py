from __future__ import annotations

import json
import re

import httpx

from app.core.config import Settings, get_settings
from app.schemas.knowledge import KnowledgeCitation


# Bumped when the synthesis system-prompt is materially revised. Recorded
# in KnowledgeAnswerTrace.synthesis_prompt_version so benchmark runs can
# attribute score changes to specific prompt revisions instead of guessing.
# History:
#   v1-baseline: original prompt, citations + concrete-answer constraint.
#   v2-claim-binding: answer prose plus structured claim-to-citation bindings.
SYNTHESIS_PROMPT_VERSION = "v2-claim-binding"


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
        """Return LLM-synthesized answer or raise so callers can fall back."""
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
    def _build_system_prompt(*, use_chinese: bool) -> str:
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
            return (
                "You are an enterprise codebase Q&A assistant. Answer in Chinese. "
                "The retrieval system has already selected candidate files and snippets as evidence.\n\n"
                f"{language_rule}\n"
                "Give a concrete, actionable answer grounded in the evidence snippets. "
                "If evidence is insufficient, say what is missing and what to add next. "
                "Do not invent file names, class names, methods, or code not present in evidence. "
                "Keep the tone natural and direct, like a senior engineer explaining to a teammate.\n\n"
                f"{claim_structure}"
            )
        return (
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
