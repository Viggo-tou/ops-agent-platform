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
) -> FeaturePresenceResult:
    """Static feature-presence eval.

    Args:
        must_touch_files: files the planner said must be modified.
        file_contents: post-apply file content keyed by relative path.
        required_tokens: tokens we expect to see in the must_touch files.
        min_tokens_per_file: minimum number of required tokens that must
            appear in EACH must_touch file. Defaults to 1.

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

    matched_per_file: dict[str, list[str]] = {}
    unmatched: list[str] = []
    lower_tokens = {t.lower() for t in required_tokens}
    for must_path in must_touch:
        # Suffix-tolerant lookup (mirror evidence_chain helper)
        content = ""
        for path, body in file_contents.items():
            if (
                path == must_path
                or path.endswith("/" + must_path)
                or must_path.endswith("/" + path)
            ):
                content = body or ""
                break
        if not content:
            unmatched.append(must_path)
            matched_per_file[must_path] = []
            continue
        # Stage X.8.b improvement: strip comments before token grep so
        # codegen can't fool the gate by putting required tokens in `//`.
        content = _strip_comments(content)
        content_lower = content.lower()
        hits = [tok for tok in required_tokens if tok.lower() in content_lower]
        matched_per_file[must_path] = hits
        if len(hits) < min_tokens_per_file:
            unmatched.append(must_path)

    if unmatched:
        sample = ", ".join(unmatched[:3])
        return FeaturePresenceResult(
            feature_absent=True,
            reason=(
                f"feature presence check: {len(unmatched)} must_touch file(s) "
                f"lack required tokens (sample: {sample}). Required tokens: "
                f"{required_tokens[:8]}..."
            ),
            required_tokens=list(required_tokens),
            matched_per_file=matched_per_file,
            unmatched_required_files=unmatched,
        )

    return FeaturePresenceResult(
        feature_absent=False,
        reason="all must_touch files contain at least 1 required token",
        required_tokens=list(required_tokens),
        matched_per_file=matched_per_file,
        unmatched_required_files=[],
    )
