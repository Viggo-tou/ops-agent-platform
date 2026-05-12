"""T-026-C: lock in that /api/model-config never leaks API keys.

The current ModelProviderRead / ModelEntryRead / SelectedModelRead schemas
deliberately have no `api_key`, `secret`, `token`, or `credential` field.
This test fails loudly if a future change ever adds one, so the "no secret
in response body" invariant is enforced at the schema level.
"""

from __future__ import annotations

from app.schemas.model_config import (
    ModelEntryRead,
    ModelProviderRead,
    SelectedModelRead,
    SelectedModelUpdate,
)

_FORBIDDEN_SUBSTRINGS = ("key", "secret", "token", "credential", "password")


def _assert_no_secret_fields(model_cls: type) -> None:
    fields = set(model_cls.model_fields.keys())
    leaks = [
        name
        for name in fields
        if any(bad in name.lower() for bad in _FORBIDDEN_SUBSTRINGS)
    ]
    assert not leaks, (
        f"{model_cls.__name__} exposes fields that look like secrets: {leaks}. "
        f"Secrets must never be serialized into model-config API responses."
    )


def test_model_provider_read_has_no_secret_fields() -> None:
    _assert_no_secret_fields(ModelProviderRead)


def test_model_entry_read_has_no_secret_fields() -> None:
    _assert_no_secret_fields(ModelEntryRead)


def test_selected_model_read_has_no_secret_fields() -> None:
    _assert_no_secret_fields(SelectedModelRead)


def test_selected_model_update_has_no_secret_fields() -> None:
    _assert_no_secret_fields(SelectedModelUpdate)
