"""Stage 19 handymanapp diagnostic: where does the score gap come from?

Splits records into diagnostic buckets to disambiguate:
  - retrieval failure (expected file not at top)
  - card weakness (expected file retrieved, but card lacks keypoint tokens)
  - answer weakness (card has tokens, but answer doesn't)
  - judge harshness (answer paraphrases vs literal substring)

Decides Stage 20 priority: 20B (answer prompt) vs 20C (cards-v2) vs 20A (judge upgrade).

Usage:
  python -m scripts.analyze_stage19_diagnostic <artifact.jsonl>
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _tokenize_keypoint(keypoint: str) -> list[str]:
    """Match harness rule: 4+ char tokens, lowercased, alnum/underscore/dot/dash/at."""
    return re.findall(r"[@a-z0-9_./-]{4,}", str(keypoint).lower())


def _keypoint_in_text(keypoint: str, text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    if str(keypoint).lower() in text_lower:
        return True
    for token in _tokenize_keypoint(keypoint):
        if token in text_lower:
            return True
    return False


def _classify(record: dict[str, Any]) -> str:
    """Diagnostic bucket per record."""
    if record.get("score_status") != "valid":
        return "infra"
    score = float(record.get("score") or 0.0)
    rank = record.get("expected_citation_top_rank")
    fact_in_card = record.get("expected_fact_in_card")

    if rank is None:
        return "retrieval_miss"
    if rank > 1:
        return f"retrieval_rank_{rank}"
    if fact_in_card is False:
        return "card_missing_token"
    # rank=1 AND (fact_in_card True or None)
    if score >= 50:
        return "high_control"
    if score < 30:
        return "answer_or_judge_low"
    return "mid_score"


def _answer_keypoint_hit_rate(record: dict[str, Any]) -> tuple[int, int, list[str]]:
    """Count how many expected keypoints appear in answer_excerpt (substring or token)."""
    answer = str(record.get("answer_excerpt") or "")
    keypoints = list(record.get("expected_answer_keypoints") or [])
    misses: list[str] = []
    hits = 0
    for kp in keypoints:
        if _keypoint_in_text(kp, answer):
            hits += 1
        else:
            misses.append(kp)
    return hits, len(keypoints), misses


def _per_tier_aggregate(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for tier in ("A", "B", "C", "D"):
        tier_records = [r for r in records if r.get("tier") == tier]
        if not tier_records:
            continue
        valid = [r for r in tier_records if r.get("score_status") == "valid"]
        scores = [float(r.get("score") or 0.0) for r in valid]
        fact_records = [
            r for r in tier_records if r.get("expected_fact_in_card") is not None
        ]
        fact_true = [r for r in fact_records if r.get("expected_fact_in_card") is True]
        out[tier] = {
            "count": len(tier_records),
            "valid_count": len(valid),
            "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
            "fact_in_card_rate": (
                round(len(fact_true) / len(fact_records), 3) if fact_records else None
            ),
            "fact_in_card_n": f"{len(fact_true)}/{len(fact_records)}",
        }
    return out


def _per_tier_buckets(records: list[dict[str, Any]]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for r in records:
        tier = r.get("tier") or "?"
        out[tier][_classify(r)] += 1
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python -m scripts.analyze_stage19_diagnostic <artifact.jsonl>", file=sys.stderr)
        return 2
    artifact_path = Path(argv[1])
    with artifact_path.open(encoding="utf-8") as f:
        lines = f.readlines()
    summary = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:]]

    print(f"=== ARTIFACT {artifact_path.name} ===")
    print(f"records: {len(records)}; running={summary.get('running')}; "
          f"abort_reason={summary.get('abort_reason')}")
    print()

    print("=== PER-TIER ===")
    per_tier = _per_tier_aggregate(records)
    for tier, agg in per_tier.items():
        print(f"  {tier}: count={agg['count']:3d} valid={agg['valid_count']:3d} "
              f"mean={agg['mean_score']} fact_in_card={agg['fact_in_card_rate']} "
              f"({agg['fact_in_card_n']})")
    print()

    print("=== DIAGNOSTIC BUCKETS PER TIER ===")
    tier_buckets = _per_tier_buckets(records)
    all_buckets = sorted({b for c in tier_buckets.values() for b in c})
    print(f"  {'tier':5s}", *(f"{b:24s}" for b in all_buckets))
    for tier in ("A", "B", "C", "D"):
        if tier not in tier_buckets:
            continue
        c = tier_buckets[tier]
        print(f"  {tier:5s}", *(f"{c.get(b, 0):24d}" for b in all_buckets))
    print()

    print("=== ANSWER-LEVEL KEYPOINT MATCH (top_rank=1 AND score<30) ===")
    print("Disambiguates: did the LLM answer literally include expected keypoint tokens?")
    print("Per record: hits/total, then list of missing keypoints (≤3 shown).")
    print()
    low_samples = [
        r for r in records
        if r.get("expected_citation_top_rank") == 1
        and r.get("score_status") == "valid"
        and float(r.get("score") or 0.0) < 30.0
    ]
    answer_hit_zero = 0
    answer_hit_partial = 0
    fact_in_card_but_answer_zero = 0
    for r in low_samples:
        hits, total, misses = _answer_keypoint_hit_rate(r)
        flag_card = r.get("expected_fact_in_card")
        if hits == 0:
            answer_hit_zero += 1
            if flag_card is True:
                fact_in_card_but_answer_zero += 1
        elif hits < total:
            answer_hit_partial += 1
        miss_preview = "; ".join(str(m)[:60] for m in misses[:3])
        print(f"  {r['question_id']:6s} score={r['score']:5.1f} "
              f"answer_kp_hit={hits}/{total} fact_in_card={flag_card} "
              f"misses=[{miss_preview}]")
    print()
    if low_samples:
        print(f"  -- summary: zero-hit={answer_hit_zero}/{len(low_samples)} "
              f"partial={answer_hit_partial}/{len(low_samples)} "
              f"fact_in_card_but_answer_zero={fact_in_card_but_answer_zero}/{len(low_samples)}")
    print()

    print("=== HIGH-CONTROL (top_rank=1 AND score>=50) ===")
    high_samples = [
        r for r in records
        if r.get("expected_citation_top_rank") == 1
        and r.get("score_status") == "valid"
        and float(r.get("score") or 0.0) >= 50.0
    ]
    hi_zero = 0
    hi_partial = 0
    hi_full = 0
    for r in high_samples:
        hits, total, misses = _answer_keypoint_hit_rate(r)
        if hits == 0:
            hi_zero += 1
        elif hits < total:
            hi_partial += 1
        else:
            hi_full += 1
        miss_preview = "; ".join(str(m)[:60] for m in misses[:3])
        print(f"  {r['question_id']:6s} score={r['score']:5.1f} "
              f"answer_kp_hit={hits}/{total} fact_in_card={r.get('expected_fact_in_card')} "
              f"misses=[{miss_preview}]")
    print()
    if high_samples:
        print(f"  -- summary: full-hit={hi_full}/{len(high_samples)} "
              f"partial={hi_partial}/{len(high_samples)} zero-hit={hi_zero}/{len(high_samples)}")
    print()

    print("=== STAGE 20 RECOMMENDATION ===")
    if not low_samples:
        print("  Insufficient low samples; need more records before recommending.")
    else:
        ratio_card_has_token = fact_in_card_but_answer_zero / max(answer_hit_zero, 1)
        if answer_hit_zero >= 3 and ratio_card_has_token >= 0.7:
            print("  → Stage 20B (answer prompt fix) is PRIMARY:")
            print(f"    {fact_in_card_but_answer_zero}/{answer_hit_zero} zero-hit cases "
                  f"have card containing keypoint tokens. Bottleneck is answer generation, "
                  f"not card extraction.")
            print("  → Stage 20A (hybrid judge) still required as infra (rule-only is "
                  f"structurally fragile cross-stack).")
            print("  → Stage 20C (cards-v2) deprioritized unless Stage 20B alone is insufficient.")
        elif answer_hit_zero >= 3 and ratio_card_has_token < 0.3:
            print("  → Stage 20C (cards-v2) is PRIMARY:")
            print(f"    only {fact_in_card_but_answer_zero}/{answer_hit_zero} zero-hit cases "
                  f"had card containing tokens. Cards are missing the symbol layer.")
            print("  → Stage 20A still required as infra.")
        else:
            print(f"  → MIXED signal: Stage 20B and 20C both warranted.")
            print(f"    {fact_in_card_but_answer_zero}/{answer_hit_zero} zero-hit cases "
                  f"have token in card; ratio {ratio_card_has_token:.2f} "
                  f"(>=0.7 → 20B primary; <0.3 → 20C primary).")
        print()
        if hi_full >= 3 and answer_hit_zero >= 3:
            print(f"  Control-vs-low contrast: high-score samples {hi_full}/{len(high_samples)} "
                  f"land full-hit; low-score samples {answer_hit_zero}/{len(low_samples)} "
                  f"are zero-hit. Mechanism is clean: same retrieval, different answer behavior.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
