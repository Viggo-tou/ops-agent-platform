from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.claim_extraction import extract_claims  # noqa: E402


def test_extract_claims_from_well_formed_synthesis() -> None:
    raw = """
<answer>
The admin login path starts when <claim id="1">handleLogin reads the email and password from state.</claim>
Then <claim id="2">the flow checks the admin credentials from mockUsers.js.</claim>
Finally, <claim id="3">a successful admin login navigates to the dashboard.</claim>
</answer>

<claims>
1. cite=[1] confidence=high - handleLogin reads email and password.
2. cite=[2,3] confidence=medium - Admin credentials are checked.
3. cite=[4] confidence=high chunk_kind=function - Successful admin login navigates to dashboard.
</claims>
"""

    answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=4)

    assert "<claim" not in answer
    assert "handleLogin reads the email and password from state." in answer
    assert len(claims) == 3
    assert claims[0].text == "handleLogin reads the email and password from state."
    assert claims[0].citation_indices == [0]
    assert claims[0].confidence == "high"
    assert claims[1].citation_indices == [1, 2]
    assert claims[1].confidence == "medium"
    assert claims[2].chunk_kind_hint == "function"
    assert ungrounded_count == 0


def test_extract_claims_tolerates_missing_closing_claims_tag() -> None:
    raw = """
<answer><claim id="1">Login compares the submitted password with the stored admin password.</claim></answer>
<claims>
1. cite=[1] confidence=high - Login compares submitted and stored passwords.
"""

    answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=1)

    assert answer == "Login compares the submitted password with the stored admin password."
    assert len(claims) == 1
    assert claims[0].citation_indices == [0]
    assert ungrounded_count == 0


def test_extract_claims_falls_back_when_answer_block_missing() -> None:
    raw = "Plain prose with handymanapp:src/auth.py (lines 1-2)."

    answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=4)

    assert answer == raw
    assert claims == []
    assert ungrounded_count == 0


def test_extract_claims_drops_out_of_range_citation_indices() -> None:
    raw = """
<answer><claim id="1">The login flow writes the session to localStorage.</claim></answer>
<claims>
1. cite=[7,0,-1] confidence=high - The login flow writes session state.
</claims>
"""

    _answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=4)

    assert len(claims) == 1
    assert claims[0].citation_indices == []
    assert claims[0].confidence == "high"
    assert ungrounded_count == 1


def test_extract_claims_records_empty_cite_list_as_ungrounded() -> None:
    raw = """
<answer><claim id="1">The dashboard route is probably protected by role checks.</claim></answer>
<claims>
1. cite=[] confidence=medium - Dashboard route protection is inferred.
</claims>
"""

    _answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=2)

    assert len(claims) == 1
    assert claims[0].citation_indices == []
    assert claims[0].confidence == "medium"
    assert ungrounded_count == 1


def test_extract_claims_coerces_unknown_confidence_to_low() -> None:
    raw = """
<answer><claim id="1">The form submit handler is named handleLogin.</claim></answer>
<claims>
1. cite=[1] confidence=unsure - The handler is named handleLogin.
</claims>
"""

    _answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=1)

    assert len(claims) == 1
    assert claims[0].citation_indices == [0]
    assert claims[0].confidence == "low"
    assert ungrounded_count == 0


def test_extract_claims_strips_invalid_zero_id_and_keeps_default_claim() -> None:
    raw = """
<answer>Here is the relevant point: <claim id="0">This claim has an invalid id but should stay readable.</claim></answer>
<claims>
</claims>
"""

    answer, claims, ungrounded_count = extract_claims(raw_synthesis=raw, citation_count=1)

    assert answer == "Here is the relevant point: This claim has an invalid id but should stay readable."
    assert len(claims) == 1
    assert claims[0].text == "This claim has an invalid id but should stay readable."
    assert claims[0].citation_indices == []
    assert claims[0].confidence == "low"
    assert ungrounded_count == 1
