"""Contract Coverage verification (v16.2).

The C2-class failure surfaced in b5d0a085 / f2fce4f4: the model returns
``NO_CHANGE_NEEDED_VERIFIED`` for a file that has ONE of the required
signals (latitude/longitude persisted) but is missing the actual feature
(map UI). Prose-level claims pass the literal check but miss the spirit.

The fix is contract-level coverage:

  1. Plan carries ``required_contracts`` — list of contract IDs from the
     matched domain playbook.
  2. Codegen output must include a ``## CONTRACT_COVERAGE`` JSON block
     declaring per-contract status: ``implemented_contracts`` /
     ``verified_no_change_contracts`` / ``unimplemented_contracts``.
  3. Harness verifies each declaration against the actual artifact (the
     added-lines of the diff for ``implemented``, the file content for
     ``verified_no_change``) using the contract's verification patterns
     from the playbook. The model cannot satisfy the gate with prose
     alone — the patterns have to grep-match.
  4. Set equation: ``required_contracts ⊆ implemented ∪
     verified_no_change ∪ explicitly_deferred``. Anything else → reject.
  5. ``claimed but failed verification`` is treated worse than ``not
     claimed at all``: the model is lying.

Failure classes addressed:
  - C2 (polish_passing_as_feature) — both variants:
      C2a: empty acceptance_tests (gated by v16.1)
      C2b: trivially-true acceptance_tests where the model satisfies
           letter but not spirit (today's case)

Not yet addressed:
  - The ChangeIntent ledger (v16.3) — still needed to record what
    contract a repair must preserve. Coverage Output tells us WHAT was
    implemented; ledger tells us WHAT MUST NOT BE LOST.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

_logger = logging.getLogger("contract_coverage")


# ---- Data shapes --------------------------------------------------------


# Verification rule kinds (v16.2.1 — diff-anchored final-tree verification).
#
# - ``diff_contains_pattern``: regex must hit at least one + line in the
#   merged diff (existing behavior).
# - ``final_context_contains_pattern``: regex must hit the post-patch file
#   content within a scope ANCHORED to a changed hunk (e.g. same function
#   body or N-line window around the new_start). This is the load-bearing
#   primitive for "diff modifies payload + existing sink consumes it" —
#   the sink line is unchanged but in the same scope as the changed hunk.
# - ``any_of`` / ``all_of``: compose sub-rules. ``rules`` carries them.
#
# Phase A introduces the schema; the verifier evaluator that consumes
# composite rules lands in Phase B. The legacy flat
# ``verification_patterns: list[str]`` is preserved on RequiredContract
# so the Phase A change is behavior-equivalent.
VerificationRuleKind = Literal[
    "diff_contains_pattern",
    "final_context_contains_pattern",
    "any_of",
    "all_of",
]


@dataclass
class VerificationRule:
    """A single verification rule from the playbook.

    Composite kinds (``any_of`` / ``all_of``) carry their children in
    ``rules`` and ignore ``pattern``; leaf kinds carry ``pattern`` (and
    optionally ``anchor`` / ``scope`` for ``final_context_contains_pattern``).
    """
    kind: VerificationRuleKind = "diff_contains_pattern"
    pattern: str = ""
    rules: list["VerificationRule"] = field(default_factory=list)
    # For ``final_context_contains_pattern`` only:
    #   anchor: "changed_hunk" — the rule is evaluated once per changed hunk
    #     in the diff; matching ANY hunk counts as a pass.
    #   scope: "same_function" — search a function-body window around the
    #     hunk's new_start in the patched file. Falls back to a fixed
    #     window when no enclosing function is found.
    #   "window:N" — search N lines on either side of the hunk's
    #     new_start in the patched file.
    anchor: str = ""
    scope: str = ""
    # Optional: when set, restrict the rule's evaluation to files whose
    # path matches this glob/substring. Empty = all files.
    file_filter: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "pattern": self.pattern,
            "rules": [r.to_dict() for r in self.rules],
            "anchor": self.anchor,
            "scope": self.scope,
            "file_filter": self.file_filter,
        }


@dataclass
class RequiredContract:
    """A contract the plan committed to. Sourced from the domain playbook
    `required_contracts` list at task-creation time. Carries the
    verification rules so the harness can verify model claims without
    re-reading the playbook every gate run.

    ``verification_patterns`` is the legacy flat list of regexes preserved
    for backwards compatibility with Phase A callers and the v16.2.0
    verifier. ``verifications`` is the new typed/composite form consumed
    by the diff-anchored verifier from Phase B onward. When the playbook
    declares only flat patterns, ``verifications`` mirrors them as a
    list of ``diff_contains_pattern`` leaves.
    """
    contract_id: str
    signal: str
    verification_patterns: list[str] = field(default_factory=list)
    # Optional: patterns that MUST NOT appear (e.g. forbidden_patterns
    # for installed_library_only). When non-empty, the harness ALSO
    # checks the negative side.
    forbidden_patterns: list[str] = field(default_factory=list)
    # New in v16.2.1: typed composite verification rules.
    verifications: list[VerificationRule] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "signal": self.signal[:400],
            "verification_patterns": list(self.verification_patterns),
            "forbidden_patterns": list(self.forbidden_patterns),
            "verifications": [v.to_dict() for v in self.verifications],
        }


@dataclass
class CoverageClaim:
    """One row from the model's ``## CONTRACT_COVERAGE`` block.
    Fields are tolerant of missing/extra keys so the parser doesn't
    blow up on a slightly malformed model output.

    v16.2.1 adds three explanatory fields the prompt now asks the model
    to populate when it claims a contract is implemented via an indirect
    pathway (e.g. "I changed the payload that flows into an existing,
    unmodified sink call"):

      - ``evidence_mode``: e.g. ``direct_diff``,
        ``diff_modified_payload_existing_sink``, ``preexisting``.
      - ``diff_evidence``: short prose pointing at WHAT the diff changes.
      - ``context_evidence``: short prose pointing at the UNCHANGED
        surrounding code that completes the data flow.

    These are advisory: the verifier authoritatively decides via patterns
    and the patched tree. But populating them helps a human reviewer
    understand the verdict, and lets the harness emit clearer error
    messages when verification fails.
    """
    contract_id: str
    file_path: str = ""
    evidence_quote: str = ""
    reason: str = ""  # only meaningful for unimplemented_contracts
    # New in v16.2.1:
    evidence_mode: str = ""
    diff_evidence: str = ""
    context_evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_id": self.contract_id,
            "file_path": self.file_path,
            "evidence_quote": self.evidence_quote[:600],
            "reason": self.reason[:600],
            "evidence_mode": self.evidence_mode[:120],
            "diff_evidence": self.diff_evidence[:600],
            "context_evidence": self.context_evidence[:600],
        }


@dataclass
class CoverageDeclaration:
    """The model's full coverage output, aggregated across batches.
    Each list is a flat union of claims from every batch that touched
    a given contract — a contract claimed by multiple batches lives in
    each list it was claimed in (the harness deduplicates by
    contract_id at verification time).
    """
    implemented_contracts: list[CoverageClaim] = field(default_factory=list)
    verified_no_change_contracts: list[CoverageClaim] = field(default_factory=list)
    unimplemented_contracts: list[CoverageClaim] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "implemented_contracts": [c.to_dict() for c in self.implemented_contracts],
            "verified_no_change_contracts": [c.to_dict() for c in self.verified_no_change_contracts],
            "unimplemented_contracts": [c.to_dict() for c in self.unimplemented_contracts],
        }

    def claimed_contract_ids(self) -> set[str]:
        out: set[str] = set()
        for c in self.implemented_contracts:
            if c.contract_id: out.add(c.contract_id)
        for c in self.verified_no_change_contracts:
            if c.contract_id: out.add(c.contract_id)
        for c in self.unimplemented_contracts:
            if c.contract_id: out.add(c.contract_id)
        return out

    def merge(self, other: "CoverageDeclaration") -> "CoverageDeclaration":
        """Combine two batch outputs into one aggregate declaration."""
        return CoverageDeclaration(
            implemented_contracts=self.implemented_contracts + other.implemented_contracts,
            verified_no_change_contracts=self.verified_no_change_contracts + other.verified_no_change_contracts,
            unimplemented_contracts=self.unimplemented_contracts + other.unimplemented_contracts,
        )


# Coverage verdict kinds.
#
# Phase A taxonomy split (v16.2.1):
#   - ``complete``: every required_contract is verified.
#   - ``incomplete``: some contracts not addressed at all (route to
#     plan_codegen_conflict approval).
#   - ``unverified``: model CLAIMED a contract but the verifier cannot
#     find supporting evidence — this is a verifier limitation OR a
#     genuinely thin claim. Soft fail; route to human review without
#     calling the model a liar. Replaces most of the v16.2.0
#     ``claims_unverified`` cases.
#   - ``contradicted``: the final patched tree DIRECTLY disproves the
#     model's claim (e.g. model says ``lat/lng persisted`` but the patched
#     file still has ``"latitude" to 0.0``). Hard fail.
#   - ``lie``: extreme case where the required contract is wholly absent
#     and the model still claimed it (e.g. claimed ``map_ui_present`` but
#     no map-related symbol appears anywhere). Hard fail.
#   - ``claims_unverified`` (legacy): kept for backwards compatibility
#     with the v16.2.0 verifier output. New verifier code SHOULD emit one
#     of ``unverified`` / ``contradicted`` / ``lie`` instead.
CoverageVerdictKind = Literal[
    "complete",
    "incomplete",
    "unverified",
    "contradicted",
    "lie",
    "claims_unverified",
]


@dataclass
class CoverageVerdict:
    """The harness's judgment after running the verifier against the
    actual sandbox + diff content.

    Outcomes:
      - ``ok = True``: every required_contract has a verified claim.
      - ``ok = False`` with ``lies`` populated: model claimed something
        the verifier could not (``unverified``) or actively disproved
        (``contradicted`` / ``lie``). Routing depends on verdict_kind —
        the orchestrator decides hard-fail vs soft-fail per kind.
      - ``ok = False`` with only ``missing`` populated: contracts weren't
        addressed at all. Route to plan_codegen_conflict approval.
    """
    ok: bool
    verdict_kind: CoverageVerdictKind
    verified_implemented: list[str] = field(default_factory=list)
    verified_no_change: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    # Each entry: {contract_id, claim, reason, severity?}. ``severity`` is
    # populated by the new verifier with one of
    # ``"unverified" | "contradicted" | "lie"`` when known; absent when
    # the legacy verifier produced the entry.
    lies: list[dict[str, str]] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "verdict_kind": self.verdict_kind,
            "verified_implemented": list(self.verified_implemented),
            "verified_no_change": list(self.verified_no_change),
            "missing": list(self.missing),
            "lies": list(self.lies),
            "summary": self.summary[:400],
        }


# ---- Parser -------------------------------------------------------------


# Marker the model emits before the JSON block.
_COVERAGE_MARKER_RE = re.compile(
    r"##\s*CONTRACT_COVERAGE\s*\n",
    re.IGNORECASE,
)


def parse_coverage_block(response_text: str) -> CoverageDeclaration | None:
    """Extract the model's ``## CONTRACT_COVERAGE`` JSON block.

    The model is instructed to emit::

        ## CONTRACT_COVERAGE
        {
          "implemented_contracts": [{"id": "X", "file": "...", "evidence_quote": "..."}],
          "verified_no_change_contracts": [{"id": "Y", "file": "...", "evidence_quote": "..."}],
          "unimplemented_contracts": [{"id": "Z", "reason": "..."}]
        }

    Returns None when the block is absent or unparseable. Caller should
    treat None as a violation (the model didn't follow the contract
    coverage protocol) when ``required_contracts`` is non-empty.
    """
    if not response_text:
        return None
    m = _COVERAGE_MARKER_RE.search(response_text)
    if not m:
        return None
    tail = response_text[m.end():]
    # Find the JSON object after the marker — tolerate code fences.
    tail = tail.lstrip()
    if tail.startswith("```"):
        # Strip ```json fence
        first_newline = tail.find("\n")
        if first_newline >= 0:
            tail = tail[first_newline + 1:]
        fence_end = tail.rfind("```")
        if fence_end >= 0:
            tail = tail[:fence_end]
    # Find the JSON object boundaries.
    start = tail.find("{")
    if start < 0:
        return None
    # Greedy: take until the last } that gives valid JSON. Simpler than
    # a full bracket-matcher and handles model output that includes
    # trailing prose after the JSON.
    import json
    last = tail.rfind("}")
    while last > start:
        candidate = tail[start:last + 1]
        try:
            data = json.loads(candidate)
            break
        except json.JSONDecodeError:
            last = tail.rfind("}", 0, last)
    else:
        return None
    if not isinstance(data, dict):
        return None
    return _declaration_from_dict(data)


def _declaration_from_dict(data: dict[str, Any]) -> CoverageDeclaration:
    def _claims(key: str) -> list[CoverageClaim]:
        raw = data.get(key) or []
        if not isinstance(raw, list):
            return []
        out: list[CoverageClaim] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or item.get("contract_id") or "").strip()
            if not cid:
                continue
            out.append(CoverageClaim(
                contract_id=cid,
                file_path=str(item.get("file") or item.get("file_path") or "").strip(),
                evidence_quote=str(item.get("evidence_quote") or item.get("quote") or "").strip(),
                reason=str(item.get("reason") or "").strip(),
                # New in v16.2.1 — optional; safe to be empty on legacy outputs.
                evidence_mode=str(item.get("evidence_mode") or "").strip(),
                diff_evidence=str(item.get("diff_evidence") or "").strip(),
                context_evidence=str(item.get("context_evidence") or "").strip(),
            ))
        return out
    return CoverageDeclaration(
        implemented_contracts=_claims("implemented_contracts"),
        verified_no_change_contracts=_claims("verified_no_change_contracts"),
        unimplemented_contracts=_claims("unimplemented_contracts"),
    )


# ---- Diff hunk parser (v16.2.1 — Phase B) -------------------------------


@dataclass
class DiffHunk:
    """One ``@@ -X,Y +A,B @@`` block from a unified diff.

    ``new_start`` / ``new_count`` describe where the hunk lands in the
    post-patch file (1-indexed). ``added_lines`` is the joined text of
    only the ``+`` lines (no leading ``+``); ``body`` is the full hunk
    text including unchanged context lines (with their original prefix
    stripped). The verifier uses ``added_lines`` for
    ``diff_contains_pattern`` rules and ``new_start`` to anchor
    ``final_context_contains_pattern`` rules into the patched file.
    """
    file_path: str
    new_start: int = 1
    new_count: int = 0
    added_lines: str = ""
    body: str = ""


_HUNK_HEADER_RE = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")


def _parse_diff_hunks(diff_text: str) -> list[DiffHunk]:
    """Walk a unified diff and yield one DiffHunk per ``@@`` block.

    Handles multi-file diffs by tracking the current ``+++ b/PATH``
    header. Unknown lines between a ``diff --git`` header and the first
    ``@@`` are skipped (typically ``--- a/`` and ``+++ b/`` lines).
    """
    if not diff_text:
        return []
    hunks: list[DiffHunk] = []
    current_file = ""
    current: DiffHunk | None = None
    added: list[str] = []
    body: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                current.added_lines = "\n".join(added)
                current.body = "\n".join(body)
                hunks.append(current)
                current = None
                added = []
                body = []
            current_file = ""
            continue
        if line.startswith("+++ b/"):
            current_file = line[len("+++ b/"):].strip()
            continue
        if line.startswith("+++ "):
            current_file = line[len("+++ "):].strip()
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current is not None:
                current.added_lines = "\n".join(added)
                current.body = "\n".join(body)
                hunks.append(current)
                added = []
                body = []
            new_start = int(m.group(3) or "1")
            new_count = int(m.group(4) or "1")
            current = DiffHunk(
                file_path=current_file,
                new_start=new_start,
                new_count=new_count,
            )
            continue
        if current is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
            body.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            # Removed line — present in pre-patch only; skip from body so
            # body reflects only post-patch content.
            continue
        elif line.startswith(" "):
            body.append(line[1:])
        elif line.startswith("\\"):
            # Diff metadata such as "\ No newline at end of file" — ignore.
            continue
    if current is not None:
        current.added_lines = "\n".join(added)
        current.body = "\n".join(body)
        hunks.append(current)
    return hunks


# ---- Scope resolver (v16.2.1 — Phase B) ---------------------------------


_FUN_DECL_RE = re.compile(
    r"^\s*(?:public|private|internal|protected|inline|suspend|operator|override|open|final|abstract|tailrec|infix|external)*"
    r"\s*fun\s+",
)


def _resolve_scope(
    patched_text: str,
    new_start: int,
    scope: str,
) -> str | None:
    """Return a substring of ``patched_text`` describing the resolution of
    a given ``scope`` anchored at ``new_start`` (1-indexed line).

    Supported scopes (case-insensitive):

      - ``same_function``: walk backwards from ``new_start`` to the
        nearest ``fun XXX(...) {`` declaration; brace-count forward to
        the matching ``}`` and return that range. Falls back to a
        ±60-line window when no enclosing ``fun`` is found.
      - ``window:N``: return ±N lines around ``new_start``.
      - Anything else: return None (caller treats as not-applicable).

    The function is intentionally regex-only (no AST). Kotlin's ``fun``
    keyword and balanced braces are enough to cover the cases the
    verifier needs.
    """
    if not patched_text or new_start <= 0:
        return None
    scope_l = (scope or "").strip().lower()
    if not scope_l:
        return None
    lines = patched_text.splitlines()
    idx0 = max(0, min(new_start - 1, len(lines) - 1))
    if scope_l.startswith("window:"):
        try:
            n = int(scope_l.split(":", 1)[1])
        except ValueError:
            n = 60
        lo = max(0, idx0 - n)
        hi = min(len(lines), idx0 + n + 1)
        return "\n".join(lines[lo:hi])
    if scope_l == "same_function":
        fun_start = -1
        for i in range(idx0, -1, -1):
            if _FUN_DECL_RE.match(lines[i]):
                fun_start = i
                break
        if fun_start < 0:
            lo = max(0, idx0 - 60)
            hi = min(len(lines), idx0 + 60 + 1)
            return "\n".join(lines[lo:hi])
        # Brace-count forward from fun_start until balanced.
        depth = 0
        saw_open = False
        fun_end = len(lines) - 1
        for j in range(fun_start, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                    saw_open = True
                elif ch == "}":
                    depth -= 1
                    if saw_open and depth == 0:
                        fun_end = j
                        break
            if saw_open and depth == 0:
                fun_end = j
                break
        return "\n".join(lines[fun_start:fun_end + 1])
    return None


# ---- Composite rule evaluator (v16.2.1 — Phase B) -----------------------


@dataclass
class _RuleEvalContext:
    """Carrier for the inputs a rule evaluator needs."""
    diff_hunks: list[DiffHunk]
    patched_files: dict[str, str]


@dataclass
class _RuleResult:
    """Outcome of evaluating one VerificationRule.

    ``evidence_mode`` summarizes which kind of evidence let the rule pass
    (e.g. ``"direct_diff"`` for a ``diff_contains_pattern`` hit, or
    ``"diff_modified_payload_existing_sink"`` for an ``all_of`` that
    combines diff change + same-scope final-tree sink). Used by the
    verdict to surface non-trivial verification paths.
    """
    passed: bool
    evidence_mode: str = ""
    matched_file: str = ""
    notes: str = ""


def _file_matches_filter(file_path: str, file_filter: str) -> bool:
    """Cheap substring check — playbook authors use bare path fragments
    like ``CustomerSignup.kt`` rather than full globs."""
    if not file_filter:
        return True
    return file_filter in file_path


def _evaluate_rule(rule: VerificationRule, ctx: _RuleEvalContext) -> _RuleResult:
    """Recursively evaluate a VerificationRule against the diff + patched
    files. Returns a structured result so callers can surface what KIND
    of evidence let a contract pass (and where).
    """
    if rule.kind == "diff_contains_pattern":
        if not rule.pattern:
            return _RuleResult(passed=False, notes="empty pattern")
        for hunk in ctx.diff_hunks:
            if not _file_matches_filter(hunk.file_path, rule.file_filter):
                continue
            try:
                if re.search(rule.pattern, hunk.added_lines):
                    return _RuleResult(
                        passed=True,
                        evidence_mode="direct_diff",
                        matched_file=hunk.file_path,
                    )
            except re.error:
                continue
        return _RuleResult(passed=False, notes=f"no diff hunk added line matched {rule.pattern!r}")
    if rule.kind == "final_context_contains_pattern":
        if not rule.pattern:
            return _RuleResult(passed=False, notes="empty pattern")
        if not ctx.patched_files:
            return _RuleResult(
                passed=False,
                notes="patched_files not available; final_context_contains_pattern cannot be evaluated",
            )
        scope = rule.scope or "same_function"
        for hunk in ctx.diff_hunks:
            if not _file_matches_filter(hunk.file_path, rule.file_filter):
                continue
            patched_text = ctx.patched_files.get(hunk.file_path)
            if patched_text is None:
                continue
            scope_text = _resolve_scope(patched_text, hunk.new_start, scope)
            if scope_text is None:
                continue
            try:
                if re.search(rule.pattern, scope_text):
                    return _RuleResult(
                        passed=True,
                        evidence_mode="final_tree_context",
                        matched_file=hunk.file_path,
                    )
            except re.error:
                continue
        return _RuleResult(
            passed=False,
            notes=f"no scope ({scope}) anchored at a changed hunk matched {rule.pattern!r}",
        )
    if rule.kind == "any_of":
        if not rule.rules:
            return _RuleResult(passed=False, notes="any_of with no children")
        last_notes: list[str] = []
        for child in rule.rules:
            r = _evaluate_rule(child, ctx)
            if r.passed:
                return r
            if r.notes:
                last_notes.append(r.notes)
        return _RuleResult(passed=False, notes="; ".join(last_notes[:3]))
    if rule.kind == "all_of":
        if not rule.rules:
            return _RuleResult(passed=False, notes="all_of with no children")
        modes: list[str] = []
        matched_file = ""
        for child in rule.rules:
            r = _evaluate_rule(child, ctx)
            if not r.passed:
                return _RuleResult(
                    passed=False,
                    notes=f"all_of child failed: {r.notes}",
                )
            if r.evidence_mode and r.evidence_mode not in modes:
                modes.append(r.evidence_mode)
            if r.matched_file and not matched_file:
                matched_file = r.matched_file
        # Synthesize a richer evidence_mode for the canonical case:
        # diff added the payload + final tree shows the sink in the same scope.
        if "direct_diff" in modes and "final_tree_context" in modes:
            mode = "diff_modified_payload_existing_sink"
        else:
            mode = "+".join(modes) if modes else "all_of"
        return _RuleResult(passed=True, evidence_mode=mode, matched_file=matched_file)
    return _RuleResult(passed=False, notes=f"unknown rule kind: {rule.kind}")


def _evaluate_contract_rules(
    contract: RequiredContract,
    ctx: _RuleEvalContext,
) -> _RuleResult:
    """Walk a contract's top-level rules and return the first passing
    one (treating the list as an implicit OR — any rule confirming
    coverage is enough).

    A contract is verified if EITHER:
      - the legacy ``verification_patterns`` flat list hits the diff
        (handled by the caller for backwards compat), OR
      - any rule in ``verifications`` passes via this evaluator.
    """
    if not contract.verifications:
        return _RuleResult(passed=False, notes="no typed rules")
    # Treat top-level rules as implicit OR.
    last_notes: list[str] = []
    for rule in contract.verifications:
        r = _evaluate_rule(rule, ctx)
        if r.passed:
            return r
        if r.notes:
            last_notes.append(r.notes)
    return _RuleResult(passed=False, notes="; ".join(last_notes[:3]))


# ---- Legacy diff helpers (v16.2.0 verifier) -----------------------------


def _grep_any(text: str, patterns: list[str]) -> bool:
    """Return True if any of ``patterns`` (regex) matches ``text``.
    Case-sensitive — the playbook patterns are authored carefully."""
    if not text or not patterns:
        return False
    for p in patterns:
        if not p:
            continue
        try:
            if re.search(p, text):
                return True
        except re.error:
            # Bad regex in the playbook is a config error — fail closed
            # by NOT matching (verifier becomes conservative).
            continue
    return False


def _extract_added_lines(diff_text: str, file_filter: str | None = None) -> str:
    """Pull all ``+`` lines (excluding ``+++`` file headers) from a
    unified diff, optionally restricted to one file.

    Returned as one big string so the regex-grep can sweep across them.
    """
    if not diff_text:
        return ""
    out: list[str] = []
    in_target = file_filter is None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            if file_filter is not None:
                in_target = file_filter in line
            continue
        if in_target and line.startswith("+") and not line.startswith("+++"):
            out.append(line[1:])
    return "\n".join(out)


def verify_coverage(
    *,
    declaration: CoverageDeclaration,
    required: list[RequiredContract],
    diff_text: str,
    file_snapshots: dict[str, str] | None = None,
    patched_files: dict[str, str] | None = None,
) -> CoverageVerdict:
    """Run the harness-side verification.

    Arguments:
      declaration: the aggregated model coverage output across batches.
      required: the playbook's required contracts. When a contract
        carries typed ``verifications`` (v16.2.1+), the new diff-anchored
        evaluator is used; otherwise the legacy flat ``verification_patterns``
        path runs for backwards compatibility.
      diff_text: the merged final diff applied (or about to be applied).
      file_snapshots: pre-patch file contents, keyed by path. Used to
        verify ``verified_no_change_contracts`` claims (the contract
        must already be present in the actual file, not in the model's
        prose).
      patched_files: POST-patch file contents, keyed by path. Required
        for ``final_context_contains_pattern`` rules to evaluate (the
        new diff-anchored verifier needs to look at the patched tree to
        confirm an unchanged sink line sits in the same scope as a
        changed hunk). When None, ``final_context_contains_pattern``
        rules fail-soft (return false with a note), degrading the
        verifier to diff-only checks.

    Set equation rule:
      required ⊆ verified_implemented ∪ verified_no_change

    Failure verdicts (Phase A taxonomy):
      - ``unverified``: contract claimed but verifier couldn't confirm.
      - ``contradicted``: patched tree directly disproves the claim.
      - ``lie``: contract wholly absent from the artifact.
      - ``claims_unverified`` (legacy): emitted by the legacy verifier
        path when the typed rules are absent.
      - ``incomplete``: contract not addressed at all → route to
        plan_codegen_conflict.
    """
    required_ids = [r.contract_id for r in required if r.contract_id]
    if not required_ids:
        # No required contracts — the gate is a no-op (caller shouldn't
        # have invoked us, but be safe).
        return CoverageVerdict(
            ok=True,
            verdict_kind="complete",
            summary="No required contracts; coverage check skipped.",
        )

    by_id: dict[str, RequiredContract] = {r.contract_id: r for r in required if r.contract_id}
    file_snapshots = file_snapshots or {}
    patched_files_map = patched_files or {}

    # Pre-parse hunks once for the new diff-anchored evaluator. Cheap
    # enough to do unconditionally; the legacy path simply doesn't use it.
    parsed_hunks = _parse_diff_hunks(diff_text)
    rule_ctx = _RuleEvalContext(
        diff_hunks=parsed_hunks,
        patched_files=patched_files_map,
    )

    # Build per-contract claim look-up.
    implemented_claims: dict[str, list[CoverageClaim]] = {}
    nochange_claims: dict[str, list[CoverageClaim]] = {}
    unimplemented_claims: dict[str, list[CoverageClaim]] = {}
    for c in declaration.implemented_contracts:
        implemented_claims.setdefault(c.contract_id, []).append(c)
    for c in declaration.verified_no_change_contracts:
        nochange_claims.setdefault(c.contract_id, []).append(c)
    for c in declaration.unimplemented_contracts:
        unimplemented_claims.setdefault(c.contract_id, []).append(c)

    verified_implemented: list[str] = []
    verified_no_change: list[str] = []
    missing: list[str] = []
    lies: list[dict[str, str]] = []

    for cid in required_ids:
        contract = by_id[cid]
        # Order of preference: implemented > verified_no_change > unimplemented.
        # The reason: implemented means the model actively WROTE the
        # feature; verified_no_change means the feature was ALREADY
        # there (still satisfies the contract); unimplemented means the
        # contract is unmet.

        if cid in implemented_claims:
            # New (v16.2.1) diff-anchored evaluator runs when the
            # contract carries typed ``verifications`` rules. It supports
            # composite ``any_of`` / ``all_of`` and
            # ``final_context_contains_pattern``, which captures the
            # "diff modifies payload + existing sink consumes it" case.
            if contract.verifications:
                rule_result = _evaluate_contract_rules(contract, rule_ctx)
                if rule_result.passed:
                    # Forbidden-pattern negative check still applies
                    # (e.g. installed_library_only's blocklist of foreign
                    # SDK imports). Evaluate against all added lines.
                    if contract.forbidden_patterns:
                        all_added = "\n".join(h.added_lines for h in parsed_hunks)
                        if _grep_any(all_added, contract.forbidden_patterns):
                            lies.append({
                                "contract_id": cid,
                                "claim": "implemented",
                                "reason": (
                                    "diff contains forbidden patterns for this contract "
                                    f"({', '.join(contract.forbidden_patterns[:2])})"
                                ),
                                "severity": "contradicted",
                            })
                            continue
                    verified_implemented.append(cid)
                    continue
                # Rule evaluation failed → unverified (NOT a lie unless
                # we have positive evidence of contradiction, which the
                # current rule set doesn't surface).
                lies.append({
                    "contract_id": cid,
                    "claim": "implemented",
                    "reason": (
                        f"verifier could not confirm coverage: {rule_result.notes or 'no rule passed'}"
                    ),
                    "severity": "unverified",
                })
                continue

            # Legacy path (v16.2.0): flat verification_patterns. Verify
            # the contract's verification_patterns hit at least one added
            # line in the diff.
            target_file = next(
                (c.file_path for c in implemented_claims[cid] if c.file_path),
                None,
            )
            added_text = _extract_added_lines(diff_text, file_filter=target_file)
            # Also try a global sweep if a file-specific scan misses; the
            # contract might span multiple files and the claim's file_path
            # might be one of several.
            if not _grep_any(added_text, contract.verification_patterns):
                if target_file:
                    added_text = _extract_added_lines(diff_text)
            if _grep_any(added_text, contract.verification_patterns):
                # Also check forbidden_patterns weren't added.
                if contract.forbidden_patterns and _grep_any(
                    added_text, contract.forbidden_patterns
                ):
                    lies.append({
                        "contract_id": cid,
                        "claim": "implemented",
                        "reason": (
                            "diff contains forbidden patterns for this contract "
                            f"({', '.join(contract.forbidden_patterns[:2])})"
                        ),
                    })
                    continue
                verified_implemented.append(cid)
                continue
            lies.append({
                "contract_id": cid,
                "claim": "implemented",
                "reason": (
                    "no added line in diff matches any verification pattern "
                    f"({', '.join(contract.verification_patterns[:2])})"
                ),
            })
            continue

        if cid in nochange_claims:
            # Verify the patterns hit the pre-existing file content
            # (file_snapshots), not the diff.
            matched_in_any_file = False
            for claim in nochange_claims[cid]:
                fp = claim.file_path
                if fp and fp in file_snapshots:
                    if _grep_any(file_snapshots[fp], contract.verification_patterns):
                        matched_in_any_file = True
                        break
                else:
                    # Claim didn't pin a file — sweep all snapshots.
                    for content in file_snapshots.values():
                        if _grep_any(content, contract.verification_patterns):
                            matched_in_any_file = True
                            break
                    if matched_in_any_file:
                        break
            if matched_in_any_file:
                verified_no_change.append(cid)
                continue
            lies.append({
                "contract_id": cid,
                "claim": "verified_no_change",
                "reason": (
                    "no file snapshot contains a line matching any "
                    "verification pattern; the model's no-change claim "
                    "is unverifiable"
                ),
            })
            continue

        if cid in unimplemented_claims:
            missing.append(cid)
            continue

        # Not mentioned anywhere — also missing.
        missing.append(cid)

    if lies:
        # Promote verdict kind based on the strongest severity present.
        # ``contradicted`` > ``lie`` > ``unverified``; legacy entries
        # without severity → ``claims_unverified`` (v16.2.0 behavior).
        severities = {entry.get("severity", "") for entry in lies}
        if "contradicted" in severities:
            verdict_kind = "contradicted"
            summary_prefix = (
                f"Model claimed coverage for {len(lies)} contract(s) but the "
                "patched tree directly contradicts the claim"
            )
        elif "lie" in severities:
            verdict_kind = "lie"
            summary_prefix = (
                f"Model claimed coverage for {len(lies)} contract(s) but the "
                "artifact contains no supporting evidence"
            )
        elif "unverified" in severities:
            verdict_kind = "unverified"
            summary_prefix = (
                f"Model claimed coverage for {len(lies)} contract(s) but the "
                "verifier could not confirm the claim against the diff/patched tree"
            )
        else:
            # Legacy verifier output (verification_patterns path).
            verdict_kind = "claims_unverified"
            summary_prefix = (
                f"Model claimed coverage for {len(lies)} contract(s) the "
                "harness could not verify against the actual artifact"
            )
        ok = False
        summary = (
            f"{summary_prefix}: "
            f"{', '.join(l['contract_id'] for l in lies[:5])}."
        )
    elif missing:
        verdict_kind = "incomplete"
        ok = False
        summary = (
            f"{len(missing)} of {len(required_ids)} required contract(s) "
            f"not implemented or verified: "
            f"{', '.join(missing[:5])}. Patch covers "
            f"{len(verified_implemented) + len(verified_no_change)} of "
            f"{len(required_ids)} contracts."
        )
    else:
        verdict_kind = "complete"
        ok = True
        summary = (
            f"All {len(required_ids)} required contract(s) covered: "
            f"{len(verified_implemented)} implemented, "
            f"{len(verified_no_change)} verified no-change."
        )

    return CoverageVerdict(
        ok=ok,
        verdict_kind=verdict_kind,
        verified_implemented=verified_implemented,
        verified_no_change=verified_no_change,
        missing=missing,
        lies=lies,
        summary=summary,
    )


# ---- Playbook → required_contracts helper -------------------------------


def _rule_from_dict(node: Any) -> VerificationRule | None:
    """Build a typed VerificationRule tree from a playbook node.

    Recognized shapes (Phase A — schema only; evaluator lands in Phase B):

      ``{kind: "diff_contains_pattern", pattern: "<regex>", file_filter?: "..."}``
        → leaf; matches any + line in the diff (optionally scoped to file)

      ``{kind: "final_context_contains_pattern", pattern: "<regex>",
         anchor: "changed_hunk", scope: "same_function" | "window:N",
         file_filter?: "..."}``
        → leaf; matches in the patched file within a scope anchored to a
        changed hunk

      ``{kind: "any_of", rules: [...]}`` — passes if ANY child passes
      ``{kind: "all_of", rules: [...]}`` — passes if ALL children pass

    Legacy form is also accepted: a dict with ``pattern`` but no
    ``kind`` is treated as ``diff_contains_pattern``.

    Unknown ``kind`` values return None (caller filters them out).
    """
    if not isinstance(node, dict):
        return None
    kind = str(node.get("kind") or "").strip()
    pattern = str(node.get("pattern") or "")
    # Legacy fallback: bare {pattern: "..."} → diff_contains_pattern.
    if not kind and pattern:
        kind = "diff_contains_pattern"
    if kind not in (
        "diff_contains_pattern",
        "final_context_contains_pattern",
        "any_of",
        "all_of",
    ):
        return None
    rules: list[VerificationRule] = []
    if kind in ("any_of", "all_of"):
        raw_children = node.get("rules") or []
        if isinstance(raw_children, list):
            for child in raw_children:
                built = _rule_from_dict(child)
                if built is not None:
                    rules.append(built)
        if not rules:
            return None
    return VerificationRule(
        kind=kind,  # type: ignore[arg-type]
        pattern=pattern,
        rules=rules,
        anchor=str(node.get("anchor") or "").strip(),
        scope=str(node.get("scope") or "").strip(),
        file_filter=str(node.get("file_filter") or "").strip(),
    )


def _flatten_top_level_diff_patterns(rules: list[VerificationRule]) -> list[str]:
    """Collect ``diff_contains_pattern`` strings from anywhere in the
    rule tree — used to populate the legacy ``verification_patterns``
    list for backwards compatibility with the v16.2.0 verifier paths
    that still scan against a flat pattern list (notably the
    ``verified_no_change`` branch of ``verify_coverage``).

    Semantics: we descend through ``any_of`` and ``all_of`` to pick up
    every ``diff_contains_pattern`` leaf, then return the deduplicated
    list. ``final_context_contains_pattern`` leaves are intentionally
    skipped — the legacy verifier has no notion of "patched-tree scope",
    and including those patterns would cause false positives when the
    legacy verifier greps them against the diff alone.

    The result is fingerprint-quality only: it tells the legacy verifier
    "if any of these regex hits the file, the contract is plausibly
    present". The Phase B verifier consumes ``verifications`` directly
    and honors the full any_of / all_of structure.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _walk(rule: VerificationRule) -> None:
        if rule.kind == "diff_contains_pattern" and rule.pattern:
            if rule.pattern not in seen:
                seen.add(rule.pattern)
                out.append(rule.pattern)
            return
        if rule.kind in ("any_of", "all_of"):
            for child in rule.rules:
                _walk(child)
            return
        # final_context_contains_pattern and unknown kinds: skip.

    for r in rules:
        _walk(r)
    return out


def required_contracts_from_playbook(
    playbook: dict[str, Any] | None,
) -> list[RequiredContract]:
    """Convert a domain playbook's `required_contracts` array into the
    typed RequiredContract list used by the verifier. Returns [] when
    no playbook or empty.

    Builds both the legacy flat ``verification_patterns`` (consumed by
    the v16.2.0 verifier) and the new typed ``verifications`` tree
    (consumed by the Phase B verifier). Old playbooks with bare
    ``{pattern: "..."}`` entries are accepted unchanged; new playbooks
    can use composite ``any_of`` / ``all_of`` and
    ``final_context_contains_pattern`` nodes.
    """
    if not isinstance(playbook, dict):
        return []
    raw = playbook.get("required_contracts") or []
    if not isinstance(raw, list):
        return []
    out: list[RequiredContract] = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        signal = str(c.get("signal") or "").strip()
        # Typed rule tree (Phase A).
        verifications_raw = c.get("verifications") or []
        verifications: list[VerificationRule] = []
        if isinstance(verifications_raw, list):
            for v in verifications_raw:
                built = _rule_from_dict(v)
                if built is not None:
                    verifications.append(built)
        # Legacy flat patterns derived from the typed tree (top-level
        # diff_contains_pattern only; see _flatten_top_level_diff_patterns).
        patterns = _flatten_top_level_diff_patterns(verifications)
        forbidden = c.get("forbidden_patterns") or []
        forbidden_list: list[str] = [
            p for p in forbidden if isinstance(p, str)
        ] if isinstance(forbidden, list) else []
        out.append(RequiredContract(
            contract_id=cid,
            signal=signal,
            verification_patterns=patterns,
            forbidden_patterns=forbidden_list,
            verifications=verifications,
        ))
    return out
