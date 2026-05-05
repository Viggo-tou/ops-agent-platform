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

import logging
import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings

logger = logging.getLogger(__name__)


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
    # Plan-A telemetry: which retrieval strategy produced each anchor's hits.
    # Empty dict when FTS5 was unavailable and we fell back to substring scan.
    anchor_strategy: dict[str, str] = None  # type: ignore[assignment]

    def to_payload(self) -> dict:
        strategy = self.anchor_strategy or {}
        return {
            "verdict": self.verdict,
            "anchors_searched": self.anchors_searched,
            "anchor_hit_count": {a: sum(h.values()) for a, h in self.anchor_hits.items()},
            "anchor_strategy": dict(strategy),
            "must_touch_files": self.must_touch_files,
            "forbidden_files": self.forbidden_files,
            "candidate_files": self.candidate_files[:20],
            "coverage_score": round(self.coverage_score, 2),
            "reason": self.reason,
        }


# --- Plan A: FTS5-backed anchor matching ---------------------------------
#
# Previously evidence_bundle relied on _find_files_containing_anchor (raw
# substring contains) for anchor matching. That meant "home address" had to
# appear as 11 contiguous characters in source — which it almost never does.
# Empirical: 75% of dogfood tasks had anchor_hit_count={} on real Android repos.
#
# Plan A re-routes anchor matching through the existing knowledge_document_fts
# index (porter unicode61 tokenizer, AND/OR boolean operators):
#   1. Tokenize anchor (CamelCase split + drop stopwords)
#   2. FTS5 AND query (precision)
#   3. Fallback FTS5 OR query (recall) if AND returned 0
#   4. Last-resort substring scan if FTS5 unavailable / source_name missing
#
# Each anchor's strategy ("fts5_and"/"fts5_or"/"substring") is recorded in
# anchor_strategy so we can measure the actual retrieval distribution.

# Maximum FTS5 candidate files returned per anchor. Tuned to balance
# downstream codegen context size against under-recall.
_FTS5_PER_ANCHOR_LIMIT = 20

# Stopwords stripped from anchor phrases before forming FTS5 token query.
# Mirrors spec_conformance._ANCHOR_STOPWORDS (single source of truth there
# was specific to the rejection gate; we duplicate here intentionally so
# evidence_bundle stays self-contained and import-light).
_FTS5_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "and", "or", "for", "to", "is", "as",
    "at", "by", "be", "with", "from", "that", "this", "these", "those", "it",
    "its", "but", "not", "no", "so",
    "component", "page", "module", "file", "function", "method", "class",
    "screen", "view", "ui", "flow",
    "shared", "common", "main", "base", "global", "generic", "default",
    "parent", "root", "wrapper", "container",
})


def _tokenize_for_fts5(anchor: str) -> list[str]:
    """CamelCase + word-split, lowercased, stopwords removed, len>=2."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", anchor)
    spaced = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", spaced)
    words = re.findall(r"[A-Za-z][A-Za-z0-9]*", spaced.lower())
    return [w for w in words if len(w) >= 2 and w not in _FTS5_STOPWORDS]


def _fts5_match_query(tokens: list[str], *, operator: str) -> str:
    """Build a safe FTS5 MATCH query from tokenized anchor terms.

    operator is "AND" (precision) or "OR" (recall). Tokens are bare words —
    safe under porter unicode61 tokenizer and pre-validated by the regex in
    _tokenize_for_fts5 (alphanum only, len>=2).

    Source code commonly contains the CamelCase compound form (e.g.
    ``homeAddress`` -> single token ``homeaddress`` after porter unicode61).
    The split anchor "home AND address" misses that compound. To match both
    forms, the AND query also OR-includes the concatenated joined form;
    the OR query OR-includes it too. For tokens=["home","address"]:
        AND -> "(home AND address) OR homeaddress"
        OR  -> "home OR address OR homeaddress"
    """
    if not tokens:
        return ""
    if operator == "AND":
        if len(tokens) == 1:
            return tokens[0]
        joined = "".join(tokens)
        and_clause = " AND ".join(tokens)
        return f"({and_clause}) OR {joined}"
    # OR
    joined = "".join(tokens)
    parts = list(tokens)
    if joined not in parts:
        parts.append(joined)
    return " OR ".join(parts)


def _find_files_via_fts5(
    db: Session,
    source_name: str,
    anchor: str,
    *,
    limit: int = _FTS5_PER_ANCHOR_LIMIT,
) -> tuple[dict[str, int], str]:
    """Return ({relative_path: rough_hit_count}, strategy_name).

    strategy_name is one of {"fts5_and", "fts5_or", ""} where empty means
    no FTS5 hits at all (caller should fall back).
    """
    tokens = _tokenize_for_fts5(anchor)
    if not tokens:
        return {}, ""

    # Query FTS5 directly using the UNINDEXED `source_name` and
    # `relative_path` columns — avoid joining knowledge_document on rowid
    # because rowid is auto-int and document.id is a UUID string (mismatched
    # types produce zero rows). FTS5 returns one row per matching document;
    # we synthesize a count of 1 because downstream only needs truthiness.
    for operator, strategy in (("AND", "fts5_and"), ("OR", "fts5_or")):
        query = _fts5_match_query(tokens, operator=operator)
        if not query:
            continue
        try:
            rows = db.execute(
                text(
                    "SELECT relative_path "
                    "FROM knowledge_document_fts "
                    "WHERE knowledge_document_fts MATCH :q "
                    "  AND source_name = :sn "
                    "ORDER BY rank LIMIT :lim"
                ),
                {"q": query, "sn": source_name, "lim": int(limit)},
            ).all()
        except Exception as exc:  # noqa: BLE001
            # FTS5 syntax error or DB hiccup — log + fall through to next strategy.
            logger.warning(
                "evidence_bundle FTS5 query failed",
                extra={"anchor": anchor, "operator": operator, "error": str(exc)[:200]},
            )
            continue
        if rows:
            counts = {str(r[0]).replace("\\", "/"): 1 for r in rows if r[0]}
            if counts:
                return counts, strategy
    return {}, ""


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
    db: Session | None = None,
    source_name: str | None = None,
) -> EvidenceBundle:
    """Build a pre-codegen evidence bundle.

    This runs BEFORE codegen to prove the task targets real code.
    If evidence is insufficient, codegen should not proceed.

    Plan A (2026-05-05): when ``db`` and ``source_name`` are supplied,
    anchor matching is routed through the knowledge_document_fts FTS5
    index (tokenized AND -> tokenized OR -> substring fallback). The
    legacy substring-only path remains the fallback so callers without
    a DB session still work.
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
            anchor_strategy={},
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
            anchor_strategy={},
        )

    anchor_hits: dict[str, dict[str, int]] = {}
    anchor_strategy: dict[str, str] = {}
    all_candidate_files: set[str] = set()

    use_fts5 = db is not None and bool(source_name)

    for anchor in anchors:
        hits: dict[str, int] = {}
        strategy = ""
        if use_fts5:
            hits, strategy = _find_files_via_fts5(db, source_name, anchor)
        if not hits:
            hits = _find_files_containing_anchor(source_tree, anchor)
            if hits:
                strategy = "substring"
        if hits:
            anchor_hits[anchor] = hits
            anchor_strategy[anchor] = strategy
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
            anchor_strategy=anchor_strategy,
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
            anchor_strategy=anchor_strategy,
        )

    # B2 fail-closed: when ZERO anchors hit AND the planner did not pre-commit
    # any must-touch files, the bundle has no real grounding in source. Pre-
    # Plan-A this path silently returned "sufficient" with empty anchor_hits,
    # which let codegen wander to whatever filenames it could guess from the
    # request text — exactly how P69-17 v9 ended up editing AndroidManifest.xml
    # for a feature that lives in a Kotlin Fragment.
    if coverage == 0.0 and not (planner_must_touch or []):
        return EvidenceBundle(
            verdict="insufficient",
            anchors_searched=anchors,
            anchor_hits=anchor_hits,
            must_touch_files=must_touch,
            forbidden_files=forbidden,
            candidate_files=sorted(all_candidate_files)[:20],
            coverage_score=coverage,
            reason=(
                f"Zero anchor hits across {len(anchors)} term(s) and planner "
                "did not pre-commit any must-touch files. Bundle cannot ground "
                "the task in source — refusing to emit 'sufficient'. Refine "
                "the request with more specific identifiers (CamelCase symbol "
                "names, file paths) or have the planner produce "
                "affected_code_locations."
            ),
            anchor_strategy=anchor_strategy,
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
        anchor_strategy=anchor_strategy,
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
