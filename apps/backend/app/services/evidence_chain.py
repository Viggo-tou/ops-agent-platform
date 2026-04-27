from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from app.agents.schemas import GeneratedPlan
from app.core.config import Settings
from app.schemas.evidence import EvidenceItem
from app.schemas.knowledge import KnowledgeClaim
from app.services.spec_conformance import _classify_files_in_diff
from app.services.task_workspace import TaskWorkspace


@dataclass(frozen=True)
class EvidenceChainFinding:
    rule: str
    severity: Literal["block", "warn"]
    file_path: str | None
    message: str
    details: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "file_path": self.file_path,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True)
class EvidenceChainReport:
    closed: bool
    findings: list[EvidenceChainFinding]
    summary: str
    diagnostic: dict[str, object]

    def to_payload(self) -> dict[str, object]:
        return {
            "closed": self.closed,
            "summary": self.summary,
            "findings": [finding.to_payload() for finding in self.findings],
            "diagnostic": self.diagnostic,
        }


_DEFAULT_STRONG_SOURCES = {
    "rag_lexical",
    "rag_fts5",
    "rag_card",
    "cc_glob",
    "cc_grep",
    "cc_read",
    "spec_anchor",
}


def check_evidence_chain(
    *,
    workspace: TaskWorkspace,
    diff: str,
    plan: GeneratedPlan,
    claims: list[KnowledgeClaim],
    citations: list[EvidenceItem],
    attestation: dict | None,
    settings: Settings,
) -> EvidenceChainReport:
    findings: list[EvidenceChainFinding] = []

    intent = _read_intent(workspace)
    scenario = str(getattr(plan, "scenario", "") or "")
    _check_intent(findings=findings, intent=intent, scenario=scenario)

    evidence_items = _load_evidence_items(workspace=workspace, citations=citations)
    strong_sources = _strong_sources(settings)
    strong_items = [item for item in evidence_items if item.source in strong_sources]
    user_provided_count = sum(1 for item in evidence_items if item.source == "user_provided")
    if not strong_items:
        if evidence_items and user_provided_count == len(evidence_items):
            message = (
                "Evidence chain broken: weak evidence "
                f"(only {user_provided_count} user-provided items, no retrieval/CC backing)."
            )
        elif evidence_items:
            sources = sorted({str(item.source) for item in evidence_items})
            message = (
                "Evidence chain broken: weak evidence "
                f"(sources {sources!r} do not include retrieval/CC/spec backing)."
            )
        else:
            message = (
                "Evidence chain broken: weak evidence "
                "(no EvidenceItems in evidence manifest)."
            )
        findings.append(
            EvidenceChainFinding(
                rule="evidence_weak",
                severity="block",
                file_path=None,
                message=message,
                details={
                    "evidence_count": len(evidence_items),
                    "user_provided_count": user_provided_count,
                    "strong_sources": sorted(strong_sources),
                },
            )
        )

    file_shapes = _classify_files_in_diff(diff)
    touched_files = set(file_shapes)
    evidence_paths = {_normalize_path(item.file_path) for item in evidence_items}
    evidence_paths.discard("")
    expected_new_files = {
        _normalize_path(path)
        for path in (getattr(plan, "expected_new_files", []) or [])
        if isinstance(path, str)
    }
    expected_new_files.discard("")
    attestation_files = _extract_attestation_modified_files(attestation)
    justification_files = _extract_per_file_justification_files(attestation)

    backed_files: set[str] = set()
    for file_path, shape in sorted(file_shapes.items()):
        normalized = _normalize_path(file_path)
        if not normalized:
            continue
        if _path_in(normalized, evidence_paths):
            backed_files.add(normalized)
            continue
        if _path_in(normalized, attestation_files):
            backed_files.add(normalized)
            continue
        if shape == "create" and _path_in(normalized, expected_new_files):
            backed_files.add(normalized)
            continue
        if _path_in(normalized, justification_files):
            backed_files.add(normalized)
            findings.append(
                EvidenceChainFinding(
                    rule="untracked_file",
                    severity="warn",
                    file_path=normalized,
                    message=(
                        "Evidence chain warning: file "
                        f"{normalized} relies on per_file_justification; "
                        "TODO(T-041-08): dereference reviewer evidence before "
                        "making this a hard pass."
                    ),
                    details={"todo": "T-041-08", "file_shape": shape},
                )
            )
            continue
        findings.append(
            EvidenceChainFinding(
                rule="untracked_file",
                severity="block",
                file_path=normalized,
                message=(
                    "Evidence chain broken: file "
                    f"{normalized} has no supporting EvidenceItem and is not a planned new file."
                ),
                details={
                    "file_shape": shape,
                    "evidence_paths": sorted(evidence_paths),
                    "expected_new_files": sorted(expected_new_files),
                    "attestation_files": sorted(attestation_files),
                },
            )
        )

    _check_claims(
        findings=findings,
        claims=claims,
        citation_count=len(evidence_items),
        settings=settings,
    )
    _check_attestation(
        findings=findings,
        attestation=attestation,
        touched_files=touched_files,
        settings=settings,
    )

    block_count = sum(1 for finding in findings if finding.severity == "block")
    warn_count = sum(1 for finding in findings if finding.severity == "warn")
    diagnostic = {
        "evidence_count": len(evidence_items),
        "strong_evidence_count": len(strong_items),
        "weak_evidence_count": len(evidence_items) - len(strong_items),
        "evidence_sources": sorted({str(item.source) for item in evidence_items}),
        "evidence_paths": sorted(evidence_paths),
        "modified_files": sorted(touched_files),
        "modified_files_with_evidence": sorted(backed_files),
        "untracked_files": sorted(
            {
                finding.file_path
                for finding in findings
                if finding.rule == "untracked_file"
                and finding.severity == "block"
                and finding.file_path
            }
        ),
        "expected_new_files": sorted(expected_new_files),
        "attestation_modified_files": sorted(attestation_files),
        "claims_total": len(claims),
        "claims_high_confidence": _count_confident_cited_claims(
            claims=claims,
            citation_count=len(evidence_items),
        ),
        "block_count": block_count,
        "warn_count": warn_count,
    }
    closed = block_count == 0
    return EvidenceChainReport(
        closed=closed,
        findings=findings,
        summary=_build_summary(findings=findings, diagnostic=diagnostic),
        diagnostic=diagnostic,
    )


def _read_intent(workspace: TaskWorkspace) -> dict[str, Any] | None:
    try:
        intent = workspace.read_intent()
    except Exception:  # noqa: BLE001
        return None
    return intent if isinstance(intent, dict) else None


def _check_intent(
    *,
    findings: list[EvidenceChainFinding],
    intent: dict[str, Any] | None,
    scenario: str,
) -> None:
    if not intent:
        findings.append(
            EvidenceChainFinding(
                rule="intent_missing",
                severity="block",
                file_path=None,
                message=(
                    "Evidence chain broken: intent missing — refusing to approve "
                    "a task without a recorded intent."
                ),
                details={"reason": "intent.md missing or unreadable"},
            )
        )
        return

    request_text = _clean_text(intent.get("request_text") or intent.get("intent_text"))
    jira_issue_body = _clean_text(intent.get("jira_issue_body"))
    missing: list[str] = []
    if not request_text:
        missing.append("request_text")
    if scenario == "jira_issue_develop" and not jira_issue_body:
        missing.append("jira_issue_body")
    if missing:
        findings.append(
            EvidenceChainFinding(
                rule="intent_missing",
                severity="block",
                file_path=None,
                message=(
                    "Evidence chain broken: intent missing — refusing to approve "
                    "a task without a recorded intent."
                ),
                details={"missing_fields": missing, "scenario": scenario},
            )
        )


def _load_evidence_items(
    *,
    workspace: TaskWorkspace,
    citations: list[EvidenceItem],
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    try:
        items.extend(workspace.list_evidence())
    except Exception:  # noqa: BLE001
        pass
    items.extend(citations or [])

    deduped: list[EvidenceItem] = []
    seen: set[tuple[str, str, str]] = set()
    for item in items:
        key = (str(item.id), str(item.source), _normalize_path(item.file_path))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _strong_sources(settings: Settings) -> set[str]:
    raw = str(getattr(settings, "evidence_chain_strong_sources", "") or "")
    values = {part.strip() for part in raw.split(",") if part.strip()}
    return values or set(_DEFAULT_STRONG_SOURCES)


def _check_claims(
    *,
    findings: list[EvidenceChainFinding],
    claims: list[KnowledgeClaim],
    citation_count: int,
    settings: Settings,
) -> None:
    if not claims:
        return

    min_confident = max(
        0,
        int(getattr(settings, "evidence_chain_min_confident_claims", 3) or 0),
    )
    ungrounded: list[int] = []
    low_or_ungrounded: list[int] = []
    confident = 0
    for index, claim in enumerate(claims):
        citation_indices = list(getattr(claim, "citation_indices", []) or [])
        citations_valid = bool(citation_indices) and all(
            isinstance(citation_index, int)
            and 0 <= citation_index < citation_count
            for citation_index in citation_indices
        )
        confidence = str(getattr(claim, "confidence", "") or "").casefold()
        if not citations_valid:
            ungrounded.append(index)
        if not citations_valid or confidence == "low":
            low_or_ungrounded.append(index)
        if citations_valid and confidence != "low":
            confident += 1

    if ungrounded and confident >= min_confident:
        findings.append(
            EvidenceChainFinding(
                rule="ungrounded_claims",
                severity="warn",
                file_path=None,
                message=(
                    "Evidence chain warning: "
                    f"{len(ungrounded)} claim(s) have missing or invalid citations."
                ),
                details={
                    "ungrounded_claim_indices": ungrounded,
                    "claims_confident": confident,
                    "min_confident": min_confident,
                },
            )
        )

    if len(low_or_ungrounded) == len(claims):
        findings.append(
            EvidenceChainFinding(
                rule="ungrounded_claims",
                severity="block",
                file_path=None,
                message=(
                    "Evidence chain broken: all claims are low-confidence or "
                    "lack valid evidence citations."
                ),
                details={
                    "claim_count": len(claims),
                    "ungrounded_claim_indices": ungrounded,
                },
            )
        )
        return

    if confident < min_confident:
        findings.append(
            EvidenceChainFinding(
                rule="ungrounded_claims",
                severity="block",
                file_path=None,
                message=(
                    "Evidence chain broken: only "
                    f"{confident} confident cited claim(s); minimum is {min_confident}."
                ),
                details={
                    "claims_confident": confident,
                    "min_confident": min_confident,
                    "claim_count": len(claims),
                },
            )
        )


def _check_attestation(
    *,
    findings: list[EvidenceChainFinding],
    attestation: dict | None,
    touched_files: set[str],
    settings: Settings,
) -> None:
    if not isinstance(attestation, dict) or not attestation:
        findings.append(
            EvidenceChainFinding(
                rule="attestation_missing",
                severity="warn",
                file_path=None,
                message=(
                    "Evidence chain warning: goal_attestation is missing; "
                    "older tasks may not have per-goal proof."
                ),
                details={},
            )
        )
        return

    attested_files = _extract_attestation_modified_files(attestation)
    diff_paths = {_normalize_path(path) for path in touched_files}
    unexpected = sorted(
        path for path in attested_files
        if not _path_in(path, diff_paths)
    )
    if unexpected:
        block = bool(
            getattr(settings, "evidence_chain_block_on_attestation_mismatch", True)
        )
        findings.append(
            EvidenceChainFinding(
                rule="attestation_mismatch",
                severity="block" if block else "warn",
                file_path=unexpected[0],
                message=(
                    "Evidence chain broken: goal_attestation references files "
                    "that are not present in the actual diff: "
                    + ", ".join(unexpected[:5])
                ),
                details={
                    "attestation_files": sorted(attested_files),
                    "diff_files": sorted(touched_files),
                    "unexpected_files": unexpected,
                },
            )
        )


def _count_confident_cited_claims(
    *,
    claims: list[KnowledgeClaim],
    citation_count: int,
) -> int:
    count = 0
    for claim in claims:
        indices = list(getattr(claim, "citation_indices", []) or [])
        if not indices:
            continue
        if not all(isinstance(index, int) and 0 <= index < citation_count for index in indices):
            continue
        if str(getattr(claim, "confidence", "") or "").casefold() != "low":
            count += 1
    return count


def _extract_attestation_modified_files(attestation: dict | None) -> set[str]:
    if not isinstance(attestation, dict):
        return set()

    paths: set[str] = set()
    for key in (
        "modified_files",
        "files_modified",
        "must_touch",
        "must_touch_files",
        "proven_must_touch",
        "proven_must_touch_files",
    ):
        paths.update(_paths_from_value(attestation.get(key)))

    for collection_key in ("goals", "anchors"):
        entries = attestation.get(collection_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key in (
                "modified_files",
                "files_modified",
                "must_touch",
                "must_touch_files",
                "proven_must_touch",
                "proven_must_touch_files",
            ):
                paths.update(_paths_from_value(entry.get(key)))
    paths.discard("")
    return paths


def _extract_per_file_justification_files(attestation: dict | None) -> set[str]:
    if not isinstance(attestation, dict):
        return set()
    files: set[str] = set()
    for key in ("per_file_justification", "per_file_justifications", "file_justifications"):
        raw = attestation.get(key)
        if isinstance(raw, dict):
            for path, value in raw.items():
                if value:
                    files.add(_normalize_path(str(path)))
        elif isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                if not entry.get("justified", True):
                    continue
                path = entry.get("file_path") or entry.get("file")
                if isinstance(path, str) and path.strip():
                    files.add(_normalize_path(path))
    files.discard("")
    return files


def _paths_from_value(value: object) -> set[str]:
    paths: set[str] = set()
    if isinstance(value, str):
        paths.add(_normalize_path(value))
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, dict)):
        for item in value:
            if isinstance(item, str):
                paths.add(_normalize_path(item))
    paths.discard("")
    return paths


def _normalize_path(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text.startswith("a/") or text.startswith("b/"):
        text = text[2:]
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _paths_match(left: str, right: str) -> bool:
    """Path-segment suffix-tolerant equality for normalized repo paths.

    Returns True iff two paths refer to the same file even when one has a
    leading source-name prefix (e.g. ``handyman-admin-dashboard/src/foo.js``)
    and the other is rooted at the source root (``src/foo.js``).

    Codegen.repair sometimes re-emits patches with the source-name prefix
    inlined while EvidenceItem.file_path stays repo-relative. Without this
    tolerance every repaired file gets flagged as untracked even though
    the corresponding evidence exists. Match must be on segment boundaries
    so "src/foo.js" doesn't accidentally match "rc/foo.js".
    """
    if not left or not right:
        return False
    if left == right:
        return True
    if right.endswith("/" + left):
        return True
    if left.endswith("/" + right):
        return True
    return False


def _path_in(needle: str, haystack: Iterable[str]) -> bool:
    """Suffix-tolerant ``needle in haystack`` for normalized repo paths."""
    if not needle:
        return False
    for candidate in haystack:
        if _paths_match(needle, candidate):
            return True
    return False


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _build_summary(
    *,
    findings: list[EvidenceChainFinding],
    diagnostic: dict[str, object],
) -> str:
    block_findings = [finding for finding in findings if finding.severity == "block"]
    if block_findings:
        untracked = [
            finding for finding in block_findings if finding.rule == "untracked_file"
        ]
        if len(untracked) > 1:
            return (
                "Evidence chain broken: "
                f"{len(untracked)} modified files have no evidence backing."
            )
        return block_findings[0].message
    warn_count = int(diagnostic.get("warn_count") or 0)
    evidence_count = int(diagnostic.get("evidence_count") or 0)
    modified_count = len(diagnostic.get("modified_files") or [])
    if warn_count:
        return (
            "Evidence chain closed with "
            f"{warn_count} warning(s), {evidence_count} evidence item(s), "
            f"and {modified_count} modified file(s)."
        )
    return (
        "Evidence chain closed: "
        f"{evidence_count} evidence item(s), {modified_count} modified file(s)."
    )
