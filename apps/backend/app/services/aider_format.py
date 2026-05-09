"""Aider-style search/replace block parser, applier, diff converter.

Why this format. Unified diff requires the model to reproduce hunk
counts and context lines byte-for-byte. Mid-tier models (DeepSeek,
GPT-4o-mini, Mistral) miscount or paraphrase context, so the diff
fails to apply. Aider published data showing search/replace blocks
beat unified diff by 15-25 percentage points on coding benchmarks
for those models. The format reads as prose, has a built-in anchor
(the SEARCH block IS the anchor), and can be trivially converted to
unified diff for downstream consumers.

Format::

    filename.py
    <<<<<<< SEARCH
    exact source text
    =======
    new text
    >>>>>>> REPLACE

Multiple blocks per file are allowed; just emit them back to back
under the same filename header. Empty SEARCH + ``### NEW FILE: <path>``
header on the line above the filename means "create new file".
Empty REPLACE means "delete the matched region".

Apply contract:

  1. SEARCH must occur exactly once in the current file. 0 → fail
     with anchor_not_found, ≥2 → fail with anchor_ambiguous.
  2. Edits to the same file are applied in the order given so that
     a later block can match a region introduced by an earlier one.
  3. After all blocks apply, callers can call
     aider_blocks_to_unified_diff() to get a normal unified diff for
     SWE-bench / git apply / human review.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AiderBlock:
    file: str
    search: str           # may be empty for new-file create
    replace: str          # may be empty for delete
    is_new_file: bool = False


@dataclass(frozen=True)
class AiderApplyError:
    file: str
    block_index: int
    reason: str


@dataclass
class AiderApplyResult:
    applied_files: list[str] = field(default_factory=list)
    errors: list[AiderApplyError] = field(default_factory=list)
    # Map of file → (before_content, after_content) for any file that
    # was touched. Used to construct the unified diff at the boundary.
    before_after: dict[str, tuple[str, str]] = field(default_factory=dict)


_SEARCH_HEAD = "<<<<<<< SEARCH"
_DIVIDER = "======="
_REPLACE_TAIL = ">>>>>>> REPLACE"
_NEW_FILE_RE = re.compile(r"^### NEW FILE:\s*(\S+)\s*$")


class AiderParseError(Exception):
    """Raised when the LLM output is not parseable as Aider blocks."""


def parse_aider_blocks(text: str) -> list[AiderBlock]:
    """Parse a model's Aider-format output into a sequence of blocks.

    Tolerant of blank lines between blocks and between a filename
    header and its first SEARCH marker. Raises AiderParseError on
    malformed input.
    """
    lines = text.splitlines()
    blocks: list[AiderBlock] = []
    i = 0
    pending_new_file: str | None = None
    current_file: str | None = None

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # NEW FILE marker — applies to the next filename + block.
        new_file_match = _NEW_FILE_RE.match(line)
        if new_file_match:
            pending_new_file = new_file_match.group(1)
            i += 1
            continue

        # Skip blank lines outside blocks.
        if stripped == "":
            i += 1
            continue

        # SEARCH marker without a known file → error.
        if stripped == _SEARCH_HEAD:
            if current_file is None:
                raise AiderParseError(
                    f"<<<<<<< SEARCH at line {i + 1} has no filename header above it"
                )
            search_lines: list[str] = []
            replace_lines: list[str] = []
            i += 1
            in_search = True
            in_replace = False
            saw_divider = False
            saw_tail = False
            while i < len(lines):
                inner = lines[i]
                inner_stripped = inner.strip()
                if in_search and inner_stripped == _DIVIDER:
                    in_search = False
                    in_replace = True
                    saw_divider = True
                    i += 1
                    continue
                if in_replace and inner_stripped == _REPLACE_TAIL:
                    saw_tail = True
                    i += 1
                    break
                if in_search:
                    search_lines.append(inner)
                else:
                    replace_lines.append(inner)
                i += 1
            if not saw_divider:
                raise AiderParseError(
                    f"missing ======= divider after <<<<<<< SEARCH for {current_file}"
                )
            if not saw_tail:
                raise AiderParseError(
                    f"missing >>>>>>> REPLACE for {current_file}"
                )
            blocks.append(
                AiderBlock(
                    file=current_file,
                    search="\n".join(search_lines),
                    replace="\n".join(replace_lines),
                    is_new_file=(pending_new_file == current_file),
                )
            )
            pending_new_file = None
            continue

        # Otherwise this line is a filename header.
        if stripped:
            current_file = stripped
            i += 1
            continue
        i += 1

    if not blocks:
        raise AiderParseError("no blocks found in input")
    return blocks


def apply_aider_blocks(
    blocks: list[AiderBlock], sandbox_dir: Path
) -> AiderApplyResult:
    """Apply the parsed blocks against the sandbox.

    Reads each file once before its first block, accumulates edits,
    and writes the file back at the end. Captures before/after for
    diff generation downstream.
    """
    sandbox = Path(sandbox_dir)
    file_state: dict[str, str] = {}   # current text per file
    file_before: dict[str, str] = {}  # original text (for diff)
    result = AiderApplyResult()

    for idx, block in enumerate(blocks):
        target = sandbox / block.file
        if block.is_new_file:
            if block.file in file_state:
                # Already created earlier in this batch; keep current.
                pass
            else:
                if target.exists():
                    result.errors.append(
                        AiderApplyError(
                            file=block.file,
                            block_index=idx,
                            reason="new_file requested but file already exists",
                        )
                    )
                    continue
                file_state[block.file] = ""
                file_before[block.file] = ""

        if block.file not in file_state:
            try:
                original = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                result.errors.append(
                    AiderApplyError(
                        file=block.file,
                        block_index=idx,
                        reason="file not found in sandbox",
                    )
                )
                continue
            except UnicodeDecodeError as exc:
                result.errors.append(
                    AiderApplyError(
                        file=block.file,
                        block_index=idx,
                        reason=f"file is not utf-8: {exc}",
                    )
                )
                continue
            file_state[block.file] = original
            file_before[block.file] = original

        current = file_state[block.file]
        # New file with empty search → set content directly.
        if block.is_new_file and block.search == "":
            file_state[block.file] = block.replace
            continue

        # Empty search on an existing file = append at end.
        if block.search == "":
            file_state[block.file] = current + block.replace
            continue

        occurrences = current.count(block.search)
        if occurrences == 0:
            result.errors.append(
                AiderApplyError(
                    file=block.file,
                    block_index=idx,
                    reason="anchor_not_found: SEARCH block does not match any region",
                )
            )
            continue
        if occurrences > 1:
            result.errors.append(
                AiderApplyError(
                    file=block.file,
                    block_index=idx,
                    reason=f"anchor_ambiguous: SEARCH block matches {occurrences} regions",
                )
            )
            continue

        file_state[block.file] = current.replace(
            block.search, block.replace, 1
        )

    # Write only files that actually changed; leave touched-but-unchanged
    # alone so we don't perturb mtimes.
    for path, after in file_state.items():
        before = file_before.get(path, "")
        if after == before and not any(b.is_new_file and b.file == path for b in blocks):
            continue
        target = sandbox / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(after, encoding="utf-8")
        result.applied_files.append(path)
        result.before_after[path] = (before, after)

    return result


def apply_aider_blocks_in_memory(
    blocks: list[AiderBlock], originals: dict[str, str]
) -> AiderApplyResult:
    """Apply Aider blocks against an in-memory dict of file contents.

    Mirrors :func:`apply_aider_blocks` but does not touch the filesystem.
    Used by the codegen pipeline, where the LLM emits blocks and we
    convert them to a unified diff before any sandbox exists. ``originals``
    maps file paths → current contents (empty string is fine for new
    files). The returned ``before_after`` map is what feeds
    :func:`aider_blocks_to_unified_diff`.
    """
    file_state: dict[str, str] = {}
    file_before: dict[str, str] = {}
    result = AiderApplyResult()

    for idx, block in enumerate(blocks):
        if block.is_new_file:
            if block.file in file_state:
                pass
            else:
                if block.file in originals and originals[block.file].strip():
                    result.errors.append(
                        AiderApplyError(
                            file=block.file,
                            block_index=idx,
                            reason="new_file requested but file already has content in context",
                        )
                    )
                    continue
                file_state[block.file] = ""
                file_before[block.file] = ""

        if block.file not in file_state:
            if block.file not in originals:
                result.errors.append(
                    AiderApplyError(
                        file=block.file,
                        block_index=idx,
                        reason="file not present in codegen context",
                    )
                )
                continue
            original = originals[block.file]
            file_state[block.file] = original
            file_before[block.file] = original

        current = file_state[block.file]

        if block.is_new_file and block.search == "":
            file_state[block.file] = block.replace
            continue
        if block.search == "":
            file_state[block.file] = current + block.replace
            continue

        occurrences = current.count(block.search)
        if occurrences == 0:
            result.errors.append(
                AiderApplyError(
                    file=block.file,
                    block_index=idx,
                    reason="anchor_not_found: SEARCH block does not match any region",
                )
            )
            continue
        if occurrences > 1:
            result.errors.append(
                AiderApplyError(
                    file=block.file,
                    block_index=idx,
                    reason=f"anchor_ambiguous: SEARCH block matches {occurrences} regions",
                )
            )
            continue

        file_state[block.file] = current.replace(
            block.search, block.replace, 1
        )

    for path, after in file_state.items():
        before = file_before.get(path, "")
        if after == before and not any(b.is_new_file and b.file == path for b in blocks):
            continue
        result.applied_files.append(path)
        result.before_after[path] = (before, after)

    return result


def aider_blocks_to_unified_diff(
    result: AiderApplyResult, *, context_lines: int = 3
) -> str:
    """Build a single unified diff covering everything the apply touched.

    Uses ``difflib.unified_diff`` so the output is git-apply compatible.
    Returns "" if no files changed (caller should treat as "no patch").
    """
    chunks: list[str] = []
    for path, (before, after) in sorted(result.before_after.items()):
        before_lines = before.splitlines(keepends=True) if before else []
        after_lines = after.splitlines(keepends=True) if after else []
        diff = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=context_lines,
            )
        )
        if not diff.strip():
            continue
        chunks.append(f"diff --git a/{path} b/{path}\n{diff}")
    return "".join(chunks)
