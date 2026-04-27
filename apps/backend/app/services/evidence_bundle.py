"""Evidence bundle: pre-codegen proof that the task targets real code.

Before codegen is allowed to run, this service builds an evidence bundle
that proves:

1. Anchors from the request actually exist in the source tree
2. Which files contain those anchors (hit map)
3. Which files MUST be touched (derived from hits + planner commitment)
4. Which files are FORBIDDEN (protected paths)

If the evidence is insufficient (no anchor hits, targets don't exist),
the bundle verdict is "insufficient" and codegen should not proceed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

from app.core.config import get_settings


@dataclass(frozen=True)
class EvidenceBundle:
    verdict: str  # "sufficient" | "insufficient" | "skip"
    anchors_searched: list[str]
    anchor_hits: dict[str, dict[str, int]]  # anchor -> {file: count}
    must_touch_files: list[str]
    forbidden_files: list[str]
    candidate_files: list[str]  # files that contain any anchor
    coverage_score: float  # 0.0-1.0: fraction of anchors with hits
    reason: str  # human-readable explanation

    def to_payload(self) -> dict:
        return {
            "verdict": self.verdict,
            "anchors_searched": self.anchors_searched,
            "anchor_hit_count": {a: sum(h.values()) for a, h in self.anchor_hits.items()},
            "must_touch_files": self.must_touch_files,
            "forbidden_files": self.forbidden_files,
            "candidate_files": self.candidate_files[:20],
            "coverage_score": round(self.coverage_score, 2),
            "reason": self.reason,
        }


_PROTECTED_PATTERNS = (
    re.compile(r"\.env"),
    re.compile(r"secrets?/"),
    re.compile(r"\.pem$"),
    re.compile(r"\.key$"),
    re.compile(r"migrations?/"),
    re.compile(r"node_modules/"),
    re.compile(r"\.git/"),
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
)


def build_evidence_bundle(
    *,
    request_text: str | None,
    normalized_request: str | None,
    source_tree: Path | None,
    grounding_terms: list[str] | None = None,
    planner_must_touch: list[str] | None = None,
    has_destructive_verb: bool = False,
    settings: object | None = None,
) -> EvidenceBundle:
    """Build a pre-codegen evidence bundle.

    This runs BEFORE codegen to prove the task targets real code.
    If evidence is insufficient, codegen should not proceed.
    """
    settings = settings or get_settings()

    if source_tree is None or not source_tree.exists():
        return EvidenceBundle(
            verdict="skip",
            anchors_searched=[],
            anchor_hits={},
            must_touch_files=_filter_must_touch_files(
                planner_must_touch or [],
                settings=settings,
            ),
            forbidden_files=[],
            candidate_files=[],
            coverage_score=0.0,
            reason="No source tree configured; skipping evidence check.",
        )

    from app.services.spec_conformance import (
        _extract_quoted_anchors,
        _find_files_containing_anchor,
    )

    combined = " ".join(
        part.strip() for part in (request_text, normalized_request) if part
    )

    anchors = list(grounding_terms or [])
    extracted = _extract_quoted_anchors(combined)
    for a in extracted:
        if a.lower() not in {x.lower() for x in anchors}:
            anchors.append(a)

    if not anchors:
        return EvidenceBundle(
            verdict="skip",
            anchors_searched=[],
            anchor_hits={},
            must_touch_files=_filter_must_touch_files(
                planner_must_touch or [],
                settings=settings,
            ),
            forbidden_files=[],
            candidate_files=[],
            coverage_score=0.0,
            reason="No anchors found in request; skipping evidence check.",
        )

    anchor_hits: dict[str, dict[str, int]] = {}
    all_candidate_files: set[str] = set()

    for anchor in anchors:
        hits = _find_files_containing_anchor(source_tree, anchor)
        if hits:
            anchor_hits[anchor] = hits
            all_candidate_files.update(hits.keys())

    anchors_with_hits = len(anchor_hits)
    coverage = anchors_with_hits / len(anchors) if anchors else 0.0

    must_touch = _derive_must_touch(
        anchor_hits=anchor_hits,
        planner_must_touch=planner_must_touch or [],
        candidate_files=all_candidate_files,
        settings=settings,
    )

    forbidden = _derive_forbidden(candidate_files=all_candidate_files)

    if has_destructive_verb and coverage == 0.0:
        return EvidenceBundle(
            verdict="insufficient",
            anchors_searched=anchors,
            anchor_hits=anchor_hits,
            must_touch_files=must_touch,
            forbidden_files=forbidden,
            candidate_files=sorted(all_candidate_files)[:20],
            coverage_score=coverage,
            reason=(
                f"Destructive task references {anchors!r} but NONE found in "
                f"source tree ({source_tree.name}). Cannot proceed to codegen."
            ),
        )

    if has_destructive_verb and not must_touch:
        return EvidenceBundle(
            verdict="insufficient",
            anchors_searched=anchors,
            anchor_hits=anchor_hits,
            must_touch_files=[],
            forbidden_files=forbidden,
            candidate_files=sorted(all_candidate_files)[:20],
            coverage_score=coverage,
            reason="Destructive task but no must-touch files could be derived.",
        )

    return EvidenceBundle(
        verdict="sufficient",
        anchors_searched=anchors,
        anchor_hits=anchor_hits,
        must_touch_files=must_touch,
        forbidden_files=forbidden,
        candidate_files=sorted(all_candidate_files)[:20],
        coverage_score=coverage,
        reason=f"{anchors_with_hits}/{len(anchors)} anchors found, {len(must_touch)} must-touch files.",
    )


def _derive_must_touch(
    *,
    anchor_hits: dict[str, dict[str, int]],
    planner_must_touch: list[str],
    candidate_files: set[str],
    settings: object | None = None,
) -> list[str]:
    """Merge planner commitments with evidence-derived targets."""
    must: set[str] = set()

    for f in planner_must_touch:
        norm = f.strip().replace("\\", "/")
        if norm:
            must.add(norm)

    for anchor, hits in anchor_hits.items():
        for filepath in hits:
            norm = filepath.strip().replace("\\", "/")
            if not _is_protected(norm):
                must.add(norm)

    return _filter_must_touch_files(sorted(must), settings=settings)[:20]


def _derive_forbidden(*, candidate_files: set[str]) -> list[str]:
    """Identify files that should NOT be modified."""
    forbidden: list[str] = []
    for f in candidate_files:
        if _is_protected(f):
            forbidden.append(f)
    return sorted(forbidden)


def _is_protected(filepath: str) -> bool:
    for pattern in _PROTECTED_PATTERNS:
        if pattern.search(filepath):
            return True
    return False


def _filter_must_touch_files(
    filepaths: Iterable[str],
    *,
    settings: object | None = None,
) -> list[str]:
    """Return only paths suitable as edit targets for must_touch_files."""
    settings = settings or get_settings()
    include_configs = bool(
        getattr(settings, "evidence_must_touch_include_configs", False)
    )
    filtered: list[str] = []
    seen: set[str] = set()

    for filepath in filepaths:
        norm = str(filepath or "").strip().replace("\\", "/")
        if not norm or norm in seen:
            continue
        seen.add(norm)
        if _is_must_touch_excluded_path(
            norm,
            settings=settings,
            include_configs=include_configs,
        ):
            continue
        filtered.append(norm)

    return filtered


def _is_must_touch_excluded_path(
    filepath: str,
    *,
    settings: object | None = None,
    include_configs: bool | None = None,
) -> bool:
    settings = settings or get_settings()
    norm = filepath.strip().replace("\\", "/")
    lower = norm.lower()
    name = lower.rsplit("/", 1)[-1]

    for pattern in _split_setting(
        getattr(settings, "evidence_must_touch_excluded_extensions", "")
    ):
        if name.endswith(pattern):
            return True

    padded = f"/{lower.strip('/')}/"
    for segment in _split_setting(
        getattr(settings, "evidence_must_touch_excluded_path_segments", "")
    ):
        segment = segment.strip("/")
        if segment and f"/{segment}/" in padded:
            return True

    allow_configs = (
        bool(getattr(settings, "evidence_must_touch_include_configs", False))
        if include_configs is None
        else include_configs
    )
    if not allow_configs:
        for pattern in _split_setting(
            getattr(settings, "evidence_must_touch_excluded_filenames", "")
        ):
            if fnmatch(name, pattern.lower()):
                return True

    return False


def _split_setting(raw_value: object) -> list[str]:
    values: list[str] = []
    for raw_item in str(raw_value or "").replace(";", ",").split(","):
        item = raw_item.strip().lower()
        if item:
            values.append(item)
    return values
