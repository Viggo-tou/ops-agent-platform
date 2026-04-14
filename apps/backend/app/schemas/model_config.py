from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ModelEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Stable identifier of the model entry, assigned by the server.")
    display_name: str = Field(..., description="Human-readable name of the model shown in the UI.")
    sort_order: int = Field(..., description="Integer defining the display order among model entries.")


class ModelProviderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str = Field(..., description="Unique name of the model provider (e.g., OpenAI, Anthropic).")
    note: str = Field(..., description="Optional annotation or comment about the provider.")
    sort_order: int = Field(..., description="Integer defining the display order among providers.")
    models: list[ModelEntryRead] = Field(..., description="List of model entries available under this provider.")


class SelectedModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_id: str | None = Field(default=None, description="Identifier of the currently selected model, or null if none selected.")
    updated_at: datetime = Field(..., description="UTC timestamp when the model selection was last updated.")


class SelectedModelUpdate(BaseModel):
    model_id: str | None = Field(default=None, description="New model identifier to set, or null to deselect.")
