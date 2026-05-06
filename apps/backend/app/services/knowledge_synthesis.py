from __future__ import annotations

import json
import hashlib
import re
import time
import traceback

import httpx
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.enums import EventSource, EventType, RoleName, WorkflowStage
from app.core.timeouts import external_http_timeout
from app.schemas.knowledge import KnowledgeCitation
from app.services.events import commit_checkpoint, record_event
from app.services.llm_cache import cached_http_post
from app.services.llm_telemetry import LlmCall, record_llm_call


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
        synth_provider = str(getattr(self.settings, "knowledge_synthesis_provider", "minimax") or "minimax").lower()
        if synth_provider == "minimax" and not self.settings.minimax_api_key:
            raise KnowledgeSynthesisError("OPS_AGENT_MINIMAX_API_KEY not configured")
        # deepseek path checks deepseek_api_key inside _call_deepseek.
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

        started = time.perf_counter()
        prompt_fingerprint = hashlib.sha256(f"{system_prompt}\n{user_prompt}".encode("utf-8")).hexdigest()[:10]
        request_size_bytes = len(f"{system_prompt}\n{user_prompt}".encode("utf-8"))
        self._record_synthesis_lifecycle(
            event_type=EventType.SYNTHESIS_CALL_STARTED,
            message="Knowledge synthesis MiniMax call started.",
            request_size_bytes=request_size_bytes,
            duration_ms=0,
            checkpoint_label="synthesis_call_started",
        )
        try:
            provider = str(getattr(self.settings, "knowledge_synthesis_provider", "minimax") or "minimax").lower()
            if provider == "deepseek":
                call_fn = self._call_deepseek
            else:
                call_fn = self._call_minimax
            response_text, input_tokens, output_tokens = call_fn(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        except Exception as exc:
            self._record_synthesis_lifecycle(
                event_type=EventType.SYNTHESIS_CALL_FAILED,
                message="Knowledge synthesis MiniMax call failed.",
                request_size_bytes=request_size_bytes,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=exc,
                checkpoint_label="synthesis_call_failed",
            )
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
            self._record_synthesis_lifecycle(
                event_type=EventType.SYNTHESIS_CALL_FAILED,
                message="Knowledge synthesis MiniMax call returned an empty answer.",
                request_size_bytes=request_size_bytes,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=error,
                checkpoint_label="synthesis_call_failed",
            )
            self._record_call(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=int((time.perf_counter() - started) * 1000),
                success=False,
                error_type=type(error).__name__,
                prompt_fingerprint=prompt_fingerprint,
            )
            raise error
        self._record_synthesis_lifecycle(
            event_type=EventType.SYNTHESIS_CALL_SUCCEEDED,
            message="Knowledge synthesis MiniMax call completed.",
            request_size_bytes=request_size_bytes,
            duration_ms=int((time.perf_counter() - started) * 1000),
            checkpoint_label="synthesis_call_succeeded",
        )
        self._record_call(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
            success=True,
            prompt_fingerprint=prompt_fingerprint,
        )
        return cleaned

    def _record_synthesis_lifecycle(
        self,
        *,
        event_type: EventType,
        message: str,
        request_size_bytes: int,
        duration_ms: int,
        checkpoint_label: str,
        error: Exception | None = None,
    ) -> None:
        if self.db is None or not self.task_id:
            return
        payload: dict[str, object] = {
            "provider_name": "minimax",
            "model_name": self.settings.knowledge_synthesis_model,
            "request_size_bytes": request_size_bytes,
            "duration_ms": duration_ms,
        }
        if error is not None:
            trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
            payload.update(
                {
                    "error_type": type(error).__name__,
                    "error_message": str(error)[:1000],
                    "traceback": trace[:2000],
                }
            )
        record_event(
            self.db,
            task_id=self.task_id,
            event_type=event_type,
            source=EventSource.ORCHESTRATOR,
            stage=WorkflowStage.KNOWLEDGE,
            role=RoleName.KNOWLEDGE,
            message=message,
            payload=payload,
        )
        commit_checkpoint(self.db, label=checkpoint_label)

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

    def _call_deepseek(self, *, system_prompt: str, user_prompt: str) -> tuple[str, int, int]:
        """Call DeepSeek API (OpenAI-compatible) for knowledge synthesis.

        NOTE: settings.deepseek_base_url may be configured for the
        Anthropic-compat path used by the deepseek_agent.py wrapper.
        /chat/completions only exists on the OpenAI-compat path, so
        hardcode that here. Same pattern as cc_agent_loop / codegen.
        """
        if not getattr(self.settings, "deepseek_api_key", None):
            raise KnowledgeSynthesisError("OPS_AGENT_DEEPSEEK_API_KEY not configured for synthesis")
        deepseek_chat_url = "https://api.deepseek.com/v1/chat/completions"
        # Prefer per-feature override; fall back to global deepseek_model
        # (e.g. deepseek-v4-pro from .env) so this branch picks up the same
        # model that cc_agent_loop and codegen use unless explicitly overridden.
        model_name = (
            getattr(self.settings, "knowledge_synthesis_deepseek_model", None)
            or getattr(self.settings, "deepseek_model", None)
            or "deepseek-chat"
        )
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = cached_http_post(
                url=deepseek_chat_url,
                json=payload,
                headers=headers,
                timeout=external_http_timeout(self.settings.knowledge_synthesis_timeout_seconds),
                provider_hint="knowledge_synthesis.deepseek",
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise KnowledgeSynthesisError(f"DeepSeek synthesis call failed: {exc}") from exc
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        usage = data.get("usage", {})
        try:
            from app.services.llm_telemetry import log_llm_cache_hit
            log_llm_cache_hit(
                provider="deepseek",
                model=model_name,
                purpose="knowledge_synthesis",
                usage=usage,
            )
        except Exception:  # noqa: BLE001
            pass
        return (
            str(content),
            int(usage.get("prompt_tokens", 0) or 0),
            int(usage.get("completion_tokens", 0) or 0),
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
            with httpx.Client(
                timeout=external_http_timeout(self.settings.knowledge_synthesis_timeout_seconds)
            ) as client:
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
