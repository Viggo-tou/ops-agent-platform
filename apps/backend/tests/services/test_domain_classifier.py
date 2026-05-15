"""Domain classifier + playbook + empty-acceptance completeness gate
(v16.1 — addresses C1 plan_under_specified)."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.domain_classifier import (  # noqa: E402
    all_playbooks,
    classify_android_map_location_subtype,
    classify_domain,
    evaluate_acceptance_completeness,
    format_playbook_for_planner_prompt,
    is_feature_task,
    synthesize_acceptance_tests_from_playbook,
    synthesize_must_touch_files_from_candidates,
)


# ---- playbook loading ---------------------------------------------------


def test_android_map_location_playbook_exists():
    playbooks = all_playbooks()
    assert any(p.get("id") == "android_map_location" for p in playbooks), (
        "android_map_location.yaml must exist; it's the v16.1 reference "
        "playbook"
    )


def test_android_job_default_address_playbook_exists():
    playbooks = all_playbooks()
    assert any(p.get("id") == "android_job_default_address" for p in playbooks)


# ---- domain classifier --------------------------------------------------


def test_p69_19_request_text_matches_android_map_location():
    """The exact phrasing from today's b5d0a085 — `finish jira p69-19`
    augmented with the change summary — should match the map/location
    playbook via the `map`/`location` keywords."""
    pb = classify_domain(
        request_text="finish jira P69-19: add map-based address selection",
        project_tag="handymanapp",
    )
    assert pb is not None
    assert pb.get("id") == "android_map_location"


def test_project_tag_without_keywords_does_not_force_domain():
    """The source tag is too broad to be a domain trigger by itself."""
    pb = classify_domain(
        request_text="finish jira p69-19",
        project_tag="handymanapp",
    )
    assert pb is None


def test_p69_17_issue_text_matches_job_default_address_playbook():
    pb = classify_domain(
        request_text=(
            "Map Integration: Load default address when creating jobs. "
            "Pre-fill job location map using user saved home address. "
            "Updated work location is saved to the job instead of overwriting home address."
        ),
        project_tag="handymanapp",
    )
    assert pb is not None
    assert pb.get("id") == "android_job_default_address"
    assert classify_android_map_location_subtype(
        "Pre-fill job location map using user saved home address"
    ) == "job_default_address"


def test_keyword_match_without_project_tag():
    """A request that mentions map but on a different project still gets
    classified — the keyword is enough signal."""
    pb = classify_domain(
        request_text="implement address picker using map view",
        project_tag="someotherapp",
    )
    assert pb is not None
    assert pb.get("id") == "android_map_location"


def test_no_match_for_unrelated_request():
    pb = classify_domain(
        request_text="fix the button color on the login page",
        project_tag="someotherapp",
    )
    assert pb is None


def test_word_boundary_prevents_false_match():
    """Short keywords like `map` use word-boundary matching so `compare`
    or `mapping` (as parts of longer words) don't trigger."""
    pb = classify_domain(
        request_text="refactor the comparator to remove the mapping helper",
        project_tag="someotherapp",
    )
    assert pb is None


# ---- feature-task heuristic ---------------------------------------------


def test_is_feature_task_recognizes_english_verbs():
    for verb in ("implement", "add", "build", "create", "finish", "ship"):
        assert is_feature_task(f"{verb} the map picker"), f"{verb!r}"


def test_is_feature_task_recognizes_chinese_verbs():
    assert is_feature_task("新增 map picker")
    assert is_feature_task("实现地图选点功能")


def test_is_feature_task_rejects_pure_bugfix():
    """Pure bugfix language shouldn't fire the completeness gate — those
    runs can legitimately have small diffs without acceptance_tests."""
    assert not is_feature_task("fix the typo in the address label")
    assert not is_feature_task("refactor the address form helpers")


# ---- completeness evaluator ---------------------------------------------


def test_completeness_no_playbook_is_noop():
    """When no domain matched, the gate is a no-op (we have no contract
    baseline to enforce)."""
    result = evaluate_acceptance_completeness(
        playbook=None, acceptance_tests=[]
    )
    assert result["ok"] is True
    assert result["reason"] == "no_domain_playbook"


def test_completeness_empty_acceptance_tests_fails_for_map_domain():
    """The b5d0a085 regression case: planner emitted [] acceptance_tests
    for a map/location task → must fail the gate."""
    pb = classify_domain(
        request_text="finish jira p69-19: map picker",
        project_tag="handymanapp",
    )
    assert pb is not None
    result = evaluate_acceptance_completeness(
        playbook=pb, acceptance_tests=[]
    )
    assert result["ok"] is False
    assert result["on_failure"] == "PLAN_UNDER_SPECIFIED"
    assert result["retry_planner_once"] is True
    assert result["minimum_required"] >= 1
    assert len(result["missing_contracts"]) >= 3


def test_completeness_matches_by_contract_id():
    """If the planner explicitly tags its acceptance_test with
    `contract_id` matching a required contract, the gate counts it as
    referenced."""
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    acceptance = [
        {"contract_id": "map_ui_present", "pattern": "anything"},
        {"contract_id": "user_can_select_location", "pattern": "x"},
        {"contract_id": "location_updates_form_state", "pattern": "y"},
    ]
    result = evaluate_acceptance_completeness(
        playbook=pb, acceptance_tests=acceptance
    )
    assert result["ok"] is True
    assert set(result["matched_contracts"]) >= {
        "map_ui_present",
        "user_can_select_location",
        "location_updates_form_state",
    }


def test_completeness_matches_by_pattern_token_overlap():
    """If the planner emits a pattern that shares identifier tokens
    with a contract's verification pattern, that's a match. Today's
    common case: planner writes `MapView|SupportMapFragment|googleMap`
    and the contract has `MapView|SupportMapFragment|AndroidView`."""
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    acceptance = [
        {
            "kind": "diff_contains_pattern",
            "pattern": "MapView|SupportMapFragment|googleMap",
        },
        {
            "kind": "diff_contains_pattern_in_file",
            "pattern": "selectedGeoPoint|selectedLat",
        },
        {
            "kind": "diff_contains_pattern",
            "pattern": "reverseGeocode|getFromLocation",
        },
    ]
    result = evaluate_acceptance_completeness(
        playbook=pb, acceptance_tests=acceptance
    )
    assert result["ok"] is True
    # MapView matches map_ui_present; selectedLat matches
    # user_can_select_location; reverseGeocode/getFromLocation matches
    # location_updates_form_state.
    assert "map_ui_present" in result["matched_contracts"]
    assert "user_can_select_location" in result["matched_contracts"]
    assert "location_updates_form_state" in result["matched_contracts"]


def test_completeness_below_threshold_fails():
    """Only one matching pattern → below minimum (3) → fail."""
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    acceptance = [
        {"pattern": "Geocoder|GeoPoint"},  # only matches Geocoder-related
    ]
    result = evaluate_acceptance_completeness(
        playbook=pb, acceptance_tests=acceptance
    )
    assert result["ok"] is False


def test_synthesize_acceptance_tests_from_playbook_fills_minimum():
    """Empty domain acceptance tests can be deterministically backfilled
    from the matched playbook instead of asking the planner to restate
    known contracts."""
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    tests = synthesize_acceptance_tests_from_playbook(
        playbook=pb,
        acceptance_tests=[],
    )

    verdict = evaluate_acceptance_completeness(
        playbook=pb,
        acceptance_tests=tests,
    )
    assert verdict["ok"] is True
    assert len(tests) == verdict["minimum_required"]
    assert {t["contract_id"] for t in tests} >= {
        "map_ui_present",
        "user_can_select_location",
        "location_updates_form_state",
    }


def test_synthesize_acceptance_tests_preserves_existing_and_fills_missing():
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    existing = [
        {
            "kind": "diff_contains_pattern",
            "pattern": "MapView|SupportMapFragment",
            "rationale": "Planner supplied map UI check",
        }
    ]

    tests = synthesize_acceptance_tests_from_playbook(
        playbook=pb,
        acceptance_tests=existing,
    )

    assert tests[0]["pattern"] == existing[0]["pattern"]
    verdict = evaluate_acceptance_completeness(
        playbook=pb,
        acceptance_tests=tests,
    )
    assert verdict["ok"] is True
    assert len(tests) == verdict["minimum_required"]


def test_synthesize_must_touch_uses_signup_address_candidates():
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    candidates = [
        {
            "path": "app/src/main/java/com/example/handyman/CustomerJobDetailsFragment.kt",
            "score": 25.0,
            "matched_terms": ["details", "map", "address"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
            "score": 18.0,
            "matched_terms": ["address", "map"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
            "score": 17.0,
            "matched_terms": ["signup", "address"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
            "score": 16.0,
            "matched_terms": ["signup", "address"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
            "score": 14.0,
            "matched_terms": ["address"],
        },
    ]

    must_touch = synthesize_must_touch_files_from_candidates(
        playbook=pb,
        candidate_files=candidates,
        issue_text=(
            "Add map-based address selection to account signup flow. "
            "Both methods are available in the signup flow."
        ),
        existing_must_touch=[],
        expected_new_files=[],
    )

    assert must_touch == [
        "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
        "app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
        "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
        "app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
    ]


def test_synthesize_must_touch_uses_job_default_address_candidates():
    pb = classify_domain(
        request_text=(
            "Load default address when creating jobs; pre-fill job location "
            "from saved home address"
        ),
        project_tag="handymanapp",
    )
    candidates = [
        {
            "path": "app/src/main/java/com/example/handyman/CustomerJobDetailsFragment.kt",
            "score": 25.0,
            "matched_terms": ["details", "map", "address"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/JobPostingFragment.kt",
            "score": 18.0,
            "matched_terms": ["job", "address"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/JobPostingFlow.kt",
            "score": 17.0,
            "matched_terms": ["job", "location"],
        },
        {
            "path": "app/src/main/java/com/example/handyman/JobPostingViewModel.kt",
            "score": 12.0,
            "matched_terms": ["location"],
        },
    ]

    must_touch = synthesize_must_touch_files_from_candidates(
        playbook=pb,
        candidate_files=candidates,
        issue_text=(
            "Load default address when creating jobs; pre-fill job location "
            "from saved home address"
        ),
        existing_must_touch=[],
        expected_new_files=[],
    )

    assert must_touch == [
        "app/src/main/java/com/example/handyman/JobPostingFragment.kt",
        "app/src/main/java/com/example/handyman/JobPostingFlow.kt",
        "app/src/main/java/com/example/handyman/JobPostingViewModel.kt",
    ]


# ---- planner prompt block ----------------------------------------------


def test_format_playbook_emits_contract_signals():
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp"
    )
    text = format_playbook_for_planner_prompt(pb)
    assert "DOMAIN PLAYBOOK MATCHED" in text
    assert "android_map_location" in text
    assert "map_ui_present" in text
    assert "user_can_select_location" in text
    # Prompt should communicate the completeness threshold to the model.
    assert "at least" in text.lower() or "minimum" in text.lower()


def test_format_playbook_handles_none_gracefully():
    assert format_playbook_for_planner_prompt({}) == ""
    assert format_playbook_for_planner_prompt(None) == ""  # type: ignore[arg-type]
