"""Bounded, relevance-ranked evidence pack for codegen.

Replaces "dump every must_touch file at any size" (the cause of our
DeepSeek-V4-Pro 0/4 SWE-bench result, where 90-140k byte injections
sat well past DeepSeek's reliable codegen window) with a controlled
pack: priority-sorted, byte-capped, with explicit per-file truncation.

Inputs are ``FileEvidence`` records that the caller has already
ranked. This module does not do retrieval or ranking — that's the
upstream evidence_bundle / KB layer's job. It just enforces the
budget so codegen never sees more than it can reliably handle.

Drop strategy when the budget is tight:

  1. Files arrive sorted by priority (lower number = higher priority).
  2. Walk the list, keep adding while:
     - file count < max_files, AND
     - bytes_used + file.size <= max_total_bytes
  3. Files that don't fit go to ``dropped`` with a reason.
  4. Individual files larger than ``max_per_file_bytes`` are truncated
     to that cap before fitting; the truncated content includes a
     ``... truncated`` marker the LLM can read.

The resulting EvidencePack carries metrics for the event log so we
can dashboard "how often were we capped" and tune budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Defaults sized for DeepSeek-V4-Pro's reliable codegen window
# (~30k tokens; ~20k bytes of source after subtracting prompt
# overhead). Override per-call by passing a custom EvidencePackBudget.
@dataclass(frozen=True)
class EvidencePackBudget:
    max_files: int = 6
    max_total_bytes: int = 18_000
    max_per_file_bytes: int = 6_000


@dataclass
class FileEvidence:
    path: str
    content: str
    priority: int = 5  # lower = higher priority


@dataclass
class DroppedEvidence:
    path: str
    reason: str
    bytes_size: int


@dataclass
class EvidencePack:
    included_files: list[FileEvidence]
    dropped: list[DroppedEvidence]
    metrics: dict[str, object] = field(default_factory=dict)


_TRUNCATE_MARKER = "\n... (truncated)"


def truncate_file(
    content: str,
    *,
    max_bytes: int,
    path: str | None = None,
    keep_symbols: list[str] | None = None,
) -> str:
    """Cut a single file's content to stay under ``max_bytes``.

    For Python files (path ending ``.py``), use AST-aware structural
    truncation so function bodies are preserved intact wherever
    possible. The fix on 2026-05-09 was driven by a 2000-line django
    file where naive byte truncation kept only imports and class
    headers, leaving the LLM nothing to edit.

    For non-Python files (or when AST parsing fails), fall back to the
    historical byte truncation. ``keep_symbols`` is forwarded to the
    AST truncator when relevant; ignored for the byte path.
    """
    if len(content) <= max_bytes:
        return content
    if path and path.endswith(".py"):
        try:
            from app.services.ast_truncate import truncate_python_source

            result = truncate_python_source(
                content,
                max_bytes=max_bytes,
                keep_symbols=keep_symbols or [],
            )
            if result.used_ast and result.text:
                if len(result.text) <= max_bytes:
                    return result.text
                # AST truncation shrank the file but couldn't fit it
                # under the cap (e.g. dozens of small methods all
                # pinned). Byte-cap the AST output rather than raw
                # source — the AST version drops big function bodies
                # first, so even after a final byte cut the model
                # sees more useful structure.
                return result.text[:max_bytes] + _TRUNCATE_MARKER
        except Exception:  # noqa: BLE001
            pass
    return content[:max_bytes] + _TRUNCATE_MARKER


def build_evidence_pack(
    files: Iterable[FileEvidence],
    budget: EvidencePackBudget,
) -> EvidencePack:
    """Apply the budget; return what fit and what didn't."""
    sorted_files = sorted(files, key=lambda f: (f.priority, f.path))
    included: list[FileEvidence] = []
    dropped: list[DroppedEvidence] = []
    bytes_used = 0

    for file_ in sorted_files:
        if len(included) >= budget.max_files:
            dropped.append(
                DroppedEvidence(
                    path=file_.path,
                    reason=f"max_files={budget.max_files} exceeded",
                    bytes_size=len(file_.content),
                )
            )
            continue

        # Per-file truncation first. AST-aware for Python files (so
        # big files like django/db/models/sql/query.py keep function
        # bodies intact instead of dumping just imports).
        truncated = (
            truncate_file(
                file_.content,
                max_bytes=budget.max_per_file_bytes,
                path=file_.path,
            )
            if len(file_.content) > budget.max_per_file_bytes
            else file_.content
        )
        size = len(truncated)

        if bytes_used + size > budget.max_total_bytes:
            dropped.append(
                DroppedEvidence(
                    path=file_.path,
                    reason=(
                        f"max_total_bytes={budget.max_total_bytes} "
                        f"would exceed (current {bytes_used}, file {size})"
                    ),
                    bytes_size=size,
                )
            )
            continue

        included.append(
            FileEvidence(path=file_.path, content=truncated, priority=file_.priority)
        )
        bytes_used += size

    metrics = {
        "files_included": len(included),
        "files_dropped": len(dropped),
        "bytes_used": bytes_used,
        "max_files": budget.max_files,
        "max_total_bytes": budget.max_total_bytes,
        "max_per_file_bytes": budget.max_per_file_bytes,
    }

    return EvidencePack(included_files=included, dropped=dropped, metrics=metrics)


def render_evidence_for_prompt(pack: EvidencePack) -> str:
    """Format the pack for inclusion in a codegen system prompt.

    Layout:

        ### Evidence file 1: <path>
        ```
        <content>
        ```

        ### Evidence file 2: ...

        ### Dropped (couldn't fit budget)
        - <path>: <reason>
    """
    sections: list[str] = []
    for idx, ev in enumerate(pack.included_files, start=1):
        sections.append(f"### Evidence file {idx}: {ev.path}\n```\n{ev.content}\n```")
    if pack.dropped:
        sections.append(
            "### Dropped (couldn't fit budget)\n"
            + "\n".join(f"- {d.path}: {d.reason}" for d in pack.dropped)
        )
    return "\n\n".join(sections)
