from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import httpx

from scripts.run_qa_benchmark import (
    ANSWER_EXCERPT_MAX_BYTES,
    TERMINAL_STATUSES,
    KeypointJudge,
    _judge_family_metadata,
    compute_citation_precision,
    compute_question_entity_coverage,
    coerce_keypoint_hit_details,
    extract_answer_and_citations,
    hybrid_v2_disagreement_summary,
    judge_rung_summary,
    multifile_coverage_summary,
    _miss_details,
    truncate_utf8,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-judge a completed QA benchmark run.")
    parser.add_argument("--in-run", required=True)
    parser.add_argument("--backend-url", required=True)
    parser.add_argument(
        "--judge-mode",
        choices=("minimax", "claude_code", "codex", "anthropic", "rule", "hybrid", "hybrid_v2"),
        default="minimax",
        help=(
            "Rejudge mode. V1 default: minimax. 'rule' is lexical diagnostic; "
            "'hybrid' is the retired V1 experimental max judge. 'hybrid_v2' "
            "is the conservative MiniMax+Codex AND-gated diagnostic judge. "
            "'auto' is REMOVED; pin explicitly."
        ),
    )
    parser.add_argument("--judge-samples", type=int, default=3)
    parser.add_argument("--out-run", required=True)
    return parser.parse_args()


def resolve(raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else Path.cwd() / path


def read_run(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise SystemExit(f"Input run is empty: {path}")
    summary = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:]]
    return summary, [record for record in records if record.get("type") == "question"]


def task_answer(task: dict[str, Any]) -> tuple[str, list[str], list[str], list[dict[str, Any]]]:
    """Reuse the bench's normalized extractor so citation_precision compares
    canonical paths against the dataset's expected_citations (also canonical).
    Returns (answer, display_citations, canonical_citations, structured_citations)."""
    answer, display, canonical, structured, _trace = extract_answer_and_citations(task)
    return answer, display, canonical, structured


def keypoints(record: dict[str, Any]) -> list[str]:
    return [
        str(item.get("keypoint") or "").strip()
        for item in record.get("keypoint_hits") or []
        if isinstance(item, dict) and str(item.get("keypoint") or "").strip()
    ]


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def tier_summary(records: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tier in ("A", "B", "C", "D"):
        rows = [record for record in records if record.get("tier") == tier]
        scores = [float(record.get("score") or 0.0) for record in rows]
        out[tier] = {
            "count": len(rows),
            "completed": sum(1 for record in rows if record.get("completed")),
            "mean_score": round(mean(scores), 2) if scores else 0.0,
            "min_score": round(min(scores), 2) if scores else 0.0,
            "max_score": round(max(scores), 2) if scores else 0.0,
        }
    return out


def write_run(path: Path, summary: dict[str, Any], records: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def overall_status(counts: Counter[str], good: str, empty: str | None = None) -> str:
    total = sum(counts.values())
    if total and counts == {good: total}:
        return good
    if empty and total and counts == {empty: total}:
        return empty
    return "fail" if good == "pass" else "invalid"


def main() -> int:
    args = parse_args()
    started = time.monotonic()
    started_at = datetime.now(timezone.utc)
    original_summary, input_records = read_run(resolve(args.in_run))
    judge = KeypointJudge(requested_mode=args.judge_mode, samples=args.judge_samples)
    try:
        judge.judge(question="ping", answer="ping", keypoints=["ping"])
    except Exception as exc:  # noqa: BLE001
        print(f"Preflight judge call failed: {exc}; aborting before consuming budget", file=sys.stderr)
        return 2

    records: list[dict[str, Any]] = []
    fallback_to_rule_count = 0
    headers = {"X-Actor-Name": "qa-benchmark"}
    timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=30.0)
    with httpx.Client(timeout=timeout, headers=headers) as client:
        for index, record in enumerate(input_records, 1):
            qid = str(record.get("question_id") or f"Q{index:02d}")
            task_id = str(record.get("task_id") or "").strip()
            expected = [str(item) for item in record.get("expected_citations") or []]
            points = keypoints(record)
            answer, citations, display_citations, structured_citations = "", [], [], []
            error, judge_error = None, None
            judge_status_codex_rung, judge_error_codex_rung = None, None
            hits = _miss_details(points)
            judge_mode = "skipped"
            task_status, completed, question = "fetch_error", False, str(record.get("question") or "")
            synthesis_status, judge_status = "task_error", "skipped"
            try:
                response = client.get(f"{args.backend_url.rstrip('/')}/api/tasks/{task_id}")
                response.raise_for_status()
                task = response.json()
                if not isinstance(task, dict):
                    raise RuntimeError(f"Task {task_id} response was not an object")
                question = str(task.get("request_text") or question)
                task_status = str(task.get("status") or record.get("task_status") or "")
                completed = task_status.lower() in TERMINAL_STATUSES
                answer, display_citations, citations, structured_citations = task_answer(task)
                synthesis_status = "pass" if answer.strip() else (
                    "empty" if task_status.lower() == "completed" else "task_error"
                )
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
            if answer.strip():
                try:
                    hits, judge_mode = judge.judge(
                        question=question,
                        answer=answer,
                        keypoints=points,
                        citations=structured_citations,
                        expected_citations=expected,
                    )
                    hits = coerce_keypoint_hit_details(
                        hits,
                        keypoints=points,
                        mode=judge_mode,
                    )
                    hybrid_llm_error = getattr(judge, "last_hybrid_llm_error", None)
                    if hybrid_llm_error:
                        judge_error = f"MiniMax hybrid rung failed: {hybrid_llm_error}"
                        if error is None:
                            error = judge_error
                    judge_status_codex_rung = getattr(judge, "last_codex_rung_status", None)
                    judge_error_codex_rung = getattr(judge, "last_codex_rung_error", None)
                    if (
                        args.judge_mode == "hybrid_v2"
                        and judge_status_codex_rung
                        and judge_status_codex_rung != "pass"
                        and judge_error is None
                    ):
                        judge_error = f"Codex rung failed: {judge_error_codex_rung or judge_status_codex_rung}"
                    judge_status = "pass"
                except Exception as exc:  # noqa: BLE001
                    judge_mode = args.judge_mode
                    judge_status = "fail"
                    judge_error = f"{type(exc).__name__}: {exc}"
            if args.judge_mode == "claude_code" and judge_mode == "rule":
                fallback_to_rule_count += 1
            kp = sum(float(item.get("hit_score") or 0.0) for item in hits) / max(len(hits), 1)
            cp = compute_citation_precision(expected, citations)
            score_status = "valid" if synthesis_status == "pass" and judge_status == "pass" else "invalid"
            score = (kp * 60.0 + cp * 40.0) if score_status == "valid" else 0.0
            entity_coverage = compute_question_entity_coverage(question, answer)
            out = dict(record)
            out.update(
                task_status=task_status,
                completed=completed,
                score=round(score, 2),
                keypoint_coverage=round(kp, 4),
                citation_precision=round(cp, 4),
                keypoint_hits=hits,
                citations_found=display_citations,
                judge_mode=judge_mode,
                judge_status_codex_rung=judge_status_codex_rung,
                judge_error_codex_rung=judge_error_codex_rung,
                synthesis_status=synthesis_status,
                judge_status=judge_status,
                score_status=score_status,
                answer_excerpt=truncate_utf8(answer, min(ANSWER_EXCERPT_MAX_BYTES, 1500)),
                error=error,
                judge_error=judge_error,
                mentioned_entities=entity_coverage["mentioned_entities"],
                covered_entities=entity_coverage["covered_entities"],
                omitted_entities=entity_coverage["omitted_entities"],
                multifile_mode_active=entity_coverage["multifile_mode_active"],
                coverage_rate=round(float(entity_coverage["coverage_rate"]), 4),
            )
            records.append(out)
            print(f"[Q{index:02d}/{len(input_records):02d}] {qid} score={score:.2f} (kp={kp:.2f}, cp={cp:.2f}) judge={judge_mode}", file=sys.stderr)

    finished_at = datetime.now(timezone.utc)
    syn_counts = Counter(str(record.get("synthesis_status")) for record in records)
    judge_counts = Counter(str(record.get("judge_status")) for record in records)
    score_counts = Counter(str(record.get("score_status")) for record in records)
    summary = dict(original_summary)
    judge_rung_usage, per_rung_kp_hit_rate, disagreement_count = judge_rung_summary(
        records,
        enabled=args.judge_mode == "hybrid",
    )
    (
        v2_disagreement_taxonomy,
        v2_codex_failures,
        v2_disagreement_rate,
    ) = hybrid_v2_disagreement_summary(
        records,
        enabled=args.judge_mode == "hybrid_v2",
    )
    judge_family_count, cross_family_validated, judge_caveats = _judge_family_metadata(
        args.judge_mode,
        records,
    )
    multifile_summary = multifile_coverage_summary(records)
    summary.update(
        status="completed",
        started_at_utc=started_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        finished_at_utc=finished_at.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        artifact_path=str(resolve(args.out_run)),
        backend_url=args.backend_url,
        requested_judge_mode=args.judge_mode,
        judge_model=judge.judge_model,
        judge_modes_used=list(
            dict.fromkeys(
                str(record.get("judge_mode"))
                for record in records
                if record.get("judge_mode") and str(record.get("judge_mode")) != "skipped"
            )
        ),
        pinned_judge_failure_count=sum(
            1 for record in records if record.get("judge_status") == "fail"
        ),
        pinned_judge_run_intact=not any(
            record.get("judge_status") == "fail" for record in records
        ),
        judge_family_count=judge_family_count,
        cross_family_validated=cross_family_validated,
        judge_caveats=judge_caveats,
        score_averaging_note=(
            "Records with score_status='invalid' are infrastructure/judge/synthesis failures "
            "and must not be averaged as model-quality scores."
        ),
        synthesis_status=overall_status(syn_counts, "pass"),
        judge_status=overall_status(judge_counts, "pass", "skipped"),
        score_status=overall_status(score_counts, "valid"),
        total_questions=len(records),
        completed_questions=sum(1 for record in records if record.get("completed")),
        judge_failure_count=sum(1 for record in records if record.get("judge_status") == "fail"),
        fallback_to_rule_count=fallback_to_rule_count,
        synthesis_status_counts=dict(sorted(syn_counts.items())),
        judge_status_counts=dict(sorted(judge_counts.items())),
        score_status_counts=dict(sorted(score_counts.items())),
        tier_summary=tier_summary(records),
        overall_mean_score=round(mean([float(record.get("score") or 0.0) for record in records]), 2),
        judge_rung_usage=judge_rung_usage,
        per_rung_kp_hit_rate=per_rung_kp_hit_rate,
        disagreement_count=disagreement_count,
        v2_disagreement_taxonomy=v2_disagreement_taxonomy,
        v2_codex_failure_count=v2_codex_failures,
        v2_disagreement_rate=v2_disagreement_rate,
        multifile_mode_records=multifile_summary["multifile_mode_records"],
        multifile_mode_avg_coverage_rate=multifile_summary[
            "multifile_mode_avg_coverage_rate"
        ],
        total_omitted_entities=multifile_summary["total_omitted_entities"],
    )
    write_run(resolve(args.out_run), summary, records)
    by_tier: dict[str, list[float]] = defaultdict(list)
    for record in records:
        by_tier[str(record.get("tier") or "")].append(float(record.get("score") or 0.0))
    tiers = " ".join(f"{tier}={mean(by_tier[tier]):.2f}" for tier in ("A", "B", "C", "D"))
    overall = mean([float(record.get("score") or 0.0) for record in records])
    print(f"summary tiers: {tiers} overall={overall:.2f} score_status={dict(sorted(score_counts.items()))} runtime={time.monotonic() - started:.1f}s", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
