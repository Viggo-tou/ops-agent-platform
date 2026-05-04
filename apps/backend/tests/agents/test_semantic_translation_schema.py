from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.schemas import SemanticTranslationPayload  # noqa: E402


def _make_payload(intent: str) -> SemanticTranslationPayload:
    """Minimal valid payload with a given intent string."""
    return SemanticTranslationPayload(
        normalized_request="test request",
        intent=intent,
        work_type="feature",
        objective="test objective",
        confidence=0.5,
    )


def test_intent_accepts_up_to_320_chars() -> None:
    """A 320-char intent must pass validation (new max_length)."""
    long_intent = "x" * 320
    payload = _make_payload(intent=long_intent)
    assert payload.intent == long_intent


def test_intent_rejects_over_320_chars() -> None:
    """A 321-char intent must raise a Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        _make_payload(intent="x" * 321)


def test_intent_still_accepts_short() -> None:
    """Short (single-char) intents must still be accepted."""
    payload = _make_payload(intent="A")
    assert payload.intent == "A"


def test_intent_rejects_empty() -> None:
    """Empty intent must be rejected (min_length=1)."""
    with pytest.raises(ValidationError):
        _make_payload(intent="")
