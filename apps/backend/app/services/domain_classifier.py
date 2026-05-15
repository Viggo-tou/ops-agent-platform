"""Domain classifier + playbook loader (v16.1).

Given a Jira-style request, identify which domain (if any) the task
falls under, then load that domain's playbook. The playbook carries
the `required_contracts` list that downstream code uses to:

  - Inject contract requirements into the planner prompt so the planner
    knows what acceptance_tests to emit.
  - Run the empty-acceptance gate (C1 mitigation) — if the planner
    didn't emit acceptance_tests covering at least
    `playbook.completeness_rule.minimum_contracts_referenced`
    contracts, codegen is blocked and the planner is retried once
    before failing with PLAN_UNDER_SPECIFIED.

This is the FIRST piece of contract-first codegen. Scope deliberately
small: keyword + project-tag matching, one hand-authored playbook
(Android map/location). Once a second domain ships (Firebase auth or
data-pipeline are the obvious next candidates), the classifier may
need an LLM upgrade — for now, exact-substring matching against
`triggers.summary_keywords` and `triggers.project_tags` is sufficient.

Failure classes addressed:
  - C1 (plan_under_specified): the playbook supplies the minimum
    contract count the planner must reference.
  - C4 (library_api_hallucination): `forbidden_patterns` on
    `installed_library_only` contract feeds the import-dependency gate.

Not yet addressed:
  - C2 (polish_passing_as_feature): the harness still needs to verify
    the diff against the playbook's verifications. That's v16.2 work
    (Contract Coverage Output from codegen).
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_logger = logging.getLogger("domain_classifier")

_DEFAULT_PLAYBOOKS_DIR = (
    Path(__file__).resolve().parents[2] / "data" / "domain_playbooks"
)


@lru_cache(maxsize=32)
def _load_playbook_file(path: str) -> dict[str, Any]:
    """Parse one playbook YAML. Cached because playbooks are immutable
    for the process lifetime and we re-load per task."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        _logger.warning("playbook %s failed to load: %s", path, exc)
        return {}


def all_playbooks(*, playbooks_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load every YAML in the playbooks dir. Used by the classifier and
    by tests."""
    base = playbooks_dir or _DEFAULT_PLAYBOOKS_DIR
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.yaml")):
        pb = _load_playbook_file(str(path))
        if pb:
            out.append(pb)
    return out


def _normalize_text(text: str) -> str:
    return (text or "").strip().lower()


def _matches_summary_keywords(
    request_text: str, keywords: list[str]
) -> tuple[bool, list[str]]:
    """Returns (matched, list_of_matched_keywords). Keyword matching is
    case-insensitive substring; multi-word keywords (e.g. "google maps")
    match literally."""
    if not request_text or not keywords:
        return False, []
    text = _normalize_text(request_text)
    matched: list[str] = []
    for kw in keywords:
        if not isinstance(kw, str):
            continue
        kw_lower = _normalize_text(kw)
        if not kw_lower:
            continue
        # Use word-boundary-ish matching for short alpha keywords;
        # substring matching for phrases. This keeps "map" from matching
        # "compare" while still letting "google maps" match the obvious
        # phrase.
        if " " in kw_lower or "-" in kw_lower or len(kw_lower) > 12:
            if kw_lower in text:
                matched.append(kw)
        else:
            if re.search(r"\b" + re.escape(kw_lower) + r"\b", text):
                matched.append(kw)
    return (len(matched) > 0), matched


def _matches_project_tag(project_tag: str, tags: list[str]) -> bool:
    if not project_tag or not tags:
        return False
    pt = _normalize_text(project_tag)
    return any(_normalize_text(t) == pt for t in tags)


def classify_domain(
    *,
    request_text: str,
    project_tag: str | None = None,
    playbooks_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Return the first playbook whose triggers match this request.

    Match rule for v1: a playbook matches if EITHER (a) any of its
    `triggers.summary_keywords` appears in request_text OR (b) any of
    its `triggers.project_tags` equals project_tag. Both signals
    together is stronger but either alone is enough — letting the
    keyword match fire on cross-project tasks (e.g. a non-handymanapp
    Android task that talks about map UI still gets the Android
    map/location playbook).

    Returns the full playbook dict, or None if no playbook matches.
    Caller is expected to read fields like `required_contracts` and
    `completeness_rule`.
    """
    for pb in all_playbooks(playbooks_dir=playbooks_dir):
        triggers = pb.get("triggers") or {}
        summary_kw = triggers.get("summary_keywords") or []
        project_tags = triggers.get("project_tags") or []
        kw_match, _matched_kws = _matches_summary_keywords(
            request_text, summary_kw if isinstance(summary_kw, list) else []
        )
        tag_match = _matches_project_tag(
            project_tag or "",
            project_tags if isinstance(project_tags, list) else [],
        )
        if kw_match:
            return pb
    return None


def classify_android_map_location_subtype(issue_text: str) -> str:
    """Classify the map/location family into the narrow workflow shape."""
    text = _normalize_text(issue_text)
    if _looks_like_job_default_address_issue(text):
        return "job_default_address"
    if _looks_like_signup_address_issue(text):
        return "signup_address_map"
    return "generic_map_location"


# ---- Feature-task detection ----------------------------------------------


# Verbs that indicate the user is asking for a feature ADD/IMPLEMENT,
# as opposed to a bugfix / refactor / polish. The empty-acceptance gate
# only fires on feature tasks because polish tasks legitimately have
# tiny diffs and acceptance_tests aren't expected.
_FEATURE_VERBS_EN = (
    r"implement", r"add", r"build", r"create", r"introduce", r"enable",
    r"support", r"integrate", r"wire(?:\s+up|)", r"finish", r"ship",
    r"complete", r"develop",
)
_FEATURE_VERBS_ZH = ("实现", "新增", "添加", "加入", "完成", "开发", "实施", "支持")

_FEATURE_VERB_RE_EN = re.compile(
    r"\b(" + r"|".join(_FEATURE_VERBS_EN) + r")\b", re.IGNORECASE
)


def is_feature_task(request_text: str) -> bool:
    """Heuristic: does this request describe adding/implementing
    functionality? Used to decide whether the empty-acceptance gate
    should fire (only feature tasks need substantive acceptance_tests
    — pure refactors and polish tasks legitimately don't)."""
    if not request_text:
        return False
    if _FEATURE_VERB_RE_EN.search(request_text):
        return True
    return any(v in request_text for v in _FEATURE_VERBS_ZH)


# ---- Completeness gate ---------------------------------------------------


def evaluate_acceptance_completeness(
    *,
    playbook: dict[str, Any] | None,
    acceptance_tests: list[Any],
) -> dict[str, Any]:
    """Decide whether the planner's `acceptance_tests` array references
    enough of the playbook's `required_contracts`.

    Returns a dict with:
      - ok: bool — gate passed (True) or task should be blocked (False)
      - reason: short human-readable summary
      - matched_contracts: list[str] — contract IDs the planner covered
      - missing_contracts: list[str] — contract IDs not covered
      - minimum_required: int — playbook's threshold
      - retry_planner_once: bool — caller should retry planner before
        hard-failing
      - on_failure: str — failure verdict label (e.g.
        PLAN_UNDER_SPECIFIED) for event payload

    When `playbook is None` (no domain matched), the gate is a no-op:
    we don't have a contract baseline so we can't measure completeness.
    Returns ok=True with reason='no_domain_playbook'.
    """
    if playbook is None:
        return {
            "ok": True,
            "reason": "no_domain_playbook",
            "matched_contracts": [],
            "missing_contracts": [],
            "minimum_required": 0,
            "retry_planner_once": False,
            "on_failure": "",
        }
    required = playbook.get("required_contracts") or []
    rule = playbook.get("completeness_rule") or {}
    minimum = int(rule.get("minimum_contracts_referenced") or 0)
    retry_once = bool(rule.get("retry_planner_once"))
    on_failure_label = str(
        rule.get("on_failure") or "PLAN_UNDER_SPECIFIED"
    )

    if not required or minimum <= 0:
        return {
            "ok": True,
            "reason": "playbook_has_no_completeness_rule",
            "matched_contracts": [],
            "missing_contracts": [],
            "minimum_required": minimum,
            "retry_planner_once": retry_once,
            "on_failure": on_failure_label,
        }

    # Build the set of contract IDs the planner referenced. A contract
    # is "referenced" if any acceptance_test's pattern field matches any
    # of the contract's verifications.patterns, OR the test explicitly
    # carries a `contract_id` field equal to a required contract id.
    matched: set[str] = set()
    for c in required:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not cid:
            continue
        verifications = c.get("verifications") or []
        c_patterns: list[str] = []
        for v in verifications:
            if isinstance(v, dict) and isinstance(v.get("pattern"), str):
                c_patterns.append(v["pattern"])
        for t in acceptance_tests or []:
            if not isinstance(t, dict):
                continue
            if t.get("contract_id") == cid:
                matched.add(cid)
                break
            pat = t.get("pattern")
            if isinstance(pat, str):
                # We don't run regex-vs-regex (intractable). Instead we
                # check whether the planner-emitted pattern contains
                # tokens from any of the contract's patterns. Reasonable
                # heuristic for v1: a planner pattern "MapView|SupportMapFragment"
                # contains "MapView", which appears in our contract's
                # pattern → match.
                tokens_in_pat = _extract_alternation_tokens(pat)
                contract_tokens: set[str] = set()
                for cp in c_patterns:
                    contract_tokens.update(_extract_alternation_tokens(cp))
                if tokens_in_pat & contract_tokens:
                    matched.add(cid)
                    break

    required_ids = [
        str(c.get("id") or "")
        for c in required if isinstance(c, dict) and c.get("id")
    ]
    missing = [cid for cid in required_ids if cid and cid not in matched]
    ok = len(matched) >= minimum
    return {
        "ok": ok,
        "reason": (
            f"matched {len(matched)} of {len(required_ids)} contracts "
            f"(minimum {minimum})"
        ),
        "matched_contracts": sorted(matched),
        "missing_contracts": missing,
        "minimum_required": minimum,
        "retry_planner_once": retry_once,
        "on_failure": on_failure_label,
    }


def _extract_alternation_tokens(pattern: str) -> set[str]:
    """Pull literal identifier tokens out of a regex alternation. E.g.
    `MapView|SupportMapFragment` → {MapView, SupportMapFragment}. Drops
    regex metacharacters; this is heuristic by design — the goal is
    "does the planner's test mention the same NAMES the contract cares
    about", not "do the regexes match". For verifications that use
    heavy regex syntax, the token extraction degrades to whatever
    identifier-shaped tokens remain, which is still better than
    nothing for the v1 completeness check.
    """
    if not pattern or not isinstance(pattern, str):
        return set()
    # Normalize: split on | (regex alternation) and strip parens, escapes.
    parts = re.split(r"\|", pattern)
    out: set[str] = set()
    for part in parts:
        # Strip common regex syntax to get at literal tokens.
        cleaned = re.sub(r"[\\(){}\[\].*+?^$]", " ", part).strip()
        for tok in cleaned.split():
            if re.match(r"[A-Za-z_][A-Za-z0-9_]{2,}$", tok):
                out.add(tok)
    return out


_SUPPORTED_SYNTHETIC_ACCEPTANCE_KINDS = {
    "diff_contains_pattern",
    "diff_contains_pattern_in_file",
    "import_added",
    "forbids_pattern_in_diff",
}


def synthesize_acceptance_tests_from_playbook(
    *,
    playbook: dict[str, Any] | None,
    acceptance_tests: list[Any] | None,
    max_tests: int = 10,
) -> list[dict[str, Any]]:
    """Backfill missing domain acceptance tests from the matched playbook.

    This is a deterministic harness repair for a weak planner response:
    the model still owns the implementation plan, but playbook contracts
    are already authoritative, so an empty acceptance_tests array should
    not block codegen when the harness can express the same contracts.
    """
    existing = [
        _coerce_acceptance_test_dict(item)
        for item in (acceptance_tests or [])
    ]
    existing = [item for item in existing if item]
    if not playbook:
        return existing[:max_tests]

    verdict = evaluate_acceptance_completeness(
        playbook=playbook,
        acceptance_tests=existing,
    )
    if verdict.get("ok"):
        return existing[:max_tests]

    minimum = int(verdict.get("minimum_required") or 0)
    matched = set(verdict.get("matched_contracts") or [])
    if minimum <= 0:
        return existing[:max_tests]

    out = list(existing[:max_tests])
    remaining_slots = max(max_tests - len(out), 0)
    for contract in playbook.get("required_contracts") or []:
        if len(matched) >= minimum or remaining_slots <= 0:
            break
        if not isinstance(contract, dict):
            continue
        contract_id = str(contract.get("id") or "").strip()
        if not contract_id or contract_id in matched:
            continue
        verification = _first_synthetic_verification(contract)
        if not verification:
            continue
        pattern = str(verification.get("pattern") or "").strip()
        kind = str(verification.get("kind") or "diff_contains_pattern").strip()
        if not pattern or kind not in _SUPPORTED_SYNTHETIC_ACCEPTANCE_KINDS:
            continue
        test: dict[str, Any] = {
            "kind": kind,
            "pattern": pattern,
            "contract_id": contract_id,
            "rationale": (
                f"Backfilled from domain playbook contract {contract_id}."
            ),
        }
        file_filter = str(
            verification.get("file") or verification.get("file_filter") or ""
        ).strip()
        if file_filter and kind == "diff_contains_pattern":
            # The contract verifier can use file_filter while still treating
            # this as a diff rule. The later acceptance_check has a stronger
            # final-context path for in-file tests, which is what we want for
            # contracts whose evidence may already exist in a touched file.
            test["kind"] = "diff_contains_pattern_in_file"
            test["file"] = file_filter
        elif kind == "diff_contains_pattern_in_file" and file_filter:
            test["file"] = file_filter
        out.append(test)
        matched.add(contract_id)
        remaining_slots -= 1
    return out[:max_tests]


def _coerce_acceptance_test_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        out = dict(item)
    elif hasattr(item, "model_dump"):
        try:
            dumped = item.model_dump(mode="json")
            out = dict(dumped) if isinstance(dumped, dict) else {}
        except Exception:  # noqa: BLE001
            out = {}
    else:
        out = {}
    if out.get("pattern") and not out.get("kind"):
        out["kind"] = "diff_contains_pattern"
    return out


def _iter_verification_rules(values: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(values, list):
        return out
    for value in values:
        if not isinstance(value, dict):
            continue
        out.append(value)
        nested = value.get("rules")
        if isinstance(nested, list):
            out.extend(_iter_verification_rules(nested))
    return out


def _first_synthetic_verification(contract: dict[str, Any]) -> dict[str, Any] | None:
    for rule in _iter_verification_rules(contract.get("verifications") or []):
        kind = str(rule.get("kind") or "").strip()
        pattern = str(rule.get("pattern") or "").strip()
        if kind in _SUPPORTED_SYNTHETIC_ACCEPTANCE_KINDS and pattern:
            return rule
    return None


_SOURCE_FILE_SUFFIXES = (
    ".kt",
    ".kts",
    ".java",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
)


def synthesize_must_touch_files_from_candidates(
    *,
    playbook: dict[str, Any] | None,
    candidate_files: list[dict[str, Any]] | None,
    issue_text: str,
    existing_must_touch: list[str] | None,
    expected_new_files: list[str] | None,
    max_files: int = 4,
) -> list[str]:
    """Promote evidence-backed candidate files when the planner emits no
    concrete edit targets.

    The function never invents paths: it can only return files that came
    from preplan evidence. Domain-specific ordering is intentionally tiny
    and limited to the known Android map/location signup-address class.
    """
    existing = _unique_paths(existing_must_touch or [])
    if existing or expected_new_files or not playbook or not candidate_files:
        return existing[:max_files]

    filtered: list[tuple[dict[str, Any], str, float, int]] = []
    for index, cand in enumerate(candidate_files):
        if not isinstance(cand, dict):
            continue
        path = _normalize_candidate_path(cand.get("path") or cand.get("file"))
        if not _is_source_candidate_path(path):
            continue
        try:
            score = float(cand.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        filtered.append((cand, path, score, index))
    if not filtered:
        return []

    domain_id = str(playbook.get("id") or "").strip()
    if domain_id in {"android_job_default_address", "android_map_location"} and _is_job_default_address_issue(issue_text):
        preferred = [
            (cand, path, score, index)
            for cand, path, score, index in filtered
            if _is_job_default_address_path(path)
        ]
        if preferred:
            preferred.sort(
                key=lambda item: (
                    _job_default_address_path_rank(item[1]),
                    -item[2],
                    item[3],
                )
            )
            return _unique_paths([path for _cand, path, _score, _index in preferred])[
                :max_files
            ]

    if domain_id == "android_map_location" and _is_signup_address_issue(issue_text):
        preferred = [
            (cand, path, score, index)
            for cand, path, score, index in filtered
            if _is_signup_address_path(path)
        ]
        if preferred:
            preferred.sort(
                key=lambda item: (
                    _signup_address_path_rank(item[1]),
                    -item[2],
                    item[3],
                )
            )
            return _unique_paths([path for _cand, path, _score, _index in preferred])[
                :max_files
            ]

    issue_terms = _issue_terms(issue_text)
    scored: list[tuple[float, int, str]] = []
    for cand, path, score, index in filtered:
        haystack = " ".join(
            [
                path.lower(),
                str(cand.get("reason") or "").lower(),
                " ".join(str(t).lower() for t in cand.get("matched_terms") or []),
            ]
        )
        overlap = sum(1 for term in issue_terms if term in haystack)
        scored.append((score + (overlap * 2.0), index, path))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return _unique_paths([path for _score, _index, path in scored])[:max_files]


def _normalize_candidate_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def _unique_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_candidate_path(path)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _is_source_candidate_path(path: str) -> bool:
    lower = path.lower()
    if not lower.endswith(_SOURCE_FILE_SUFFIXES):
        return False
    blocked = (
        "/test/",
        "/tests/",
        "/androidtest/",
        "/build/",
        "/dist/",
        "/node_modules/",
    )
    return not any(marker in lower for marker in blocked)


def _is_signup_address_issue(issue_text: str) -> bool:
    text = _normalize_text(issue_text)
    return _looks_like_signup_address_issue(text)


def _is_job_default_address_issue(issue_text: str) -> bool:
    text = _normalize_text(issue_text)
    return _looks_like_job_default_address_issue(text)


def _looks_like_job_default_address_issue(text: str) -> bool:
    if "address" not in text:
        return False
    job_hints = (
        "job",
        "job location",
        "work location",
        "creating jobs",
        "create jobs",
        "new job",
    )
    default_hints = (
        "default",
        "pre-fill",
        "prefill",
        "saved home address",
        "home address",
        "saved account address",
        "saved address",
    )
    return any(hint in text for hint in job_hints) and any(
        hint in text for hint in default_hints
    )


def _looks_like_signup_address_issue(text: str) -> bool:
    if _looks_like_job_default_address_issue(text):
        return False
    if "address" not in text:
        return False
    signup_hints = (
        "signup",
        "sign up",
        "account signup",
        "account creation",
        "creation flow",
        "kyc",
    )
    return any(hint in text for hint in signup_hints)


def _is_job_default_address_path(path: str) -> bool:
    name = Path(path).name.lower()
    return name in {
        "jobpostingfragment.kt",
        "jobpostingflow.kt",
    }


def _job_default_address_path_rank(path: str) -> int:
    lower = path.lower()
    ordered = (
        "jobpostingfragment.kt",
        "jobpostingflow.kt",
    )
    for index, marker in enumerate(ordered):
        if lower.endswith(marker):
            return index
    return 99


def _is_signup_address_path(path: str) -> bool:
    name = Path(path).name.lower()
    return name in {
        "customersignup.kt",
        "customerkycaddressform.kt",
        "handymansignup.kt",
        "handymankycaddressform.kt",
    } or bool(re.search(r"(signup|kycaddressform)\.kt$", name))


def _signup_address_path_rank(path: str) -> int:
    lower = path.lower()
    ordered = (
        "customer_pages/customersignup.kt",
        "customer_pages/customerkycaddressform.kt",
        "handyman_pages/handymansignup.kt",
        "handyman_pages/handymankycaddressform.kt",
    )
    for index, marker in enumerate(ordered):
        if marker in lower:
            return index
    if "signup" in lower:
        return 10
    if "kycaddressform" in lower:
        return 11
    return 99


def _issue_terms(issue_text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "can",
        "also",
        "using",
        "flow",
    }
    return {
        token
        for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_]{2,}", issue_text.lower())
        if token not in stop
    }


def format_playbook_for_planner_prompt(playbook: dict[str, Any]) -> str:
    """Render the playbook's `required_contracts` as a planner-prompt
    block that tells the planner what acceptance_tests to emit. Acts as
    the load-bearing signal in the C1 mitigation: the planner sees the
    contract signals BEFORE producing acceptance_tests, so the result
    isn't a guess about what tests to write.
    """
    if not playbook:
        return ""
    domain_id = playbook.get("id") or "(unknown domain)"
    required = playbook.get("required_contracts") or []
    if not required:
        return ""
    rule = playbook.get("completeness_rule") or {}
    minimum = int(rule.get("minimum_contracts_referenced") or 0)

    out: list[str] = []
    out.append(
        f"## DOMAIN PLAYBOOK MATCHED: {domain_id}"
    )
    out.append(
        "The user's request matches a known domain. Your `acceptance_tests` "
        f"output MUST cover at least {minimum} of the contracts below. "
        "Each acceptance_test entry should carry a `contract_id` field "
        "matching one of the listed ids, AND a `pattern` field that an "
        "automated post-gate can grep for in added diff lines."
    )
    out.append("")
    out.append("Required contracts:")
    for c in required:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or "?"
        signal = (c.get("signal") or "").strip().replace("\n", " ")
        verifications = c.get("verifications") or []
        first_pattern = None
        for v in verifications:
            if isinstance(v, dict) and isinstance(v.get("pattern"), str):
                first_pattern = v["pattern"]
                break
        out.append(f"  - id: {cid}")
        out.append(f"    signal: {signal[:280]}")
        if first_pattern:
            out.append(f"    example_pattern: `{first_pattern}`")
    out.append("")
    out.append(
        "If the user's request genuinely does NOT need one of these "
        "contracts (e.g. the request is to fix only the geocoder, not "
        "to add a map UI), set `acceptance_tests` to cover the relevant "
        "ones and explicitly note in `assumptions` that the excluded "
        "ones are out of scope. An empty `acceptance_tests` array is "
        "NOT acceptable for a feature-add request."
    )
    return "\n".join(out)
