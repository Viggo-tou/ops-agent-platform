from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.evidence_bundle import (  # noqa: E402
    _extract_intent_identifiers,
    _smart_prefetch_intent_files,
    build_evidence_bundle,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        evidence_must_touch_excluded_extensions="",
        evidence_must_touch_excluded_path_segments="",
        evidence_must_touch_excluded_filenames="",
        evidence_must_touch_include_configs=True,
    )


def test_extract_intent_identifiers_picks_home_address() -> None:
    request = "Pre-fill the map with the user's saved home address"

    assert "homeaddress" in _extract_intent_identifiers(request)


def test_extract_intent_identifiers_handles_multiple() -> None:
    request = "Show user's email address and phone number"

    assert _extract_intent_identifiers(request) == ["emailaddress", "phonenumber"]


def test_extract_intent_identifiers_returns_empty_when_no_match() -> None:
    assert _extract_intent_identifiers("Add a new feature") == []


def test_smart_prefetch_finds_files_with_matching_content(tmp_path: Path) -> None:
    (tmp_path / "a.kt").write_text("val homeAddress = profile.homeAddress", encoding="utf-8")
    (tmp_path / "b.kt").write_text("val foo = 1", encoding="utf-8")
    (tmp_path / "c.txt").write_text("homeaddress = bar", encoding="utf-8")

    result = _smart_prefetch_intent_files(
        request_text="saved home address",
        source_root=tmp_path,
        existing_anchored_paths=set(),
    )

    assert "a.kt" in result
    assert "c.txt" in result
    assert "b.kt" not in result


def test_smart_prefetch_excludes_already_anchored(tmp_path: Path) -> None:
    (tmp_path / "a.kt").write_text("val homeAddress = profile.homeAddress", encoding="utf-8")

    result = _smart_prefetch_intent_files(
        request_text="saved home address",
        source_root=tmp_path,
        existing_anchored_paths={"a.kt"},
    )

    assert result == []


def test_smart_prefetch_respects_max_files(tmp_path: Path) -> None:
    for index in range(10):
        (tmp_path / f"{index}.kt").write_text("val homeAddress = profile.homeAddress", encoding="utf-8")

    result = _smart_prefetch_intent_files(
        request_text="saved home address",
        source_root=tmp_path,
        existing_anchored_paths=set(),
        max_files=3,
    )

    assert len(result) == 3


def test_smart_prefetch_skips_binary_extensions(tmp_path: Path) -> None:
    (tmp_path / "a.png").write_text("homeAddress", encoding="utf-8")

    result = _smart_prefetch_intent_files(
        request_text="saved home address",
        source_root=tmp_path,
        existing_anchored_paths=set(),
    )

    assert result == []


def test_smart_prefetch_returns_empty_when_no_idents(tmp_path: Path) -> None:
    (tmp_path / "a.kt").write_text("val homeAddress = profile.homeAddress", encoding="utf-8")

    result = _smart_prefetch_intent_files(
        request_text="Refactor something",
        source_root=tmp_path,
        existing_anchored_paths=set(),
    )

    assert result == []


def test_build_prefetches_intent_candidates_when_no_anchors(tmp_path: Path) -> None:
    (tmp_path / "Profile.kt").write_text(
        "val homeAddress = profile.homeAddress",
        encoding="utf-8",
    )

    result = build_evidence_bundle(
        request_text="Pre-fill the map with the user's saved home address",
        normalized_request=None,
        source_tree=tmp_path,
        grounding_terms=[],
        planner_must_touch=[],
        has_destructive_verb=False,
        settings=_settings(),
    )

    assert result.verdict == "skip"
    assert "Profile.kt" in result.candidate_files
