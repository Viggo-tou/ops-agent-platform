"""Benchmark dataset loader."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One row of the QA benchmark dataset."""
    id: str
    tier: str  # A | B | C — difficulty
    source_name: str  # handymanapp | hosteddashboard | ...
    question: str
    expected_citations: tuple[str, ...]  # repo-relative paths
    expected_answer_keypoints: tuple[str, ...] = field(default_factory=tuple)

    @property
    def expected_files(self) -> tuple[str, ...]:
        """expected_citations stripped of the source-name prefix.

        The benchmark stores citations as ``handymanapp/app/src/.../X.kt``;
        retrieval strategies usually return ``app/src/.../X.kt``. This
        property returns the latter for matching."""
        out: list[str] = []
        for c in self.expected_citations:
            norm = c.strip().replace("\\", "/")
            prefix = f"{self.source_name}/"
            if norm.startswith(prefix):
                out.append(norm[len(prefix):])
            else:
                out.append(norm)
        return tuple(out)


def load_questions(
    path: str | Path,
    *,
    tier_filter: tuple[str, ...] | None = None,
) -> list[BenchmarkQuestion]:
    """Read a JSONL benchmark file and return BenchmarkQuestions.

    ``tier_filter`` keeps only the listed tiers (e.g. ``("A","B")``).
    """
    p = Path(path)
    out: list[BenchmarkQuestion] = []
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            tier = str(row.get("tier") or "A")
            if tier_filter and tier not in tier_filter:
                continue
            out.append(BenchmarkQuestion(
                id=str(row.get("id") or ""),
                tier=tier,
                source_name=str(row.get("source_name") or ""),
                question=str(row.get("question") or ""),
                expected_citations=tuple(row.get("expected_citations") or []),
                expected_answer_keypoints=tuple(
                    row.get("expected_answer_keypoints") or []
                ),
            ))
    return out


def load_all_default_benchmarks() -> list[BenchmarkQuestion]:
    """Load both handymanapp + hosteddashboard datasets shipped with the
    project. Dataset paths are resolved relative to apps/backend/."""
    backend_root = Path(__file__).resolve().parents[3]
    bench_dir = backend_root / "tests" / "benchmarks"
    files = (
        bench_dir / "qa_benchmark_dataset_handymanapp.jsonl",
        bench_dir / "qa_benchmark_dataset_hosteddashboard.jsonl",
    )
    out: list[BenchmarkQuestion] = []
    for f in files:
        if f.is_file():
            out.extend(load_questions(f))
    return out
