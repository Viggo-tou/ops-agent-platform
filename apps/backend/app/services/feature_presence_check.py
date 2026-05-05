"""Static feature-presence check (Stage X.8.b dogfood-trigger fix).

Codex consult on P69-17 dogfood: compile_repair reverted feature code,
leaving baseline file. All 5 LLM gates passed because diff_text mentioned
the relevant tokens, but final FILE content had no implementation.

Static check:
- Per task, derive required_tokens from plan.objective + translation
  search_queries + must_touch file names (heuristic).
- For each must_touch file, scan post-apply content for at least one
  required_token. If any required must_touch file has 0 matches ->
  feature_absent, reject.

Future work:
- Extract required_tokens from a structured 'feature contract' field
  on plan, instead of heuristic.
- Per-token weighting / threshold.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FeaturePresenceResult:
    feature_absent: bool
    reason: str
    required_tokens: list[str]
    matched_per_file: dict[str, list[str]]
    unmatched_required_files: list[str]

    def to_payload(self) -> dict[str, object]:
        return {
            "feature_absent": self.feature_absent,
            "reason": self.reason,
            "required_tokens": list(self.required_tokens),
            "matched_per_file": dict(self.matched_per_file),
            "unmatched_required_files": list(self.unmatched_required_files),
        }


_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_GENERIC_ENGLISH_STOPWORDS = frozenset({
    # boilerplate planner-step verbs
    "implement", "implementing", "generating", "generate", "applying", "apply",
    "running", "reviewing", "review", "changes", "change", "patches", "patch",
    "tests", "test", "results", "result",
    # ticket/process noise
    "jira", "issue", "ticket", "task", "story",
    # generic functional words
    "the", "and", "for", "with", "from", "this", "that", "should", "will",
    "must", "can", "may", "when", "while", "first", "load", "save", "saved",
    "default", "create", "creating", "creation", "edit", "allow", "fill",
    "edit", "manually", "moving", "both", "field",
    # common UI primitives
    "view", "screen", "button", "input", "form", "label", "text", "page",
    "component", "panel", "modal", "dialog",
    # generic domain-low-signal words
    "user", "users", "data", "value", "values", "item", "items", "list",
    "type", "name", "code", "info", "detail", "title", "status", "state",
    "home", "address", "location", "map", "pin", "profile", "account",
    "signup", "login", "phone", "email", "date", "time",
})


def count_unique_identifiers_in_text(text: str) -> int:
    """Count distinct identifier-shaped tokens (CamelCase / snake_case)
    in `text`, excluding generic-English stopwords.

    Used by ``evaluate_feature_presence`` as a fallback signal when the
    spec yields too few strict tokens to anchor a meaningful gate. The
    rule is "codegen must add at least N distinct structured identifiers",
    which catches the v10b shell-only-edit pattern (where only English
    comments + UI primitives like `match_parent` get added) while
    accepting real implementations that add new fields, classes, or
    functions.
    """
    seen: set[str] = set()
    if not text:
        return 0
    for tok in _TOKEN_RE.findall(text):
        if not _is_identifier_shaped(tok):
            continue
        if tok.lower() in _GENERIC_ENGLISH_STOPWORDS:
            continue
        seen.add(tok)
    return len(seen)


def _is_identifier_shaped(tok: str) -> bool:
    """A token is 'specific' enough to count as feature evidence when:
      - it has a CamelCase boundary (homeAddress, JobPostingFlow), or
      - it contains an underscore (home_address, save_to_db), or
      - it is SCREAMING_SNAKE (HOME_ADDRESS).

    Plain English words ('home', 'address', 'user') return False — they
    pollute feature-presence checks because they appear naturally in
    pre-existing source code regardless of whether the new feature was
    implemented.
    """
    if not tok:
        return False
    # camelCase / PascalCase: lower->upper transition
    if re.search(r"[a-z][A-Z]", tok):
        return True
    # snake_case (and SCREAMING_SNAKE which contains _ too)
    if "_" in tok:
        return True
    return False


def _stem_tokens(text: str) -> list[str]:
    """Extract camelCase / snake_case identifiers (>=3 chars) from text."""
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text):
        norm = tok.strip()
        if not norm or norm.lower() in {"the", "and", "for", "with", "from", "this", "that", "should", "will", "must", "can", "may"}:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def derive_required_tokens(
    *,
    objective: str = "",
    search_queries: list[str] | None = None,
    must_touch_files: list[str] | None = None,
    spec_text: str = "",
) -> list[str]:
    """Heuristic: union of identifier-shaped tokens from objective +
    search_queries + must_touch basenames."""
    pool: list[str] = []
    pool.extend(_stem_tokens(objective))
    pool.extend(_stem_tokens(spec_text))
    for q in (search_queries or []):
        if isinstance(q, str):
            pool.extend(_stem_tokens(q))
    for path in (must_touch_files or []):
        if isinstance(path, str):
            base = path.replace("\\", "/").rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            pool.extend(_stem_tokens(stem))
    seen: set[str] = set()
    deduped: list[str] = []
    for t in pool:
        if t in seen:
            continue
        seen.add(t)
        deduped.append(t)
    return deduped


def derive_required_tokens_strict(
    *,
    objective: str = "",
    grounding_terms: list[str] | None = None,
    spec_text: str = "",
    must_touch_files: list[str] | None = None,
) -> list[str]:
    """G2: stricter token derivation.

    Returns ONLY identifier-shaped tokens (CamelCase / snake_case) plus
    any existing identifier-shaped substrings from grounding_terms /
    must_touch basenames. Generic English words are dropped via
    `_GENERIC_ENGLISH_STOPWORDS` and the `_is_identifier_shaped`
    structural filter.

    Rationale: pre-G2 derive_required_tokens collected every word from
    the planner step descriptions ("Implement / Jira / generating /
    code / changes ..."), and feature_presence then accepted any 1
    match — so a file that already contained any of those words pre-
    edit would pass the gate without the feature being implemented.
    """
    pool: list[str] = []
    pool.extend(_stem_tokens(objective))
    pool.extend(_stem_tokens(spec_text))
    for g in (grounding_terms or []):
        if isinstance(g, str):
            pool.extend(_stem_tokens(g))
    for path in (must_touch_files or []):
        if isinstance(path, str):
            base = path.replace("\\", "/").rsplit("/", 1)[-1]
            stem = base.rsplit(".", 1)[0]
            pool.extend(_stem_tokens(stem))

    seen: set[str] = set()
    deduped: list[str] = []
    for t in pool:
        if t in seen:
            continue
        seen.add(t)
        # Drop tokens that look like English (no CamelCase / no underscore).
        if not _is_identifier_shaped(t):
            continue
        # Even identifier-shaped tokens can be stopwords ("View", "Page",
        # "Status") — drop those.
        if t.lower() in _GENERIC_ENGLISH_STOPWORDS:
            continue
        deduped.append(t)
    return deduped


def merge_diffs_by_file(previous_diff: str, new_diff: str) -> str:
    """Merge two unified diffs at the file granularity.

    Each `diff --git a/PATH b/PATH ... ` block is treated as one unit.
    For files appearing in both diffs, ``new_diff`` wins (latest version
    of that file's hunk replaces previous). For files appearing in only
    one, that block is preserved.

    This is needed for the feature_presence repair loop: each repair
    round's codegen produces a fresh diff against the pristine source.
    Without merging, round N's diff for file A overwrites round N-1's
    diff for file B (we lose B's changes). Result: alternating
    "fix-A-lose-B / fix-B-lose-A" oscillation observed in P69-17 v14.

    The function is intentionally schema-light — it relies on the
    `diff --git a/PATH b/PATH` header line as the file boundary. Empty
    diffs return the other side cleanly.
    """
    if not previous_diff and not new_diff:
        return ""
    if not previous_diff:
        return new_diff
    if not new_diff:
        return previous_diff

    def _split_into_file_blocks(diff: str) -> dict[str, str]:
        """Return {b_path: full_block_text}. Order-preserving."""
        blocks: dict[str, str] = {}
        current_path: str | None = None
        current_lines: list[str] = []
        for line in diff.split("\n"):
            if line.startswith("diff --git "):
                if current_path is not None:
                    blocks[current_path] = "\n".join(current_lines)
                # Parse `diff --git a/X b/Y` -> Y as canonical key.
                parts = line.split(" b/", 1)
                current_path = parts[1].strip() if len(parts) == 2 else line
                current_lines = [line]
            else:
                current_lines.append(line)
        if current_path is not None:
            blocks[current_path] = "\n".join(current_lines)
        return blocks

    prev_blocks = _split_into_file_blocks(previous_diff)
    new_blocks = _split_into_file_blocks(new_diff)

    merged_paths: list[str] = []
    for p in prev_blocks:
        if p not in merged_paths:
            merged_paths.append(p)
    for p in new_blocks:
        if p not in merged_paths:
            merged_paths.append(p)

    out_blocks: list[str] = []
    for p in merged_paths:
        # New diff wins for files it touches; previous preserved otherwise.
        block = new_blocks.get(p) or prev_blocks.get(p) or ""
        if block.strip():
            out_blocks.append(block)
    return "\n".join(out_blocks)


def extract_added_lines_per_file(diff: str) -> dict[str, str]:
    """Parse a unified diff and return {file_path: "added_line_1\\nadded_line_2\\n..."}.

    'Added' means lines starting with '+' but excluding the file-header
    '+++' lines. Returns relative paths as they appear in the diff (using
    'b/' side, with the 'b/' prefix stripped). Used by feature_presence
    in G2-strict mode to scan only newly added code.
    """
    if not diff:
        return {}
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in diff.split("\n"):
        if line.startswith("+++ "):
            # +++ b/path/to/file
            tail = line[4:].strip()
            if tail.startswith("b/"):
                tail = tail[2:]
            elif tail == "/dev/null":
                current = None
                continue
            current = tail or None
            if current is not None:
                out.setdefault(current, [])
            continue
        if line.startswith("--- "):
            # opening of a hunk file pair; '+++' will set the path
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            out.setdefault(current, []).append(line[1:])
    return {p: "\n".join(lines) for p, lines in out.items()}


def _strip_comments(content: str) -> str:
    """Remove single-line and block comments + XML/HTML comments + YAML
    hash-comments from content. Whitespace-preserving — replaces comment
    bytes with spaces so line/column references remain valid (best-effort).

    Strips:
      - `// line comment` (Java, Kotlin, JS, TS, C, C++, Swift)
      - `# line comment` (Python, YAML, sh)
      - `/* block comment */` (C-family, multi-line)
      - `<!-- xml/html comment -->` (multi-line)

    Does NOT strip docstrings (intentional content).
    """
    if not content:
        return content
    import re as _re

    # Block comments first (greedy multi-line):
    out = _re.sub(r"/\*[\s\S]*?\*/", lambda m: " " * len(m.group(0)), content)
    out = _re.sub(r"<!--[\s\S]*?-->", lambda m: " " * len(m.group(0)), out)

    # Line comments — process line-by-line so we don't strip URLs containing //.
    lines: list[str] = []
    for line in out.split("\n"):
        # Find the position of // not inside a string literal (heuristic):
        # we use a simple rule — strip from first // to end of line. False
        # positives on URLs in string literals are acceptable for a
        # token-presence check (the goal is to prevent comment-stuffing,
        # not to preserve every legal token).
        for marker in ("//", "#"):
            idx = line.find(marker)
            if idx >= 0:
                line = line[:idx] + " " * (len(line) - idx)
                break
        lines.append(line)
    return "\n".join(lines)


def evaluate_feature_presence(
    *,
    must_touch_files: list[str],
    file_contents: dict[str, str],
    required_tokens: list[str],
    min_tokens_per_file: int = 1,
    diff_added_per_file: dict[str, str] | None = None,
    min_tokens_per_file_ratio: float | None = None,
    sparse_token_threshold: int = 3,
    min_unique_identifiers_fallback: int = 3,
) -> FeaturePresenceResult:
    """Static feature-presence eval.

    Args:
        must_touch_files: files the planner said must be modified.
        file_contents: post-apply file content keyed by relative path.
        required_tokens: tokens we expect to see in the must_touch files.
        min_tokens_per_file: absolute minimum count threshold; default 1.
        diff_added_per_file: G2 — when supplied, the scan runs against
            ONLY the added lines from this file's diff (post strip-comments)
            instead of the full post-apply file content. This blocks the
            "shell-only edit + planner-keyword soup" cheat where pre-
            existing words in the file satisfied the gate without the
            new feature actually being implemented.
        min_tokens_per_file_ratio: G2 — when supplied (e.g. 0.5), the
            effective threshold becomes
            ``max(min_tokens_per_file, ceil(ratio * len(required_tokens)))``.
            Forces real proportional coverage, not "any 1 hit passes".

    Returns FeaturePresenceResult with:
        feature_absent: True if any must_touch file lacks required tokens.
        reason: human-readable summary.
        required_tokens: the tokens checked.
        matched_per_file: per-file list of tokens that matched.
        unmatched_required_files: files missing required tokens.
    """
    must_touch = {p.strip() for p in must_touch_files if isinstance(p, str) and p.strip()}
    if not must_touch:
        return FeaturePresenceResult(
            feature_absent=False,
            reason="no must_touch files; skipping",
            required_tokens=list(required_tokens),
            matched_per_file={},
            unmatched_required_files=[],
        )
    if not required_tokens:
        return FeaturePresenceResult(
            feature_absent=False,
            reason="no required_tokens derived; skipping",
            required_tokens=[],
            matched_per_file={},
            unmatched_required_files=[],
        )

    # Compute effective threshold once.
    threshold = int(min_tokens_per_file)
    if min_tokens_per_file_ratio is not None and required_tokens:
        import math as _math
        ratio_threshold = _math.ceil(min_tokens_per_file_ratio * len(required_tokens))
        threshold = max(threshold, ratio_threshold)
    threshold = max(threshold, 1)

    matched_per_file: dict[str, list[str]] = {}
    unmatched: list[str] = []
    diff_mode = diff_added_per_file is not None

    # Sparse-token fallback: when the spec produced too few strict
    # tokens to anchor a meaningful gate (typical for prose-only Jira
    # tickets), supplement with a "structural diff substance" check —
    # the diff additions themselves must contain >= N distinct
    # identifier-shaped tokens. This still catches shell-only edits
    # (which add only English comments + UI primitives) while accepting
    # real implementations.
    use_sparse_fallback = (
        diff_mode and len(required_tokens) < sparse_token_threshold
    )

    for must_path in must_touch:
        # Pick scan source: diff added lines (G2 strict) or full file body.
        body = ""
        if diff_mode:
            body = _suffix_tolerant_get(diff_added_per_file or {}, must_path)
        else:
            body = _suffix_tolerant_get(file_contents, must_path)

        if not body:
            unmatched.append(must_path)
            matched_per_file[must_path] = []
            continue

        # Strip comments BEFORE token grep so the gate cannot be fooled
        # by tokens parked inside `//` or `/* */` blocks.
        body_stripped = _strip_comments(body)
        body_lower = body_stripped.lower()
        hits = [tok for tok in required_tokens if tok.lower() in body_lower]
        matched_per_file[must_path] = hits

        if use_sparse_fallback:
            unique_ids = count_unique_identifiers_in_text(body_stripped)
            if unique_ids < min_unique_identifiers_fallback:
                unmatched.append(must_path)
        else:
            if len(hits) < threshold:
                unmatched.append(must_path)

    if unmatched:
        sample = ", ".join(unmatched[:3])
        scope = "diff-added lines" if diff_mode else "file content"
        if use_sparse_fallback:
            tail = (
                f"sparse-token fallback active (only {len(required_tokens)} "
                f"strict token(s); needed >= {min_unique_identifiers_fallback} "
                f"unique identifier(s) per file in diff)"
            )
        else:
            tail = (
                f"Required tokens ({len(required_tokens)}): "
                f"{required_tokens[:8]}..."
            )
        return FeaturePresenceResult(
            feature_absent=True,
            reason=(
                f"feature presence check ({scope}): {len(unmatched)} must_touch "
                f"file(s) failed (sample: {sample}). {tail}"
            ),
            required_tokens=list(required_tokens),
            matched_per_file=matched_per_file,
            unmatched_required_files=unmatched,
        )

    scope = "diff-added lines" if diff_mode else "file content"
    if use_sparse_fallback:
        success_reason = (
            f"all must_touch files have >= "
            f"{min_unique_identifiers_fallback} unique identifier(s) in "
            f"diff (sparse-token fallback)"
        )
    else:
        success_reason = (
            f"all must_touch files contain >= {threshold} required "
            f"token(s) in {scope}"
        )
    return FeaturePresenceResult(
        feature_absent=False,
        reason=success_reason,
        required_tokens=list(required_tokens),
        matched_per_file=matched_per_file,
        unmatched_required_files=[],
    )


def _suffix_tolerant_get(
    bag: dict[str, str], must_path: str
) -> str:
    """Return the value whose key path best matches `must_path`.

    Mirrors the evidence_chain suffix-tolerant lookup so a planner-emitted
    relative path resolves regardless of whether the writer used the same
    workdir prefix. Returns empty string if no match.
    """
    for path, body in bag.items():
        if (
            path == must_path
            or path.endswith("/" + must_path)
            or must_path.endswith("/" + path)
        ):
            return body or ""
    return ""
