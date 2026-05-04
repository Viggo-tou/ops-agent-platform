from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models.model_config import ModelEntry, ModelProvider, SelectedModel

SELECTED_MODEL_ID = "default"

DEFAULT_PROVIDER_CATALOG: list[dict[str, object]] = [
    {
        "name": "OpenAI",
        "note": "General reasoning and tool use",
        "models": ["GPT-5.4", "GPT-5.4 Mini", "GPT-4.1"],
    },
    {
        "name": "Anthropic",
        "note": "Long-context writing and coding",
        "models": ["Claude Opus 4.6", "Claude Sonnet 4.6", "Claude Haiku 4.5"],
    },
    {
        "name": "Google AI",
        "note": "Multimodal and fast assistant work",
        "models": ["Gemini 2.5 Pro", "Gemini 2.5 Flash"],
    },
    {
        "name": "DeepSeek",
        "note": "Reasoning and code-oriented tasks",
        "models": ["DeepSeek V3", "DeepSeek R1"],
    },
    {
        "name": "Aliyun",
        "note": "Domestic provider compatibility",
        "models": ["Qwen Max"],
    },
    {
        "name": "ZhipuAI AI",
        "note": "Domestic provider compatibility",
        "models": ["GLM-5"],
    },
    {
        "name": "Moonshot",
        "note": "Long-context Chinese and mixed-language tasks",
        "models": ["Kimi K2", "Kimi Turbo"],
    },
    {
        "name": "Mistral",
        "note": "Enterprise and coding workflows",
        "models": ["Mistral Large", "Codestral"],
    },
    {
        "name": "Cohere",
        "note": "RAG and enterprise retrieval",
        "models": ["Command R+", "Command A"],
    },
]


def slugify_model_id(display_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", display_name.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def bootstrap_model_catalog(db: Session) -> None:
    for provider_order, provider_definition in enumerate(DEFAULT_PROVIDER_CATALOG):
        provider_name = str(provider_definition["name"])
        provider = db.get(ModelProvider, provider_name)
        if provider is None:
            provider = ModelProvider(name=provider_name)
            db.add(provider)

        provider.note = str(provider_definition["note"])
        provider.sort_order = provider_order

        models = provider_definition["models"]
        if not isinstance(models, list):
            continue

        for model_order, display_name in enumerate(models):
            model_display_name = str(display_name)
            model_id = slugify_model_id(model_display_name)
            model_entry = db.get(ModelEntry, model_id)
            if model_entry is None:
                model_entry = ModelEntry(id=model_id)
                db.add(model_entry)

            model_entry.provider_name = provider_name
            model_entry.display_name = model_display_name
            model_entry.sort_order = model_order

    db.commit()


def list_providers(db: Session) -> list[ModelProvider]:
    stmt = (
        select(ModelProvider)
        .options(selectinload(ModelProvider.models))
        .order_by(ModelProvider.sort_order.asc(), ModelProvider.name.asc())
    )
    return list(db.scalars(stmt))


def _get_first_model(db: Session) -> ModelEntry | None:
    stmt = (
        select(ModelEntry)
        .join(ModelProvider, ModelEntry.provider_name == ModelProvider.name)
        .order_by(
            ModelProvider.sort_order.asc(),
            ModelProvider.name.asc(),
            ModelEntry.sort_order.asc(),
            ModelEntry.display_name.asc(),
        )
    )
    return db.scalars(stmt).first()


def get_selected_model(db: Session) -> SelectedModel:
    selected = db.get(SelectedModel, SELECTED_MODEL_ID)
    if selected is None:
        first_model = _get_first_model(db)
        selected = SelectedModel(
            id=SELECTED_MODEL_ID,
            model_id=first_model.id if first_model is not None else None,
        )
        db.add(selected)
        db.commit()
        db.refresh(selected)
    return selected


def set_selected_model(db: Session, model_id: str | None) -> SelectedModel:
    if model_id is not None and db.get(ModelEntry, model_id) is None:
        raise LookupError(f"Model not found: {model_id}")

    selected = get_selected_model(db)
    selected.model_id = model_id
    db.commit()
    db.refresh(selected)
    return selected
