"""T2.1: ToolGateway retry loop applies exponential backoff with jitter
between attempts so transient 5xx responses do not get immediately
re-hit, which would amplify upstream pressure during incidents.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.tools.gateway import (  # noqa: E402
    _RETRY_BASE_DELAY_S,
    _RETRY_JITTER_S,
    _RETRY_MAX_DELAY_S,
    _retry_backoff_seconds,
)


def test_backoff_first_attempt_within_base_plus_jitter():
    # attempt=1 -> 2^0 * base = base. With jitter, between [base, base+jitter].
    for _ in range(50):
        s = _retry_backoff_seconds(1)
        assert _RETRY_BASE_DELAY_S <= s <= _RETRY_BASE_DELAY_S + _RETRY_JITTER_S


def test_backoff_doubles_per_attempt_until_cap():
    # attempt=2 -> 2*base, attempt=3 -> 4*base, ...
    for _ in range(20):
        s2 = _retry_backoff_seconds(2)
        s3 = _retry_backoff_seconds(3)
        assert 2 * _RETRY_BASE_DELAY_S <= s2 <= 2 * _RETRY_BASE_DELAY_S + _RETRY_JITTER_S
        assert 4 * _RETRY_BASE_DELAY_S <= s3 <= 4 * _RETRY_BASE_DELAY_S + _RETRY_JITTER_S


def test_backoff_capped_at_max():
    # Very high attempt should be capped at _RETRY_MAX_DELAY_S + jitter.
    s = _retry_backoff_seconds(20)
    assert s <= _RETRY_MAX_DELAY_S + _RETRY_JITTER_S


def test_backoff_zero_attempt_treated_as_one():
    # attempt=0 (defensive) shouldn't crash — should return at least base.
    s = _retry_backoff_seconds(0)
    assert s >= _RETRY_BASE_DELAY_S


def test_backoff_jitter_introduces_variation():
    """Multiple calls at same attempt must not all return identical values
    (jitter randomization sanity check)."""
    samples = {round(_retry_backoff_seconds(2), 4) for _ in range(30)}
    # With 30 samples in a 0.25s jitter window, we expect at least 5 unique.
    assert len(samples) >= 5
