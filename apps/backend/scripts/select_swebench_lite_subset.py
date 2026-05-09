"""Select a 50-task subset of SWE-bench-Lite proportional to repo distribution.

Output: apps/backend/tests/benchmarks/swebench_lite_subset_50.jsonl

The selection is deterministic given the seed, so re-running the harness
on a different day evaluates the same task set. Don't change the seed
without invalidating prior results.
"""
from __future__ import annotations

import json
import random
from collections import Counter
from pathlib import Path
from typing import Any

from datasets import load_dataset

REPO_ROOT = Path(__file__).resolve().parents[3]
OUT_PATH = REPO_ROOT / "apps" / "backend" / "tests" / "benchmarks" / "swebench_lite_subset_50.jsonl"
SEED = 42
TARGET_N = 50

_KEEP_FIELDS = (
    "instance_id",
    "repo",
    "base_commit",
    "problem_statement",
    "patch",
    "test_patch",
    "FAIL_TO_PASS",
    "PASS_TO_PASS",
    "version",
    "environment_setup_commit",
)


def proportional_alloc(counts: dict[str, int], total_target: int) -> dict[str, int]:
    """Hamilton apportionment: largest-remainder method.

    Each repo's quota is total_target * (count / sum(counts)). We floor each
    quota, then hand out the remaining seats to repos with the largest
    fractional remainders. Each repo with at least one task gets at least 1.
    """
    pool = sum(counts.values())
    raw = {r: total_target * c / pool for r, c in counts.items()}
    floor = {r: max(1, int(v)) for r, v in raw.items()}

    while sum(floor.values()) > total_target:
        # Decrement the repo whose current allocation overshoots its raw
        # quota by the most (i.e. has the smallest fractional remainder).
        candidate = max(
            (r for r, v in floor.items() if v > 1),
            key=lambda r: floor[r] - raw[r],
        )
        floor[candidate] -= 1

    while sum(floor.values()) < total_target:
        # Add to the repo whose raw quota most exceeds its current allocation.
        candidate = max(floor, key=lambda r: raw[r] - floor[r])
        floor[candidate] += 1

    return floor


def main() -> None:
    print("loading princeton-nlp/SWE-bench_Lite test split...")
    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test")
    print(f"  {len(ds)} tasks across {len(set(t['repo'] for t in ds))} repos")

    repo_counts = Counter(t["repo"] for t in ds)
    alloc = proportional_alloc(dict(repo_counts), TARGET_N)
    assert sum(alloc.values()) == TARGET_N, alloc

    print(f"\nproportional allocation (seed={SEED}):")
    for repo, n in sorted(alloc.items(), key=lambda kv: -kv[1]):
        pct = 100 * repo_counts[repo] / len(ds)
        print(f"  {repo:35s}  full={repo_counts[repo]:>3d} ({pct:>4.1f}%)  picked={n}")

    rng = random.Random(SEED)
    by_repo: dict[str, list[Any]] = {}
    for task in ds:
        by_repo.setdefault(task["repo"], []).append(task)
    for repo in by_repo:
        rng.shuffle(by_repo[repo])

    selected: list[dict[str, Any]] = []
    for repo, n in alloc.items():
        for task in by_repo[repo][:n]:
            selected.append({k: task[k] for k in _KEEP_FIELDS})

    selected.sort(key=lambda t: t["instance_id"])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as fp:
        for task in selected:
            fp.write(json.dumps(task, ensure_ascii=False) + "\n")

    print(f"\nwrote {len(selected)} tasks to {OUT_PATH.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
