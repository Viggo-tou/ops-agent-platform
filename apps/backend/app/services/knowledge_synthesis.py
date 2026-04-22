from __future__ import annotations

import json
import re

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
                "You are an enterprise codebase Q&A assistant. Answer in Chinese. "
                "The retrieval system has already selected candidate files and snippets as evidence.\n\n"
                "Your job:\n"
                "1. Give a concrete, actionable answer grounded in the evidence snippets.\n"
                "2. Cite specific files and line ranges using `source:path (lines X-Y)`.\n"
                "3. If evidence is insufficient, say what is missing and what to add next.\n"
                "4. Do not invent file names, class names, methods, or code not present in evidence.\n"
                "5. Keep the tone natural and direct, like a senior engineer explaining to a teammate.\n"
                "6. Do not output JSON or markdown code fences. Plain prose only."
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
                f"User question:\n{query}\n\n"
                f"Retrieval route: {route_kind}\n"
                f"Confidence tag: {hallucination_risk}\n\n"
                f"Evidence snippets, ranked by relevance:\n{evidence_block}\n\n"
                "Answer the user's question in Chinese based on the evidence above."
            )
        return (
            f"User question:\n{query}\n\n"
            f"Retrieval route: {route_kind}\n"
            f"Confidence tag: {hallucination_risk}\n\n"
            f"Evidence snippets (ranked by relevance):\n{evidence_block}\n\n"
            "Answer the user's question based on the evidence above."
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
