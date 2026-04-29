from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import run_qa_benchmark as bench


def _write_dataset(path: Path, count: int = 1) -> None:
    rows = []
    for index in range(count):
        rows.append(
            {
                "id": f"Q{index + 1:02d}",
                "tier": "A",
                "question": f"Question {index + 1}?",
                "expected_answer_keypoints": ["ping"],
                "expected_citations": ["src/a.py"],
            }
        )
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _task(answer: str, *, status: str = "completed") -> dict[str, object]:
    citations = [{"relative_path": "src/a.py", "source_name": "repo"}] if answer else []
    return {
        "status": status,
        "latest_result_json": {
            "result": {
                "answer": answer,
                "citations": citations,
            }
        },
    }


def _read_artifact(out_dir: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
    artifacts = list(out_dir.glob("qa-run-*.jsonl"))
    assert len(artifacts) == 1
    lines = artifacts[0].read_text(encoding="utf-8").splitlines()
    summary = json.loads(lines[0])
    records = [json.loads(line) for line in lines[1:]]
    return summary, records


class FakeClient:
    submitted = 0
    tasks: list[dict[str, object] | None] = [_task("answer ping")]

    def __init__(self, backend_url: str, actor_name: str) -> None:
        self.backend_url = backend_url
        self.actor_name = actor_name

    def ensure_backend_reachable(self) -> None:
        return None

    def submit_question(self, row: bench.DatasetRow) -> dict[str, object]:
        type(self).submitted += 1
        return {"id": f"task-{row.id}"}

    def poll_task(
        self, task_id: str, timeout_seconds: float = bench.QUESTION_TIMEOUT_SECONDS
    ) -> tuple[dict[str, object] | None, float, bool]:
        index = type(self).submitted - 1
        task = type(self).tasks[index]
        return task, 0.01, task is None

    def close(self) -> None:
        return None


class PassingJudge:
    def __init__(self, requested_mode: str, samples: int = 1) -> None:
        self.requested_mode = requested_mode
        self.samples = samples
        self.judge_model = "fake"
        self.auto_rule_reason: str | None = None

    def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
        return [True] * len(keypoints), self.requested_mode if self.requested_mode != "auto" else "rule"


@pytest.fixture()
def bench_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    dataset = tmp_path / "dataset.jsonl"
    out_dir = tmp_path / "runs"
    _write_dataset(dataset)
    FakeClient.submitted = 0
    FakeClient.tasks = [_task("answer ping")]
    monkeypatch.setattr(bench, "BenchmarkClient", FakeClient)

    def run(*, judge_mode: str = "rule", judge_cls: type[object] = PassingJudge, count: int = 1) -> int:
        _write_dataset(dataset, count=count)
        monkeypatch.setattr(bench, "KeypointJudge", judge_cls)
        monkeypatch.setattr(
            "sys.argv",
            [
                "run_qa_benchmark.py",
                "--dataset",
                str(dataset),
                "--out-dir",
                str(out_dir),
                "--backend-url",
                "http://backend.test",
                "--judge-mode",
                judge_mode,
            ],
        )
        return bench.main()

    return run, out_dir


def test_strict_pin_fails_fast_on_preflight_failure(bench_run, capsys: pytest.CaptureFixture[str]) -> None:
    class FailingPreflightJudge(PassingJudge):
        def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
            raise RuntimeError("judge down")

    run, out_dir = bench_run
    assert run(judge_mode="claude_code", judge_cls=FailingPreflightJudge) == 2
    assert FakeClient.submitted == 0
    summary, records = _read_artifact(out_dir)
    assert records == []
    assert summary["preflight_judge_status"] == "fail"
    assert "aborting before consuming synthesis budget" in capsys.readouterr().err


def test_strict_pin_records_per_q_judge_failure_without_fallback(bench_run) -> None:
    class PerQuestionFailJudge(PassingJudge):
        def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
            if question == "ping":
                return [True], "claude_code"
            raise RuntimeError("npm EPERM")

    run, out_dir = bench_run
    assert run(judge_mode="claude_code", judge_cls=PerQuestionFailJudge) == 2
    summary, records = _read_artifact(out_dir)
    assert records[0]["judge_status"] == "fail"
    assert records[0]["judge_mode"] == "claude_code"
    assert records[0]["judge_mode"] != "rule"
    assert summary["judge_modes_used"] == ["claude_code"]
    assert summary["pinned_judge_failure_count"] == 1
    assert summary["pinned_judge_run_intact"] is False


def test_auto_mode_falls_back_with_recorded_reason(bench_run) -> None:
    class AutoFallbackJudge(PassingJudge):
        def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
            self.auto_rule_reason = "Claude Code CLI judge failed: npm EPERM; trying next."
            return [True] * len(keypoints), "rule"

    run, out_dir = bench_run
    assert run(judge_mode="auto", judge_cls=AutoFallbackJudge) == 0
    summary, records = _read_artifact(out_dir)
    assert records[0]["judge_mode"] == "rule"
    assert summary["judge_modes_used"] == ["rule"]
    assert summary["judge_auto_fallback_reason"]


def test_answer_excerpt_populated_when_judge_fails(bench_run) -> None:
    class PerQuestionFailJudge(PassingJudge):
        def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
            if question == "ping":
                return [True], "claude_code"
            raise RuntimeError("judge failed")

    run, out_dir = bench_run
    assert run(judge_mode="claude_code", judge_cls=PerQuestionFailJudge) == 2
    _summary, records = _read_artifact(out_dir)
    assert records[0]["answer_excerpt"] == "answer ping"
    assert records[0]["citations_found"] == ["repo/src/a.py"]
    assert records[0]["expected_citations"] == ["src/a.py"]


def test_status_fields_separated_synthesis_pass_judge_fail(bench_run) -> None:
    class PerQuestionFailJudge(PassingJudge):
        def judge(self, *, question: str, answer: str, keypoints: list[str] | tuple[str, ...]) -> tuple[list[bool], str]:
            if question == "ping":
                return [True], "claude_code"
            raise RuntimeError("judge failed")

    run, out_dir = bench_run
    assert run(judge_mode="claude_code", judge_cls=PerQuestionFailJudge) == 2
    _summary, records = _read_artifact(out_dir)
    assert records[0]["synthesis_status"] == "pass"
    assert records[0]["judge_status"] == "fail"
    assert records[0]["score_status"] == "invalid"
    assert records[0]["score"] == 0.0


def test_status_fields_separated_synthesis_empty_judge_skipped(bench_run) -> None:
    FakeClient.tasks = [_task("")]
    run, out_dir = bench_run
    assert run(judge_mode="rule") == 2
    _summary, records = _read_artifact(out_dir)
    assert records[0]["synthesis_status"] == "empty"
    assert records[0]["judge_status"] == "skipped"
    assert records[0]["score_status"] == "invalid"


def test_summary_aggregates_status_counts(bench_run) -> None:
    FakeClient.tasks = [_task("answer ping"), _task("")]
    run, out_dir = bench_run
    assert run(judge_mode="rule", count=2) == 2
    summary, _records = _read_artifact(out_dir)
    assert summary["synthesis_status_counts"] == {"empty": 1, "pass": 1}
    assert summary["judge_status_counts"] == {"pass": 1, "skipped": 1}
    assert summary["score_status_counts"] == {"invalid": 1, "valid": 1}


def test_preflight_records_status_in_summary(bench_run) -> None:
    run, out_dir = bench_run
    assert run(judge_mode="rule") == 0
    summary, _records = _read_artifact(out_dir)
    assert summary["preflight_judge_status"] == "pass"
    assert summary["preflight_judge_error"] is None
