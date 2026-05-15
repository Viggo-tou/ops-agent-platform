from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.service import build_domain_fast_path_plan  # noqa: E402
from app.services.domain_classifier import classify_domain  # noqa: E402


def _candidate(path: str, score: float) -> dict:
    return {
        "path": path,
        "score": score,
        "matched_terms": ["address", "map"],
        "reason": "signup/KYC address form candidate",
    }


def test_android_map_plan_fast_path_materializes_contract_plan() -> None:
    playbook = classify_domain(
        request_text=(
            "develop P69-19: add map-based address selection to signup and "
            "KYC address forms"
        ),
        project_tag="handymanapp",
    )

    result = build_domain_fast_path_plan(
        task_id="11111111-1111-1111-1111-111111111111",
        request_text="develop P69-19",
        scenario="jira_issue_develop",
        matched_playbook=playbook,
        issue_context={
            "summary": "Add map-based address selection to signup and KYC forms",
            "description": (
                "Existing customer and handyman address forms should use "
                "OSMDroid map picking and persist selected coordinates."
            ),
        },
        candidate_files=[
            _candidate(
                "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
                4.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
                2.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
                1.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
                3.0,
            ),
        ],
    )

    assert result is not None
    assert result.provider_name == "harness:android_map_location_plan"
    assert result.model_name == "deterministic-v1"
    assert result.plan.expected_new_files == []
    assert result.plan.must_touch_files == [
        "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
        "app/src/main/java/com/example/handyman/customer_pages/CustomerKYCAddressForm.kt",
        "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
        "app/src/main/java/com/example/handyman/handyman_pages/HandymanKYCAddressForm.kt",
    ]

    contract_ids = {
        test.contract_id for test in result.plan.acceptance_tests if test.contract_id
    }
    assert {
        "map_ui_present",
        "user_can_select_location",
        "location_updates_form_state",
    }.issubset(contract_ids)
    assert "OSMDroid" in result.plan.change_explanation


def test_android_map_plan_fast_path_ignores_unmatched_domains() -> None:
    result = build_domain_fast_path_plan(
        task_id="22222222-2222-2222-2222-222222222222",
        request_text="answer a knowledge question",
        scenario="process_question",
        matched_playbook={"id": "android_map_location"},
        issue_context={},
        candidate_files=[],
    )

    assert result is None


def test_job_default_address_fast_path_uses_job_posting_targets() -> None:
    playbook = classify_domain(
        request_text=(
            "Map Integration: Load default address when creating jobs. "
            "Pre-fill job location map using user saved home address."
        ),
        project_tag="handymanapp",
    )

    result = build_domain_fast_path_plan(
        task_id="33333333-3333-3333-3333-333333333333",
        request_text="develop P69-17",
        scenario="jira_issue_develop",
        matched_playbook=playbook,
        issue_context={
            "summary": "Map Integration: Load default address when creating jobs",
            "description": (
                "Pre-fill job location map using user saved home address. "
                "Updated work location is saved to the job instead of "
                "overwriting home address."
            ),
        },
        candidate_files=[
            _candidate(
                "app/src/main/java/com/example/handyman/CustomerJobDetailsFragment.kt",
                9.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/JobPostingFlow.kt",
                4.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/JobPostingFragment.kt",
                3.0,
            ),
            _candidate(
                "app/src/main/java/com/example/handyman/JobPostingViewModel.kt",
                2.0,
            ),
        ],
    )

    assert result is not None
    assert result.provider_name == "harness:android_map_location_plan"
    assert result.plan.must_touch_files == [
        "app/src/main/java/com/example/handyman/JobPostingFragment.kt",
        "app/src/main/java/com/example/handyman/JobPostingFlow.kt",
        "app/src/main/java/com/example/handyman/JobPostingViewModel.kt",
    ]
    assert "job creation location" in result.plan.objective.lower()
    assert "signup" not in result.plan.objective.lower()

    contract_ids = {
        test.contract_id for test in result.plan.acceptance_tests if test.contract_id
    }
    assert {
        "saved_home_address_loaded",
        "job_location_prefilled",
        "saved_address_geocoded_to_map",
    }.issubset(contract_ids)
