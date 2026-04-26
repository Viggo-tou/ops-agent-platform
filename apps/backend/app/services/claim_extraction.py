from __future__ import annotations

import re
from dataclasses import dataclass

from app.schemas.knowledge import KnowledgeClaim


_ANSWER_OPEN_RE = re.compile(r"<\s*answer\b[^>]*>", re.IGNORECASE)
_ANSWER_CLOSE_RE = re.compile(r"</\s*answer\s*>", re.IGNORECASE)
_CLAIMS_OPEN_RE = re.compile(r"<\s*claims\b[^>]*>", re.IGNORECASE)
_CLAIMS_CLOSE_RE = re.compile(r"</\s*claims\s*>", re.IGNORECASE)
_CLAIM_TAG_RE = re.compile(
    r"<\s*claim\b(?P<attrs>[^>]*)>(?P<text>.*?)</\s*claim\s*>",
    re.IGNORECASE | re.DOTALL,
)
_CLAIM_TAG_STRIP_RE = re.compile(r"</?\s*claim\b[^>]*>", re.IGNORECASE)
_CLAIM_ID_RE = re.compile(
    r"\bid\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_CLAIM_ENTRY_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<number>\d+)\s*[\).\]:-]\s*(?P<body>.*?)(?=^\s*(?:[-*]\s*)?\d+\s*[\).\]:-]\s*|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_CITE_RE = re.compile(r"\bcite\s*=\s*\[(?P<items>[^\]]*)\]", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"\bconfidence\s*=\s*(?P<value>[a-zA-Z_]+)", re.IGNORECASE)
_CHUNK_KIND_RE = re.compile(
    r"\b(?:chunk_kind_hint|chunk_kind|kind)\s*=\s*(?P<value>[a-zA-Z0-9_.:-]+)",
    re.IGNORECASE,
)
_VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass(frozen=True)
class _AnswerClaim:
    claim_id: int | None
    text: str


@dataclass(frozen=True)
class _ClaimEntry:
    claim_id: int | None
    text: str
    citation_indices: list[int]
    confidence: str
    chunk_kind_hint: str | None


def extract_claims(
    *,
    raw_synthesis: str,
    citation_count: int,
) -> tuple[str, list[KnowledgeClaim], int]:
    """Parse a structured synthesis response into answer prose and claims.

    The parser is intentionally tolerant: malformed structured output should
    degrade to the raw answer instead of breaking the knowledge search path.
    """
    try:
        return _extract_claims(raw_synthesis=raw_synthesis, citation_count=citation_count)
    except Exception:  # noqa: BLE001
        return raw_synthesis, [], 0


def _extract_claims(
    *,
    raw_synthesis: str,
    citation_count: int,
) -> tuple[str, list[KnowledgeClaim], int]:
    answer_block = _extract_answer_block(raw_synthesis)
    claims_block = _extract_claims_block(raw_synthesis)
    if answer_block is None or claims_block is None:
        return raw_synthesis, [], 0

    answer_claims = _parse_answer_claims(answer_block)
    claim_entries = _parse_claim_entries(claims_block, citation_count=max(citation_count, 0))
    clean_answer = _strip_claim_tags(answer_block)
    claims = _bind_claims(answer_claims=answer_claims, claim_entries=claim_entries)
    ungrounded_count = sum(1 for claim in claims if not claim.citation_indices)
    return clean_answer, claims, ungrounded_count


def _extract_answer_block(raw_synthesis: str) -> str | None:
    open_match = _ANSWER_OPEN_RE.search(raw_synthesis)
    if not open_match:
        return None

    close_match = _ANSWER_CLOSE_RE.search(raw_synthesis, open_match.end())
    if close_match:
        return raw_synthesis[open_match.end() : close_match.start()]

    claims_match = _CLAIMS_OPEN_RE.search(raw_synthesis, open_match.end())
    if claims_match:
        return raw_synthesis[open_match.end() : claims_match.start()]
    return None


def _extract_claims_block(raw_synthesis: str) -> str | None:
    open_match = _CLAIMS_OPEN_RE.search(raw_synthesis)
    if not open_match:
        return None

    close_match = _CLAIMS_CLOSE_RE.search(raw_synthesis, open_match.end())
    if close_match:
        return raw_synthesis[open_match.end() : close_match.start()]
    return raw_synthesis[open_match.end() :]


def _parse_answer_claims(answer_block: str) -> list[_AnswerClaim]:
    answer_claims: list[_AnswerClaim] = []
    for match in _CLAIM_TAG_RE.finditer(answer_block):
        text = _normalize_claim_text(match.group("text"))
        if not text:
            continue
        answer_claims.append(
            _AnswerClaim(
                claim_id=_parse_claim_id(match.group("attrs")),
                text=text,
            )
        )
    return answer_claims


def _parse_claim_entries(claims_block: str, *, citation_count: int) -> list[_ClaimEntry]:
    entries: list[_ClaimEntry] = []
    for match in _CLAIM_ENTRY_RE.finditer(claims_block.strip()):
        body = match.group("body").strip()
        if not body:
            continue

        raw_number = _parse_int(match.group("number"))
        claim_id = raw_number if raw_number and raw_number > 0 else None
        entries.append(
            _ClaimEntry(
                claim_id=claim_id,
                text=_extract_entry_text(body),
                citation_indices=_extract_citation_indices(body, citation_count=citation_count),
                confidence=_extract_confidence(body),
                chunk_kind_hint=_extract_chunk_kind_hint(body),
            )
        )
    return entries


def _bind_claims(
    *,
    answer_claims: list[_AnswerClaim],
    claim_entries: list[_ClaimEntry],
) -> list[KnowledgeClaim]:
    entries_by_id = {
        entry.claim_id: entry
        for entry in claim_entries
        if entry.claim_id is not None
    }
    used_entry_ids: set[int] = set()
    claims: list[KnowledgeClaim] = []

    for answer_claim in answer_claims:
        entry = entries_by_id.get(answer_claim.claim_id) if answer_claim.claim_id else None
        if entry and entry.claim_id is not None:
            used_entry_ids.add(entry.claim_id)
        claims.append(
            KnowledgeClaim(
                text=answer_claim.text or (entry.text if entry else ""),
                citation_indices=list(entry.citation_indices) if entry else [],
                confidence=entry.confidence if entry else "low",
                chunk_kind_hint=entry.chunk_kind_hint if entry else None,
            )
        )

    for entry in claim_entries:
        if entry.claim_id is not None and entry.claim_id in used_entry_ids:
            continue
        if not entry.text:
            continue
        claims.append(
            KnowledgeClaim(
                text=entry.text,
                citation_indices=list(entry.citation_indices),
                confidence=entry.confidence,
                chunk_kind_hint=entry.chunk_kind_hint,
            )
        )

    return claims


def _strip_claim_tags(answer_block: str) -> str:
    text = _CLAIM_TAG_RE.sub(lambda match: match.group("text"), answer_block)
    text = _CLAIM_TAG_STRIP_RE.sub("", text)
    return _normalize_answer_text(text)


def _parse_claim_id(attrs: str) -> int | None:
    match = _CLAIM_ID_RE.search(attrs or "")
    if not match:
        return None
    raw_value = match.group("double") or match.group("single") or match.group("bare")
    value = _parse_int(raw_value)
    if value is None or value <= 0:
        return None
    return value


def _extract_citation_indices(body: str, *, citation_count: int) -> list[int]:
    match = _CITE_RE.search(body)
    if not match:
        return []

    indices: list[int] = []
    seen: set[int] = set()
    for raw_value in re.findall(r"-?\d+", match.group("items")):
        value = _parse_int(raw_value)
        if value is None or value <= 0 or value > citation_count:
            continue
        zero_indexed = value - 1
        if zero_indexed in seen:
            continue
        seen.add(zero_indexed)
        indices.append(zero_indexed)
    return indices


def _extract_confidence(body: str) -> str:
    match = _CONFIDENCE_RE.search(body)
    if not match:
        return "low"
    value = match.group("value").strip().lower().split()[0]
    if value not in _VALID_CONFIDENCE:
        return "low"
    return value


def _extract_chunk_kind_hint(body: str) -> str | None:
    match = _CHUNK_KIND_RE.search(body)
    if not match:
        return None
    value = match.group("value").strip()
    return value or None


def _extract_entry_text(body: str) -> str:
    text = _CITE_RE.sub("", body)
    text = _CONFIDENCE_RE.sub("", text)
    text = _CHUNK_KIND_RE.sub("", text)
    text = text.strip()
    text = re.sub(r"^[\s:;\-]+", "", text)
    text = re.sub(r"^(?:\u2014|\u2013)+", "", text).strip()
    return _normalize_claim_text(text)


def _normalize_claim_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _normalize_answer_text(text: str) -> str:
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _parse_int(raw_value: str | None) -> int | None:
    try:
        return int(str(raw_value).strip())
    except (TypeError, ValueError):
        return None
