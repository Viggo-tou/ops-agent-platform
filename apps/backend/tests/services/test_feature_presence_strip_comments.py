"""Stage X.8.b improvement: feature_presence_check strips comments before
token grep so codegen can't fool the gate by stuffing tokens in `//`.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.feature_presence_check import (  # noqa: E402
    _strip_comments,
    evaluate_feature_presence,
)


def test_strip_line_comment_double_slash():
    assert _strip_comments("val x = 1 // homeAddress here").strip() == "val x = 1"


def test_strip_line_comment_hash():
    assert _strip_comments("x = 1 # homeAddress here").strip() == "x = 1"


def test_strip_block_comment():
    src = "before /* homeAddress in block */ after"
    out = _strip_comments(src)
    assert "homeAddress" not in out
    assert "before" in out
    assert "after" in out


def test_strip_xml_html_comment():
    src = '<TextView android:id="@+id/foo"/> <!-- homeAddress here -->'
    out = _strip_comments(src)
    assert "homeAddress" not in out
    assert "@+id/foo" in out


def test_p69_17_v8_failure_mode_caught():
    """The exact failure mode from v8: claude_code put 'home address' in
    a Kotlin comment but no actual code. Gate must NOT see it."""
    must_touch = ["src/Job.kt"]
    body = """package x
data class Job(
    // Work location address for this job (independent of home address).
    // Pre-filled from user's saved homeAddress on first load.
    val jobLocation: String = "",
    val latitude: Double? = null,
    // Work location coordinates from the map pin; null when no homeAddress resolved.
    val longitude: Double? = null,
)
"""
    result = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: body},
        required_tokens=["homeAddress", "getHomeAddress"],
    )
    # All instances of 'homeAddress' are in comments — should NOT match
    assert result.feature_absent is True
    assert "homeaddress" not in (
        " ".join(result.matched_per_file[must_touch[0]]).lower()
    )


def test_real_code_still_matches_after_strip():
    """If token is in actual code (not comment), it MUST still match."""
    must_touch = ["src/Foo.kt"]
    body = """class Foo {
    fun loadHomeAddress() {  // helper that reads home addr
        return SessionManager.getHomeAddress()
    }
}
"""
    result = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: body},
        required_tokens=["loadHomeAddress", "getHomeAddress"],
    )
    assert result.feature_absent is False
    matches = result.matched_per_file[must_touch[0]]
    assert "loadHomeAddress" in matches
    assert "getHomeAddress" in matches


def test_strip_preserves_line_count():
    src = "line1\nline2 // comment\nline3"
    out = _strip_comments(src)
    assert out.count("\n") == src.count("\n")


def test_strip_block_comment_multiline():
    src = "before\n/* line1 in block\nline2 in block */\nafter"
    out = _strip_comments(src)
    assert "line1" not in out
    assert "line2 in block" not in out
    assert "before" in out
    assert "after" in out
