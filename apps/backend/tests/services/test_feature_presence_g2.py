"""G2: feature_presence_check strict tokens + diff-scoped scan.

Defeats the v10b cheating mode where codegen produced:
  - a UI shell file (an empty <EditText> in XML) and
  - a comment-only edit in a Kotlin data class
…and feature_presence still passed because pre-existing identifiers
(``location``, ``job``, ``Job``) in the file matched generic English
tokens like ``location`` derived from the planner step descriptions.

G2 introduces three orthogonal hardening axes:
  1. Strict token derivation: only identifier-shaped tokens
     (CamelCase / snake_case) survive; generic English dropped.
  2. Diff-scoped scan: scan only the lines added by the patch,
     not the full post-apply file body.
  3. Ratio threshold: require >=50% of derived tokens (not >=1).
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.feature_presence_check import (  # noqa: E402
    _is_identifier_shaped,
    derive_required_tokens_strict,
    evaluate_feature_presence,
    extract_added_lines_per_file,
)


# --- _is_identifier_shaped --------------------------------------------------

def test_identifier_shaped_camelcase():
    assert _is_identifier_shaped("homeAddress")
    assert _is_identifier_shaped("JobPostingFlow")


def test_identifier_shaped_snake_case():
    assert _is_identifier_shaped("home_address")
    assert _is_identifier_shaped("save_to_db")
    assert _is_identifier_shaped("HOME_ADDRESS")


def test_identifier_shaped_rejects_plain_english():
    # The whole point: plain English words are NOT identifier-shaped.
    assert not _is_identifier_shaped("home")
    assert not _is_identifier_shaped("address")
    assert not _is_identifier_shaped("location")
    assert not _is_identifier_shaped("Implement")  # cap'd but no internal boundary
    assert not _is_identifier_shaped("Jira")


# --- derive_required_tokens_strict ------------------------------------------

def test_strict_tokens_drops_generic_english():
    """Planner-step boilerplate words must not appear in strict tokens."""
    out = derive_required_tokens_strict(
        objective="Implement Jira P69-17 by generating code changes and applying patches",
        grounding_terms=["home address", "job creation"],
        spec_text="Pre-fill the address field on first load",
    )
    # No generic English allowed.
    for t in out:
        assert _is_identifier_shaped(t), f"non-identifier token leaked: {t!r}"
    # Specific stop words explicitly excluded.
    assert "Implement" not in out
    assert "Jira" not in out
    assert "code" not in out
    assert "address" not in out
    assert "home" not in out


def test_strict_tokens_keeps_camel_case():
    out = derive_required_tokens_strict(
        objective="Wire homeAddress into JobPostingFlow",
        grounding_terms=[],
        spec_text="",
    )
    assert "homeAddress" in out
    assert "JobPostingFlow" in out


def test_strict_tokens_keeps_snake_case_path_basenames():
    out = derive_required_tokens_strict(
        objective="",
        grounding_terms=[],
        spec_text="",
        must_touch_files=["src/session_manager.py", "src/Foo.kt"],
    )
    assert "session_manager" in out
    # "Foo" alone is just capitalized — no boundary, no underscore — drop.
    assert "Foo" not in out


def test_strict_tokens_returns_empty_when_only_generic():
    """If the spec mentions only natural-language English, strict mode
    yields an empty token list — caller is expected to fall back."""
    out = derive_required_tokens_strict(
        objective="Implement Jira issue by changing files",
        grounding_terms=["home address", "user profile"],
        spec_text="Allow user to fill the address field",
    )
    assert out == []


# --- extract_added_lines_per_file -------------------------------------------

def test_extract_added_lines_basic_diff():
    diff = (
        "diff --git a/src/Foo.py b/src/Foo.py\n"
        "index 0000001..0000002 100644\n"
        "--- a/src/Foo.py\n"
        "+++ b/src/Foo.py\n"
        "@@ -1,2 +1,4 @@\n"
        " import os\n"
        "+from session_manager import getHomeAddress\n"
        "+\n"
        "+homeAddress = getHomeAddress()\n"
    )
    out = extract_added_lines_per_file(diff)
    assert "src/Foo.py" in out
    body = out["src/Foo.py"]
    assert "from session_manager import getHomeAddress" in body
    assert "homeAddress = getHomeAddress()" in body
    # context line (' import os') must NOT be in additions
    assert "import os" not in body


def test_extract_added_lines_skips_plus_plus_plus_header():
    """The '+++ b/path' header is itself a line starting with '+', but it
    must not contaminate the added-lines stream."""
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+homeAddress = 1\n"
    )
    out = extract_added_lines_per_file(diff)
    assert out["x.py"] == "homeAddress = 1"
    # Should NOT contain the header literal
    assert "++ b/x.py" not in out["x.py"]


def test_extract_added_lines_multiple_files():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -0,0 +1 @@\n"
        "+symA = 1\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -0,0 +1 @@\n"
        "+symB = 2\n"
    )
    out = extract_added_lines_per_file(diff)
    assert out["a.py"] == "symA = 1"
    assert out["b.py"] == "symB = 2"


def test_extract_added_lines_empty_diff():
    assert extract_added_lines_per_file("") == {}
    assert extract_added_lines_per_file(None) == {}  # type: ignore[arg-type]


# --- evaluate_feature_presence: diff-scoped + ratio -------------------------

def test_v10b_cheat_caught_diff_scope():
    """The exact v10b failure mode: codegen edits leave file with
    pre-existing tokens like ``location`` / ``Job`` but the diff
    additions have neither of the strict required tokens."""
    must_touch = ["app/src/main/java/Job.kt"]
    # Final file content (post-apply) — has the old class-level identifiers.
    file_body = (
        "data class Job(\n"
        "  // Pre-filled from the user's home address on first load.\n"  # comment cheat
        "  val jobLocation: String = \"\",\n"
        "  val latitude: Double? = null,\n"
        ")\n"
    )
    # Diff added only comments + an unchanged-context line. After
    # strip-comments, additions contain nothing useful.
    diff_added = (
        "  // Pre-filled from the user's home address on first load.\n"
        "  // Work location coordinates.\n"
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: file_body},
        required_tokens=["homeAddress", "getHomeAddress", "SessionManager"],
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True
    assert "diff-added lines" in res.reason


def test_real_implementation_passes_diff_scope():
    """If the diff actually adds lines that contain the required
    identifier tokens, the gate passes."""
    must_touch = ["app/src/main/java/Job.kt"]
    file_body = "irrelevant — diff-mode ignores this anyway"
    diff_added = (
        "import com.example.SessionManager\n"
        "val homeAddress = SessionManager.getHomeAddress()\n"
        "val pinned = homeAddress.coords\n"
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: file_body},
        required_tokens=["homeAddress", "getHomeAddress", "SessionManager"],
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is False
    matches = res.matched_per_file[must_touch[0]]
    # Should match all three (case-insensitive)
    assert "homeAddress" in matches
    assert "getHomeAddress" in matches
    assert "SessionManager" in matches


def test_ratio_threshold_blocks_one_token_match():
    """Pre-G2 the gate accepted any ≥1 hit. Now ratio=0.5 of 4 tokens
    = at least 2 hits required."""
    must_touch = ["src/x.py"]
    diff_added = "homeAddress = 1\n"  # only 1 of 4 tokens
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["homeAddress", "getHomeAddress", "SessionManager", "saveAddress"],
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True


def test_legacy_full_file_mode_still_works():
    """When diff_added_per_file is None, behavior matches pre-G2 — the
    full file is scanned and >=1 token suffices unless ratio is set."""
    must_touch = ["src/x.py"]
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: "homeAddress = 42\n"},
        required_tokens=["homeAddress", "getHomeAddress"],
    )
    assert res.feature_absent is False
    assert "file content" in res.reason


def test_ratio_with_one_token_total_triggers_sparse_fallback():
    """Edge case: only 1 required token (< sparse_token_threshold=3), so
    the sparse-token fallback fires instead of the ratio check.
    The fallback requires >=3 unique identifier-shaped tokens in diff.
    A diff with only 1 identifier ('homeAddress') is rejected — that's
    the architectural intent: prose-only specs still require substantive
    structured codegen, not just one token."""
    must_touch = ["src/x.py"]
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["homeAddress"],
        diff_added_per_file={must_touch[0]: "homeAddress = 1\n"},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True
    # If we add 2 more identifiers, the fallback now passes.
    res2 = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["homeAddress"],
        diff_added_per_file={
            must_touch[0]: "homeAddress = 1\nworkAddress = 2\nsavedAddress = 3\n"
        },
        min_tokens_per_file_ratio=0.5,
    )
    assert res2.feature_absent is False


def test_sparse_token_fallback_accepts_real_implementation():
    """G2 sparse-token fallback: when the spec yields fewer than 3 strict
    tokens (prose-only Jira ticket), the gate falls back to "diff added
    >=3 unique identifier-shaped tokens per file". v12 of P69-17 added
    workAddress/workLatitude/workLongitude (3 unique CamelCase ids in
    Job.kt) — real work, must pass even with sparse spec tokens."""
    must_touch = ["app/src/main/java/Job.kt"]
    diff_added = (
        "  val workAddress: String? = null,\n"
        "  val workLatitude: Double? = null,\n"
        "  val workLongitude: Double? = null,\n"
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["fragment_job_posting"],  # only 1 strict token (sparse)
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is False
    assert "sparse-token fallback" in res.reason


def test_sparse_token_fallback_rejects_v10b_shell_only():
    """The sparse-token fallback must STILL block the v10b cheat —
    only English comments + UI primitives in the diff additions, no
    structured identifiers."""
    must_touch = ["app/src/main/java/Job.kt"]
    diff_added = (
        "  // Pre-filled from the user's home address on first load.\n"
        "  // Work location coordinates.\n"
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["fragment_job_posting"],
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True
    assert "sparse-token fallback" in res.reason


def test_sparse_token_fallback_rejects_diff_with_only_two_unique_ids():
    """Threshold is >=3 unique identifiers — exactly 2 should still fail."""
    must_touch = ["src/x.py"]
    diff_added = (
        "userName = 1\n"
        "userId = 2\n"
        "x = 3\n"  # 'x' is not identifier-shaped (no Camel/snake)
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["main"],  # 1 sparse token
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True


def test_sparse_token_fallback_NOT_used_when_spec_is_rich():
    """When required_tokens >=3, the strict ratio check fires and the
    fallback path is bypassed — even if the diff has many identifiers."""
    must_touch = ["src/x.py"]
    diff_added = "irrelevant_id_one = 1\nirrelevant_id_two = 2\n"
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["loadHomeAddress", "saveHomeAddress",
                         "getHomeAddress", "SessionManager"],  # 4 strict
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    # Strict check runs: needs >=2 of 4 strict tokens. Diff has 0. Fail.
    assert res.feature_absent is True


def test_count_unique_identifiers_camelcase_and_snake_case():
    from app.services.feature_presence_check import count_unique_identifiers_in_text
    txt = "homeAddress workAddress save_to_db x = y_value"
    # 'homeAddress', 'workAddress', 'save_to_db', 'y_value' (4 ids)
    # 'x' alone is not identifier-shaped
    assert count_unique_identifiers_in_text(txt) == 4


def test_count_unique_identifiers_drops_generic_english():
    from app.services.feature_presence_check import count_unique_identifiers_in_text
    # Even though "View" is capitalized, it's in generic stopwords.
    # 'address_field' is identifier-shaped + not stopword -> counted.
    txt = "View Page Status address_field"
    assert count_unique_identifiers_in_text(txt) == 1


def test_strip_comments_still_active_in_diff_mode():
    """G2 doesn't break X.8.b: tokens parked inside `//` comments in
    the diff additions still get stripped before grep."""
    must_touch = ["src/x.py"]
    diff_added = (
        "// homeAddress getHomeAddress SessionManager\n"  # all in comment
        "val unrelated = 1\n"
    )
    res = evaluate_feature_presence(
        must_touch_files=must_touch,
        file_contents={must_touch[0]: ""},
        required_tokens=["homeAddress", "getHomeAddress", "SessionManager"],
        diff_added_per_file={must_touch[0]: diff_added},
        min_tokens_per_file_ratio=0.5,
    )
    assert res.feature_absent is True
