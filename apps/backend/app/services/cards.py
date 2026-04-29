from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import upsert_knowledge_fts
from app.models.base import utcnow
from app.models.knowledge_card import KnowledgeCard
from app.models.knowledge_document import KnowledgeDocument

CARD_PROMPT_VERSION = "v1-card"


class CardGenerationError(RuntimeError):
    """Raised when a provider cannot produce a knowledge card."""


def _language_from_document(document: KnowledgeDocument) -> str:
    if document.language:
        return document.language
    suffix = Path(document.relative_path).suffix.lower()
    return suffix.lstrip(".") or "text"


def build_card_prompt(document: KnowledgeDocument, *, max_content_chars: int = 8000) -> str:
    content = document.content or ""
    if not content.strip():
        return (
            "You are summarizing a single source file from a code repository.\n\n"
            f"File: {document.relative_path}\n"
            f"Lines: {document.line_count}\n"
            f"Language: {_language_from_document(document)}\n\n"
            "Content:\n```\n\n```\n\n"
            "Content is empty. Output exactly:\n"
            f"**File**: {document.relative_path}\\n**Purpose**: (empty / non-code file)"
        )
    clipped = content[:max_content_chars]
    return (
        "You are summarizing a single source file from a code repository.\n\n"
        f"File: {document.relative_path}\n"
        f"Lines: {document.line_count}\n"
        f"Language: {_language_from_document(document)}\n\n"
        "Content:\n"
        "```\n"
        f"{clipped}\n"
        "```\n\n"
        "Write a markdown card of at most 400 words describing:\n"
        "1. **One-sentence purpose**: what this file does\n"
        "2. **Key exports**: name and brief role of each exported symbol (function/class/component)\n"
        "3. **Imports of note**: which external libs and which sibling files it depends on\n"
        "4. **Domain**: one tag from [auth, dashboard, data-fetching, ui-component, page, route, util, config, test]\n"
        "5. **Notable patterns**: any non-obvious responsibility\n\n"
        "Format:\n\n"
        "```markdown\n"
        f"**File**: {document.relative_path}\n"
        "**Purpose**: ...\n"
        "**Exports**: ...\n"
        "**Depends on**: ...\n"
        "**Domain**: ...\n"
        "**Notes**: ...\n"
        "```\n\n"
        "Be terse. No filler. If content is empty or non-code, output: "
        f"`**File**: {document.relative_path}\\n**Purpose**: (empty / non-code file)`."
    )


class CardGenerator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def generate(self, *, document: KnowledgeDocument) -> tuple[str, str]:
        """Return (card_text, model_name). Raises CardGenerationError on failure."""
        if not (document.content or "").strip():
            return (
                f"**File**: {document.relative_path}\n**Purpose**: (empty / non-code file)",
                self.settings.knowledge_cards_model,
            )
        provider = self.settings.knowledge_cards_provider.lower()
        if provider != "minimax":
            raise CardGenerationError(f"Unsupported card provider: {provider}")
        if not self.settings.minimax_api_key:
            raise CardGenerationError("OPS_AGENT_MINIMAX_API_KEY not configured")

        prompt = build_card_prompt(document)
        payload = {
            "model": self.settings.knowledge_cards_model,
            "messages": [
                {
                    "role": "system",
                    "name": "Ops Agent Knowledge Card Builder",
                    "content": "Write compact, factual markdown file cards for code retrieval.",
                },
                {"role": "user", "name": "user", "content": prompt},
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
            raise CardGenerationError(f"MiniMax card call failed: {exc}") from exc

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise CardGenerationError("MiniMax card response missing choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise CardGenerationError("MiniMax card response missing content")
        cleaned = content.strip()
        max_chars = int(getattr(self.settings, "knowledge_cards_max_chars", 2400))
        if max_chars > 0 and len(cleaned) > max_chars:
            cleaned = cleaned[:max_chars].rstrip()
        return cleaned, self.settings.knowledge_cards_model


def upsert_card(
    db: Session,
    *,
    document: KnowledgeDocument,
    card_text: str,
    model_name: str,
    card_version: str = CARD_PROMPT_VERSION,
) -> KnowledgeCard:
    card = db.execute(
        select(KnowledgeCard).where(KnowledgeCard.document_id == document.id)
    ).scalar_one_or_none()
    if card is None:
        card = KnowledgeCard(
            document_id=document.id,
            source_name=document.source_name,
            relative_path=document.relative_path,
            card_text=card_text,
            card_version=card_version,
            model_name=model_name,
            generated_at=utcnow(),
            content_hash=document.content_hash,
        )
        db.add(card)
    else:
        card.source_name = document.source_name
        card.relative_path = document.relative_path
        card.card_text = card_text
        card.card_version = card_version
        card.model_name = model_name
        card.generated_at = utcnow()
        card.content_hash = document.content_hash

    upsert_knowledge_fts(
        db,
        document_id=document.id,
        source_name=document.source_name,
        relative_path=document.relative_path,
        title=document.title,
        content=document.content,
        card_text=card_text,
    )
    return card
