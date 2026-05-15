"""Pre-plan candidate file discovery (Phase B.2.a, 2026-05-11).

Diagnosis (P69-19 v6): the DeepSeek planner kept returning
``must_touch_files=[]`` because the prompt never told it which files
actually exist in the active KB. Claude Code CLI works around this by
exploring the filesystem via its Edit/Read tools; DeepSeek-API can
only see what the prompt explicitly shows.

This module does a cheap, deterministic repository scan before the
planner runs, ranks candidates by issue-keyword match, and returns a
short list (top N) for inclusion in the planner prompt. The planner is
then asked to pick ``must_touch_files`` from this menu rather than
invent paths from thin air.

Designed to work on both SQLite (FTS5) and Postgres (tsvector). For V1
we keep the scoring entirely in Python so the same logic works on
either backend without dialect dispatch — 150-document KBs query in
single-digit ms.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge_document import KnowledgeDocument

logger = logging.getLogger("app.services.preplan_discover")


# Words that don't help discriminate file relevance. Kept small and
# task-agnostic; per-language tuning happens via keyword extraction
# below (e.g. "Composable" stays in for Kotlin).
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "i", "in", "is", "it", "its", "of", "on", "that",
    "the", "to", "was", "were", "will", "with", "this", "these",
    "those", "or", "but", "not", "no", "can", "should", "must",
    "would", "could", "may", "user", "users", "use", "using", "used",
    "add", "added", "adding", "make", "makes", "making", "see", "also",
    "way", "ways", "via", "any", "all", "both", "each", "more", "some",
    "into", "during", "after", "before", "between", "above", "below",
    "p69", "jira", "issue", "ticket", "task",
})

# Extension priorities (lower = preferred). Android Kotlin apps put
# logic in .kt; layouts in .xml; gradle in .gradle. Web apps reverse
# this. Could be made source-aware later; for V1 the boost is small
# enough that wrong guesses don't dominate keyword score.
_EXTENSION_BOOST: dict[str, float] = {
    ".kt": 2.0,
    ".kts": 1.5,
    ".java": 1.5,
    ".swift": 1.5,
    ".tsx": 1.5,
    ".ts": 1.3,
    ".jsx": 1.3,
    ".js": 1.0,
    ".py": 1.5,
    ".xml": 0.8,
    ".gradle": 0.5,
    ".html": 0.3,
    ".css": 0.2,
    ".json": 0.4,
    ".md": 0.2,
}


@dataclass(frozen=True)
class CandidateFile:
    path: str
    score: float
    matched_terms: list[str]
    reason: str
    extension: str


def _extract_keywords(text: str, *, max_keywords: int = 20) -> list[str]:
    """Split text into lowercase keywords, drop stopwords + short
    tokens. Preserves CamelCase boundaries by splitting on transitions
    so "MapAddressPicker" → ["map", "address", "picker"]. Also keeps
    common code-shaped tokens whole when split would lose info
    (e.g. "addressMapPicker" → already covered by camel split).
    """
    if not text:
        return []
    # CamelCase + non-alphanumeric splitter.
    parts = re.findall(r"[A-Za-z][a-z]+|[A-Z]{2,}|[a-z]{3,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for raw in parts:
        token = raw.lower()
        if len(token) < 3:
            continue
        if token in _STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= max_keywords:
            break
    return out


def _score_path(
    path: str,
    keywords: list[str],
    *,
    term_weights: dict[str, float] | None = None,
) -> tuple[float, list[str]]:
    """Score path by how many keywords appear in it. Filename matches
    weigh more than directory matches because filenames are concise
    semantic anchors (e.g. ``CustomerSignup.kt`` vs ``app/src/main``).
    """
    if not keywords:
        return 0.0, []
    path_lower = path.lower()
    # Filename portion (last segment without extension).
    last = path_lower.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    filename = last.rsplit(".", 1)[0]
    matched: list[str] = []
    score = 0.0
    for kw in keywords:
        weight = (term_weights or {}).get(kw, 1.0)
        in_filename = kw in filename
        in_full = kw in path_lower
        if in_filename:
            score += 5.0 * weight
            matched.append(kw)
        elif in_full:
            score += 1.5 * weight
            matched.append(kw)
    return score, matched


def _score_content_lite(
    content: str,
    keywords: list[str],
    *,
    term_weights: dict[str, float] | None = None,
) -> tuple[float, list[str]]:
    """Lightweight content scoring: count keyword occurrences in a
    head-sample of the file. Capped to avoid biasing toward very long
    files. Not a substitute for FTS5/tsvector; intended as a cheap
    discriminator on top of path scoring.
    """
    if not keywords or not content:
        return 0.0, []
    sample = content[:20_000].lower()
    matched: list[str] = []
    score = 0.0
    for kw in keywords:
        weight = (term_weights or {}).get(kw, 1.0)
        # Count up to 3 hits per keyword (diminishing returns).
        count = min(3, sample.count(kw))
        if count > 0:
            score += count * 0.5 * weight
            matched.append(kw)
    return score, matched


def _keyword_idf_weights(
    docs: list[KnowledgeDocument],
    keywords: list[str],
) -> dict[str, float]:
    if not docs or not keywords:
        return {}
    doc_freq: dict[str, int] = {kw: 0 for kw in keywords}
    for doc in docs:
        haystack = f"{doc.relative_path}\n{(doc.content or '')[:20_000]}".lower()
        for kw in keywords:
            if kw in haystack:
                doc_freq[kw] += 1
    total = max(1, len(docs))
    weights: dict[str, float] = {}
    for kw in keywords:
        freq = doc_freq.get(kw, 0)
        if freq <= 0:
            weights[kw] = 1.0
            continue
        weights[kw] = max(0.05, min(4.0, math.log((total + 1) / (freq + 1))))
    return weights


def _keywords_from_acceptance_tests(
    acceptance_tests: list[dict] | None,
) -> list[str]:
    """Phase 1.1 (2026-05-11): extract semantic keywords from planner's
    acceptance_tests so pre-plan retrieval surfaces the integration
    files acceptance actually requires (Maps SDK / Gradle / Manifest /
    nav graph). Strips regex metacharacters and dotted FQNs so we get
    raw tokens like "maps", "google", "android", "onMapReady".
    """
    if not acceptance_tests:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for test in acceptance_tests:
        if not isinstance(test, dict):
            continue
        # acceptance_tests entries typically have `pattern`, `rationale`,
        # `kind` — pull text from all so we capture "Google Maps" in the
        # rationale even when pattern is regex-y.
        for key in ("pattern", "rationale", "kind"):
            v = str(test.get(key) or "")
            if not v:
                continue
            # Strip common regex chars + dotted FQN separators.
            cleaned = re.sub(r"[\\^$|()\[\]{}*+?]", " ", v)
            cleaned = cleaned.replace(".", " ")
            for tok in _extract_keywords(cleaned, max_keywords=15):
                if tok not in seen:
                    seen.add(tok)
                    out.append(tok)
                if len(out) >= 25:
                    return out
    return out


def preplan_discover_files(
    *,
    issue_text: str,
    source_name: str,
    db: Session,
    top_n: int = 10,
    acceptance_tests: list[dict] | None = None,
) -> list[CandidateFile]:
    """Rank candidate files for an issue. Returns top N with score
    breakdown and matched keywords. Returns empty list if no documents
    in source or no usable keywords.

    Phase 1.1 (2026-05-11): ``acceptance_tests`` keywords are merged
    into the query so retrieval surfaces files acceptance requires
    (e.g. Google Maps integration needs build.gradle / Manifest /
    map components, not just signup form files).
    """
    keywords = _extract_keywords(issue_text)
    acceptance_keywords = _keywords_from_acceptance_tests(acceptance_tests)
    if acceptance_keywords:
        existing = set(keywords)
        for kw in acceptance_keywords:
            if kw not in existing:
                keywords.append(kw)
                existing.add(kw)
    if not keywords:
        logger.warning(
            "preplan_discover: no usable keywords extracted from issue_text (len=%d)",
            len(issue_text),
        )
        return []
    docs = list(
        db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.source_name == source_name
            )
        ).scalars()
    )
    if not docs:
        logger.warning(
            "preplan_discover: source=%s has no KnowledgeDocuments — KB sync needed?",
            source_name,
        )
        return []

    term_weights = _keyword_idf_weights(docs, keywords)
    candidates: list[CandidateFile] = []
    for doc in docs:
        ext = (doc.extension or "").lower()
        ext_boost = _EXTENSION_BOOST.get(ext, 1.0)
        path_score, path_matches = _score_path(
            doc.relative_path,
            keywords,
            term_weights=term_weights,
        )
        if path_score == 0.0:
            # Fall back to content scoring only when path didn't match
            # at all — saves us from O(N * keywords * content) on every
            # doc.
            content_score, content_matches = _score_content_lite(
                doc.content or "",
                keywords,
                term_weights=term_weights,
            )
            if content_score == 0.0:
                continue
            matched = content_matches
            total = content_score * ext_boost
        else:
            content_score, content_matches = _score_content_lite(
                doc.content or "",
                keywords,
                term_weights=term_weights,
            )
            matched = list(dict.fromkeys(path_matches + content_matches))
            total = (path_score + content_score) * ext_boost

        # Build a short human-readable reason for the planner prompt.
        if path_matches:
            reason = f"filename matches {', '.join(path_matches[:4])}"
        elif content_matches:
            reason = f"content references {', '.join(content_matches[:4])}"
        else:
            reason = "keyword match"

        candidates.append(
            CandidateFile(
                path=doc.relative_path,
                score=round(total, 2),
                matched_terms=matched,
                reason=reason,
                extension=ext,
            )
        )

    candidates.sort(key=lambda c: c.score, reverse=True)
    top = candidates[:top_n]
    logger.warning(
        "preplan_discover: source=%s keywords=%s candidates=%d shown=%d top=%s",
        source_name,
        keywords[:10],
        len(candidates),
        len(top),
        [(c.path, c.score) for c in top[:5]],
    )
    return top


def format_candidates_for_prompt(
    candidates: list[CandidateFile],
    *,
    max_shown: int = 10,
) -> str:
    """Render candidate list as a stable prompt section. Sorted by
    score descending; cache-friendly format (no timestamps, no
    randomness).
    """
    if not candidates:
        return ""
    lines = ["Candidate files (repository search; pick must_touch from here):"]
    for idx, cand in enumerate(candidates[:max_shown], start=1):
        terms = ", ".join(cand.matched_terms[:5])
        lines.append(
            f"  {idx}. {cand.path}  [score={cand.score}, matches: {terms}]"
        )
        lines.append(f"     reason: {cand.reason}")
    lines.append(
        "If none are sufficient, return status=needs_more_context with "
        "a description of what to search next instead of inventing paths."
    )
    return "\n".join(lines)
