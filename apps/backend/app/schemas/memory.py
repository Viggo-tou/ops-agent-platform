from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class MemoryItemBase(BaseModel):
    title: str = Field(min_length=1, max_length=255, description="User-defined name identifying the memory item.")
    body: str = Field(min_length=1, description="Main content or details stored in the memory item.")
    topic: str = Field(default="general", min_length=1, max_length=64, description="Category grouping for the memory item.")

    @field_validator("title", "body", "topic", mode="before")
    @classmethod
    def strip_text(cls, value: str) -> str:
        if isinstance(value, str):
            return value.strip()
        return value


class MemoryItemCreate(MemoryItemBase):
    pass


class MemoryItemUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255, description="User-defined name identifying the memory item.")
    body: str | None = Field(default=None, min_length=1, description="Main content or details stored in the memory item.")
    topic: str | None = Field(default=None, min_length=1, max_length=64, description="Category grouping for the memory item.")

    @field_validator("title", "body", "topic", mode="before")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        if isinstance(value, str):
            return value.strip()
        return value


class MemoryItemRead(MemoryItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: str = Field(..., description="Stable identifier of the memory item, assigned by the server.")
    created_at: datetime = Field(..., description="UTC timestamp when the memory item was first created.")
    updated_at: datetime = Field(..., description="UTC timestamp when the memory item was last modified.")


class MemorySettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool = Field(..., description="Whether the memory feature is active for this user.")
    allow_list: str = Field(..., description="Comma-separated list of domains or keywords the memory feature respects.")
    block_list: str = Field(..., description="Comma-separated list of domains or keywords the memory feature ignores.")
    updated_at: datetime = Field(..., description="UTC timestamp when the memory settings were last modified.")


class MemorySettingsUpdate(BaseModel):
    enabled: bool | None = Field(default=None, description="Whether the memory feature is active for this user.")
    allow_list: str | None = Field(default=None, description="Comma-separated list of domains or keywords the memory feature respects.")
    block_list: str | None = Field(default=None, description="Comma-separated list of domains or keywords the memory feature ignores.")
