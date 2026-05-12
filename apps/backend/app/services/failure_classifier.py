"""T-LEARNING-LOOP-V1 (2026-05-12) — failure classifier.

Rule-based classification of terminal pipeline failures into named
``failure_class`` labels suitable for ``agent_memory.failure_observation``
rows. Pure functions, no DB / network dependencies — easy to test, easy
to call from the orchestrator's ``pipeline.failed`` hook.

Design:

- One classifier per failure family. Each returns a typed
  ``FailureClassification`` (or ``None`` if the family doesn't fit).
- ``classify(...)`` is the dispatcher that runs every classifier in
  priority order and returns the first match. The orchestrator calls
  this with the available signals (gate verdict dict, compile errors,
  diff stats, provider info); each classifier ignores signals it
  doesn't recognize.
- ``failure_class`` names are the contract surface for the memory
  retrieval layer — adding a new class requires both a classifier
  function here and a documented retrieval scope.

Hard rule: classifiers record observed facts only. They never produce
``resolution`` text claiming "the correct fix is X". The lesson field
is for plain-English description of what went wrong, written so the
downstream planner / codegen reads it as a *warning* not a *recipe*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---- Output dataclass ----------------------------------------------------


@dataclass
class FailureClassification:
    """Structured output of the classifier dispatcher.

    The orchestrator passes this into ``MemoryService.write_failure_observation``
    which renders it into an ``agent_memory`` row tagged
    ``memory_kind='failure_observation'``.
    """
    failure_class: str
    scope: str
    task_family: str | None = None
    lesson: str = ""
    evidence_refs: dict[str, Any] = field(default_factory=dict)
    # Whitelist of prompt-section contexts this row is allowed to be
    # consumed by. Defaults to planner_warning so a downstream codegen
    # never sees a failure observation unless a follow-up classifier
    # explicitly opts in.
    prompt_eligible: list[str] = field(default_factory=lambda: ["planner_warning"])
    # Trust ladder. Auto-rule classifiers default to ``auto_classified``;
    # a human reviewer can elevate to ``human_confirmed`` later.
    trust_level: str = "auto_classified"


# ---- Individual classifiers ---------------------------------------------


def classify_must_touch_incomplete_diff(
    *,
    plan_must_touch: list[str],
    files_actually_patched: list[str],
    pipeline_failed_message: str = "",
    provider: str | None = None,
    task_family: str | None = None,
    task_id: str | None = None,
) -> FailureClassification | None:
    """Fired when ``must_touch enforcement`` gate rejects a diff that
    only covers a subset of the planner-declared must_touch files.

    Signals:
      - ``plan_must_touch`` non-empty
      - ``files_actually_patched`` proper subset of ``plan_must_touch``
      - OR the pipeline message string explicitly mentions
        "must_touch file(s) were not successfully patched"
    """
    must_touch = {f for f in (plan_must_touch or []) if f}
    patched = {f for f in (files_actually_patched or []) if f}
    missing = sorted(must_touch - patched)
    msg = (pipeline_failed_message or "").lower()
    explicit = (
        "must_touch" in msg
        and ("were not successfully patched" in msg or "missing" in msg)
    )
    if not (missing or explicit):
        return None
    return FailureClassification(
        failure_class="must_touch_incomplete_diff",
        scope="gate:must_touch",
        task_family=task_family,
        lesson=(
            "On this task_family the planner declared "
            f"{len(must_touch)} must_touch file(s) but codegen only "
            f"patched {len(patched)}. Missing: "
            f"{', '.join(missing[:5]) if missing else 'see pipeline message'}. "
            "Future planning must enumerate every file the feature "
            "touches (UI + state + persistence + tests); future codegen "
            "must validate patched_files ⊇ plan.must_touch before "
            "returning a diff."
        ),
        evidence_refs={
            "task_id": task_id,
            "provider": provider,
            "plan_must_touch": list(must_touch),
            "files_actually_patched": list(patched),
            "missing_files": missing,
            "pipeline_message": pipeline_failed_message[:400],
        },
        prompt_eligible=["planner_warning", "codegen_warning"],
    )


def classify_contract_coverage_failure(
    *,
    coverage_verdict: dict[str, Any] | None,
    provider: str | None = None,
    task_family: str | None = None,
    task_id: str | None = None,
) -> FailureClassification | None:
    """Fired when ``contract_coverage.check`` returns a non-complete
    verdict. The v16.2.1 verifier emits three severities:
    ``unverified`` / ``contradicted`` / ``lie`` (plus legacy
    ``claims_unverified``). We record each as its own failure_class so
    the retrieval layer can surface "you were here before" specifically.
    """
    if not isinstance(coverage_verdict, dict):
        return None
    kind = str(coverage_verdict.get("verdict_kind") or "").strip()
    if kind in ("complete", "", "incomplete"):
        # incomplete is not a coverage *failure* — it's a coverage gap
        # that already routes to plan_codegen_conflict approval. Don't
        # write a failure_observation for it (the human approver is
        # already in the loop).
        return None
    label_map = {
        "unverified": "contract_coverage_unverified",
        "contradicted": "contract_coverage_contradicted",
        "lie": "contract_coverage_lie",
        "claims_unverified": "contract_coverage_legacy_lie",
    }
    label = label_map.get(kind, f"contract_coverage_{kind}")
    lies = coverage_verdict.get("lies") or []
    contract_ids = [
        str(entry.get("contract_id") or "")
        for entry in lies
        if isinstance(entry, dict) and entry.get("contract_id")
    ]
    return FailureClassification(
        failure_class=label,
        scope="gate:contract_coverage",
        task_family=task_family,
        lesson=(
            f"contract_coverage gate emitted verdict_kind={kind}. "
            f"Contracts flagged: {', '.join(contract_ids[:5]) or 'none listed'}. "
            "Future planning must surface required_contracts the diff "
            "actually has to satisfy; future codegen must emit the "
            "## CONTRACT_COVERAGE JSON block with the correct "
            "evidence_mode (diff_modified_payload_existing_sink vs "
            "direct_diff) so the diff-anchored verifier can confirm "
            "the claim against the patched tree."
        ),
        evidence_refs={
            "task_id": task_id,
            "provider": provider,
            "verdict_kind": kind,
            "verdict_summary": (coverage_verdict.get("summary") or "")[:400],
            "verified_implemented": coverage_verdict.get("verified_implemented") or [],
            "verified_no_change": coverage_verdict.get("verified_no_change") or [],
            "missing": coverage_verdict.get("missing") or [],
            "lies": coverage_verdict.get("lies") or [],
        },
        prompt_eligible=["planner_warning"],
    )


def classify_compile_repair_timeout(
    *,
    pipeline_failed_message: str = "",
    cap_exceeded: bool = False,
    stuck: bool = False,
    rounds_completed: int = 0,
    provider: str | None = None,
    task_family: str | None = None,
    task_id: str | None = None,
) -> FailureClassification | None:
    """Fired when the multi-round compile_repair loop ran out of budget
    (cap_exceeded or stuck-signature exit). Distinct from a clean
    compile pass that just happened to fail on the first attempt —
    this row says "repair itself ran out of headroom".
    """
    if not (cap_exceeded or stuck):
        # Don't infer from message strings here; only record when the
        # caller explicitly signals these terminal states.
        return None
    label = "compile_repair_cap_exceeded" if cap_exceeded else "compile_repair_stuck"
    return FailureClassification(
        failure_class=label,
        scope="gate:compile_repair",
        task_family=task_family,
        lesson=(
            f"compile_repair terminated as {label} after "
            f"{rounds_completed} round(s). The diff produced by codegen "
            "needed more repair iterations than the loop budget allowed, "
            "or kept reproducing the same compile error signature. "
            "Future planning must keep change_summary tight enough that "
            "codegen produces a syntactically reasonable diff on the "
            "first pass; future codegen must verify import paths and "
            "API signatures against the repo library fingerprint before "
            "submitting."
        ),
        evidence_refs={
            "task_id": task_id,
            "provider": provider,
            "rounds_completed": rounds_completed,
            "cap_exceeded": cap_exceeded,
            "stuck": stuck,
            "pipeline_message": pipeline_failed_message[:400],
        },
        prompt_eligible=["planner_warning", "codegen_warning"],
    )


def classify_provider_response_issue(
    *,
    plan_provider_mode: str | None,
    plan_provider_name: str | None,
    codegen_provider: str | None,
    diff_chars: int | None,
    expected_min_files: int = 1,
    task_family: str | None = None,
    task_id: str | None = None,
) -> FailureClassification | None:
    """Fired when a provider returned empty or undersized output. Two
    sub-cases: empty (plan_provider fallback fired because deepseek
    returned no content) and partial (codegen returned a diff covering
    fewer files than the must_touch set required).
    """
    mode = (plan_provider_mode or "").lower()
    if mode in ("fallback_after_all_providers_failed", "fallback_after_primary_failed"):
        return FailureClassification(
            failure_class="provider_empty_response",
            scope="provider:liveness",
            task_family=task_family,
            lesson=(
                f"Provider {plan_provider_name!r} fell back during "
                "planning. The configured primary returned an unusable "
                "response (empty / timeout / error); the mock fallback "
                "produced a low-quality plan that downstream gates "
                "rejected. Future runs on this task_family should "
                "consider switching primary planner to a more reliable "
                "model when the configured one is unstable."
            ),
            evidence_refs={
                "task_id": task_id,
                "plan_provider_mode": plan_provider_mode,
                "plan_provider_name": plan_provider_name,
            },
            prompt_eligible=["planner_warning"],
        )
    # Partial-diff heuristic: codegen returned something but it was
    # clearly under-sized for the must_touch surface.
    if diff_chars is not None and diff_chars > 0 and diff_chars < 3000 and expected_min_files >= 2:
        return FailureClassification(
            failure_class="provider_partial_diff",
            scope="provider:codegen",
            task_family=task_family,
            lesson=(
                f"Provider {codegen_provider!r} returned a diff of only "
                f"{diff_chars} characters when {expected_min_files} "
                "must_touch file(s) were declared. The provider is "
                "either truncating output or skipping files. Future "
                "codegen prompt for this task_family should explicitly "
                "list every must_touch file with a per-file checklist "
                "and validate patched_files coverage before returning."
            ),
            evidence_refs={
                "task_id": task_id,
                "codegen_provider": codegen_provider,
                "diff_chars": diff_chars,
                "expected_min_files": expected_min_files,
            },
            prompt_eligible=["planner_warning", "codegen_warning"],
        )
    return None


# ---- Dispatcher ----------------------------------------------------------


def classify(
    *,
    plan_must_touch: list[str] | None = None,
    files_actually_patched: list[str] | None = None,
    pipeline_failed_message: str = "",
    coverage_verdict: dict[str, Any] | None = None,
    compile_repair_cap_exceeded: bool = False,
    compile_repair_stuck: bool = False,
    compile_repair_rounds_completed: int = 0,
    plan_provider_mode: str | None = None,
    plan_provider_name: str | None = None,
    codegen_provider: str | None = None,
    diff_chars: int | None = None,
    task_family: str | None = None,
    task_id: str | None = None,
) -> list[FailureClassification]:
    """Run every classifier in priority order and return ALL that match.

    A single terminal failure can fire multiple classifiers (e.g. a
    contract_coverage lie that ALSO had a must_touch miss). The
    orchestrator writes one ``failure_observation`` row per classifier
    that matched, so the retrieval layer can index each angle separately.

    Returns an empty list when no classifier recognized the failure
    signals — in which case the caller should NOT write a failure row,
    since we'd just be guessing.
    """
    results: list[FailureClassification] = []

    # Order matters only for documentation — the orchestrator emits
    # one row per match, so two simultaneous failure modes are recorded
    # without collision.
    for fn, kwargs in (
        (
            classify_must_touch_incomplete_diff,
            {
                "plan_must_touch": plan_must_touch or [],
                "files_actually_patched": files_actually_patched or [],
                "pipeline_failed_message": pipeline_failed_message,
                "provider": codegen_provider,
                "task_family": task_family,
                "task_id": task_id,
            },
        ),
        (
            classify_contract_coverage_failure,
            {
                "coverage_verdict": coverage_verdict,
                "provider": codegen_provider,
                "task_family": task_family,
                "task_id": task_id,
            },
        ),
        (
            classify_compile_repair_timeout,
            {
                "pipeline_failed_message": pipeline_failed_message,
                "cap_exceeded": compile_repair_cap_exceeded,
                "stuck": compile_repair_stuck,
                "rounds_completed": compile_repair_rounds_completed,
                "provider": codegen_provider,
                "task_family": task_family,
                "task_id": task_id,
            },
        ),
        (
            classify_provider_response_issue,
            {
                "plan_provider_mode": plan_provider_mode,
                "plan_provider_name": plan_provider_name,
                "codegen_provider": codegen_provider,
                "diff_chars": diff_chars,
                "expected_min_files": len(plan_must_touch or []),
                "task_family": task_family,
                "task_id": task_id,
            },
        ),
    ):
        result = fn(**kwargs)
        if result is not None:
            results.append(result)
    return results


# ---- Task-family detection (lightweight, regex-only) ---------------------


_FAMILY_KEYWORDS = (
    # ordered for first-match — most specific first
    ("android_map_location", ("map-based", "map picker", "geocod", "osmdroid", "mapview", "android.*location")),
    ("android_kyc_address", ("kyc", "address form", "address picker")),
    ("android_signup", ("signup", "sign up", "registration", "kyc")),
    ("firebase_persistence", ("firebase", "updatechildren", "setvalue", "firestore")),
    ("python_refactor", ("refactor", "rename", "extract function", "deduplicate")),
    ("python_bugfix", ("bug fix", "fix bug", "resolve bug")),
)


def detect_task_family(request_text: str, plan_json: dict[str, Any] | None = None) -> str | None:
    """Cheap keyword heuristic that buckets a task into a known family.

    Returns ``None`` if no rule matches — the failure observation is
    still written, just without a family tag (retrieval matches by
    scope only in that case).
    """
    import re as _re

    haystack_parts: list[str] = [request_text or ""]
    if isinstance(plan_json, dict):
        for key in ("objective", "change_summary", "request_summary"):
            v = plan_json.get(key)
            if isinstance(v, str):
                haystack_parts.append(v)
        files = plan_json.get("must_touch_files") or []
        if isinstance(files, list):
            haystack_parts.extend(f for f in files if isinstance(f, str))
    haystack = " ".join(haystack_parts).lower()
    if not haystack.strip():
        return None
    for family, keywords in _FAMILY_KEYWORDS:
        for kw in keywords:
            try:
                if _re.search(kw, haystack):
                    return family
            except _re.error:
                if kw in haystack:
                    return family
    return None
