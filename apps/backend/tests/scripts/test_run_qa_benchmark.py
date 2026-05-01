from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from scripts import run_qa_benchmark as bench


def _write_dataset(
    path: Path,
    count: int = 1,
    *,
    source_names: list[str | None] | None = None,
) -> None:
    rows = []
    for index in range(count):
        row = {
            "id": f"Q{index + 1:02d}",
            "tier": "A",
            "question": f"Question {index + 1}?",
            "expected_answer_keypoints": ["ping"],
            "expected_citations": ["src/a.py"],
        }
        if source_names is not None and index < len(source_names) and source_names[index] is not None:
            row["source_name"] = source_names[index]
        rows.append(row)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _task(
    answer: str,
    *,
    status: str = "completed",
    source_name: str = "repo",
    relative_path: str = "src/a.py",
    card_text: str | None = None,
) -> dict[str, object]:
    citations = [
        {
            "relative_path": relative_path,
            "source_name": source_name,
            "card_text": card_text,
        }
    ] if answer else []
    return {
        "status": status,
        "latest_result_json": {
            "result": {
                "answer": answer,
                "citations": citations,
                "answer_trace": {"selected_sources": [source_name]} if source_name else {},
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
    health_checks = 0
    tasks: list[dict[str, object] | None] = [_task("answer ping")]
    durations: list[float] = []

    def __init__(self, backend_url: str, actor_name: str) -> None:
        self.backend_url = backend_url
        self.actor_name = actor_name

    def ensure_backend_reachable(self) -> None:
        type(self).health_checks += 1
        return None

    def submit_question(self, row: bench.DatasetRow) -> dict[str, object]:
        type(self).submitted += 1
        return {"id": f"task-{row.id}"}

    def poll_task(
        self, task_id: str, timeout_seconds: float = bench.QUESTION_TIMEOUT_SECONDS
    ) -> tuple[dict[str, object] | None, float, bool]:
        index = type(self).submitted - 1
        task = type(self).tasks[index]
        duration = type(self).durations[index] if index < len(type(self).durations) else 0.01
        return task, duration, task is None

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
def bench_run(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    tmp_path = BACKEND_ROOT / "tmp-pytest" / request.node.name
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True)
    dataset = tmp_path / "dataset.jsonl"
    out_dir = tmp_path / "runs"
    _write_dataset(dataset)
    FakeClient.submitted = 0
    FakeClient.health_checks = 0
    FakeClient.tasks = [_task("answer ping")]
    FakeClient.durations = []
    monkeypatch.setattr(bench, "BenchmarkClient", FakeClient)

    def run(
        *,
        judge_mode: str = "rule",
        judge_cls: type[object] = PassingJudge,
        count: int = 1,
        extra_args: list[str] | None = None,
        source_names: list[str | None] | None = None,
    ) -> int:
        _write_dataset(dataset, count=count, source_names=source_names)
        if out_dir.exists():
            for artifact in out_dir.glob("qa-run-*.jsonl"):
                artifact.unlink()
        monkeypatch.setattr(bench, "KeypointJudge", judge_cls)
        argv = [
                "run_qa_benchmark.py",
                "--dataset",
                str(dataset),
                "--out-dir",
                str(out_dir),
                "--backend-url",
                "http://backend.test",
                "--judge-mode",
                judge_mode,
        ]
        if extra_args:
            argv.extend(extra_args)
        monkeypatch.setattr("sys.argv", argv)
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


def test_burst_pause_after_3_consecutive_infra_invalid(
    bench_run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(bench.time, "sleep", lambda seconds: sleeps.append(seconds))
    FakeClient.tasks = [None, None, None, _task("answer ping")]

    run, out_dir = bench_run
    assert run(judge_mode="rule", count=4, extra_args=["--infra-burst-backoff", "0.5,1"]) == 2

    summary, records = _read_artifact(out_dir)
    assert len(records) == 4
    assert sleeps == [0.5]
    assert FakeClient.health_checks == 2
    assert summary["infra_burst_count"] == 1
    assert summary["abort_reason"] is None
    assert "BENCH PAUSE" in capsys.readouterr().err


def test_burst_pause_resets_counter_on_valid_record(
    bench_run, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(bench.time, "sleep", lambda seconds: sleeps.append(seconds))
    FakeClient.tasks = [None, None, _task("answer ping"), None, None]

    run, out_dir = bench_run
    assert run(judge_mode="rule", count=5, extra_args=["--infra-burst-backoff", "0.5"]) == 2

    summary, records = _read_artifact(out_dir)
    assert len(records) == 5
    assert sleeps == []
    assert summary["infra_burst_count"] == 0


def test_burst_abort_after_max_bursts(
    bench_run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(bench.time, "sleep", lambda seconds: sleeps.append(seconds))
    FakeClient.tasks = [None, None, None, None, None, None]

    run, out_dir = bench_run
    assert run(judge_mode="rule", count=6, extra_args=["--infra-burst-backoff", "0"]) == 2

    summary, records = _read_artifact(out_dir)
    assert len(records) == 6
    assert sleeps == [0.0]
    assert summary["infra_burst_count"] == 2
    assert summary["abort_reason"] == "infra_burst_exceeded"
    assert summary["pinned_judge_run_intact"] is False
    assert "BENCH ABORT" in capsys.readouterr().err


def test_failure_bucket_classifies_30s_task_error_as_cc_failure() -> None:
    record = {
        "score_status": "invalid",
        "synthesis_status": "task_error",
        "judge_status": "skipped",
        "duration_s": 30.001,
        "error": None,
    }

    assert bench.classify_failure_bucket(record) == "cc_failure"


def test_failure_bucket_classifies_other_task_error_as_infra() -> None:
    record = {
        "score_status": "invalid",
        "synthesis_status": "task_error",
        "judge_status": "skipped",
        "duration_s": 2.0,
        "error": "backend task crashed",
    }

    assert bench.classify_failure_bucket(record) == "infra_task_error"


def test_summary_emits_failure_bucket_counts_and_burst_count(bench_run) -> None:
    FakeClient.tasks = [None, _task(""), _task("answer ping")]

    run, out_dir = bench_run
    assert run(judge_mode="rule", count=3) == 2

    summary, _records = _read_artifact(out_dir)
    assert summary["failure_bucket_counts"] == {
        "infra_timeout": 1,
        "synthesis_empty": 1,
    }
    assert summary["infra_burst_count"] == 0


def test_no_pause_on_burst_flag_disables_pause(
    bench_run, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(bench.time, "sleep", lambda seconds: sleeps.append(seconds))
    FakeClient.tasks = [None, None, None, None]

    run, out_dir = bench_run
    assert run(
        judge_mode="rule",
        count=4,
        extra_args=["--infra-burst-backoff", "0", "--no-pause-on-burst"],
    ) == 2

    summary, records = _read_artifact(out_dir)
    assert len(records) == 4
    assert sleeps == []
    assert summary["infra_burst_count"] == 1
    assert summary["abort_reason"] is None
    assert "pause-on-burst disabled" in capsys.readouterr().err


def test_multi_source_dataset_requires_source_name_field(
    bench_run, capsys: pytest.CaptureFixture[str]
) -> None:
    run, out_dir = bench_run

    assert run(judge_mode="rule", count=3, source_names=["repo", None, "other"]) == 2

    assert FakeClient.submitted == 0
    assert not list(out_dir.glob("qa-run-*.jsonl"))
    assert "requires source_name on every row" in capsys.readouterr().err


def test_search_passes_source_name_to_backend() -> None:
    captured: dict[str, object] = {}

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"id": "task-1"}

    class DummyHttpClient:
        def post(self, url: str, json: dict[str, object]) -> Response:
            captured["url"] = url
            captured["json"] = json
            return Response()

        def close(self) -> None:
            return None

    client = bench.BenchmarkClient(backend_url="http://backend.test", actor_name="qa")
    client.client = DummyHttpClient()  # type: ignore[assignment]
    row = bench.DatasetRow(
        id="Q01",
        tier="A",
        question="Question?",
        expected_answer_keypoints=["ping"],
        expected_citations=["src/a.py"],
        source_name="hosteddashboard",
    )

    assert client.submit_question(row) == {"id": "task-1"}
    assert captured["json"]["source_name"] == "hosteddashboard"  # type: ignore[index]


def test_diagnostics_compute_expected_citation_rank() -> None:
    row = bench.DatasetRow(
        id="Q01",
        tier="A",
        question="Question?",
        expected_answer_keypoints=["login"],
        expected_citations=["handyman-admin-dashboard/src/pages/Login.js"],
        source_name="hosteddashboard",
    )
    citations = [
        {"source_name": "hosteddashboard", "relative_path": "src/pages/Home.js"},
        {"source_name": "hosteddashboard", "relative_path": "src/pages/Login.js", "card_text": "login form"},
    ]

    diagnostics = bench.compute_retrieval_diagnostics(row, {}, citations)

    assert diagnostics["expected_citation_top_rank"] == 2
    assert diagnostics["expected_fact_in_card"] is True


def test_diagnostics_count_wrong_source_citations() -> None:
    row = bench.DatasetRow(
        id="Q01",
        tier="A",
        question="Question?",
        expected_answer_keypoints=["ping"],
        expected_citations=["src/a.py"],
        source_name="repo",
    )
    citations = [
        {"source_name": "repo", "relative_path": "src/a.py"},
        {"source_name": "other", "relative_path": "src/b.py"},
        {"source_name": "other", "relative_path": "src/c.py"},
    ]

    diagnostics = bench.compute_retrieval_diagnostics(row, {}, citations)

    assert diagnostics["top_k_source_distribution"] == {"repo": 1, "other": 2}
    assert diagnostics["wrong_source_in_top_k"] == 2


def test_buckets_assign_retrieval_wrong_source() -> None:
    record = {
        "tier": "A",
        "source_name": "repo",
        "keypoint_coverage": 1.0,
        "expected_answer_keypoints": ["ping"],
        "answer_excerpt": "ping",
    }
    diagnostics = {
        "expected_citation_top_rank": None,
        "wrong_source_in_top_k": 1,
        "top_k_source_distribution": {"repo": 0, "other": 1},
        "expected_fact_in_card": None,
    }

    assert "retrieval_wrong_source" in bench.assign_stage19_buckets(record, diagnostics)


def test_buckets_assign_retrieval_empty_no_source() -> None:
    record = {
        "tier": "A",
        "source_name": "repo",
        "keypoint_coverage": 1.0,
        "expected_answer_keypoints": ["ping"],
        "answer_excerpt": "ping",
    }
    diagnostics = {
        "expected_citation_top_rank": None,
        "wrong_source_in_top_k": 0,
        "top_k_source_distribution": {},
        "expected_fact_in_card": None,
    }

    buckets = bench.assign_stage19_buckets(record, diagnostics)
    assert "retrieval_empty_no_source" in buckets
    assert "retrieval_wrong_source" not in buckets


def test_buckets_empty_with_right_source_hits_classifies_as_wrong_file() -> None:
    record = {
        "tier": "A",
        "source_name": "repo",
        "keypoint_coverage": 1.0,
        "expected_answer_keypoints": ["ping"],
        "answer_excerpt": "ping",
    }
    diagnostics = {
        "expected_citation_top_rank": None,
        "wrong_source_in_top_k": 0,
        "top_k_source_distribution": {"repo": 3},
        "expected_fact_in_card": None,
    }

    buckets = bench.assign_stage19_buckets(record, diagnostics)
    assert "retrieval_right_source_wrong_file" in buckets
    assert "retrieval_wrong_source" not in buckets
    assert "retrieval_empty_no_source" not in buckets


def test_buckets_assign_card_missing_keypoint_facts() -> None:
    record = {
        "tier": "A",
        "keypoint_coverage": 1.0,
        "expected_answer_keypoints": ["login validates password"],
        "answer_excerpt": "login validates password",
    }
    diagnostics = {
        "expected_citation_top_rank": 1,
        "wrong_source_in_top_k": 0,
        "expected_fact_in_card": False,
    }

    assert bench.assign_stage19_buckets(record, diagnostics) == ["card_missing_keypoint_facts"]


def _task_with_trace(
    answer: str,
    *,
    source_name: str,
    selected_sources: list[str] | None,
    relative_path: str = "src/a.py",
) -> dict[str, object]:
    citations = (
        [{"relative_path": relative_path, "source_name": source_name, "card_text": None}]
        if answer
        else []
    )
    trace = {"selected_sources": selected_sources} if selected_sources is not None else {}
    return {
        "status": "completed",
        "latest_result_json": {
            "result": {"answer": answer, "citations": citations, "answer_trace": trace}
        },
    }


def test_smoke_aborts_on_cross_source_contamination(bench_run) -> None:
    FakeClient.tasks = [
        _task_with_trace(
            "answer ping",
            source_name="handymanapp",
            selected_sources=["hosteddashboard"],
        )
    ]
    run, out_dir = bench_run
    assert run(judge_mode="rule", count=1, source_names=["handymanapp"]) == 2
    summary, _ = _read_artifact(out_dir)
    assert summary["abort_reason"] == "source_filter_broken"


def test_smoke_does_not_abort_on_single_empty_selected_sources(bench_run) -> None:
    FakeClient.tasks = [
        _task_with_trace(
            "answer ping",
            source_name="handymanapp",
            selected_sources=[],
        ),
        _task("answer ping", source_name="handymanapp"),
    ]
    run, out_dir = bench_run
    assert run(judge_mode="rule", count=2, source_names=["handymanapp", "handymanapp"]) == 0
    summary, _ = _read_artifact(out_dir)
    assert summary["abort_reason"] is None


def test_smoke_aborts_on_three_consecutive_empty_selected_sources(bench_run) -> None:
    FakeClient.tasks = [
        _task_with_trace("answer ping", source_name="handymanapp", selected_sources=[]),
        _task_with_trace("answer ping", source_name="handymanapp", selected_sources=[]),
        _task_with_trace("answer ping", source_name="handymanapp", selected_sources=[]),
        _task("answer ping", source_name="handymanapp"),
    ]
    run, out_dir = bench_run
    assert run(
        judge_mode="rule",
        count=4,
        source_names=["handymanapp"] * 4,
    ) == 2
    summary, _ = _read_artifact(out_dir)
    assert summary["abort_reason"] == "systematic_empty_retrieval"


def test_summary_emits_per_source_means(bench_run) -> None:
    FakeClient.tasks = [
        _task("answer ping", source_name="hosteddashboard"),
        _task("answer ping", source_name="handymanapp"),
    ]

    run, out_dir = bench_run
    assert run(
        judge_mode="rule",
        count=2,
        source_names=["hosteddashboard", "handymanapp"],
    ) == 0

    summary, records = _read_artifact(out_dir)
    assert summary["source_summary"] == {
        "handymanapp": {
            "count": 1,
            "valid_score_count": 1,
            "invalid_score_count": 0,
            "mean_score": 100.0,
        },
        "hosteddashboard": {
            "count": 1,
            "valid_score_count": 1,
            "invalid_score_count": 0,
            "mean_score": 100.0,
        },
    }
    assert summary["retrieval_top_rank_distribution"] == {"top1": 2}
    assert summary["stage19_bucket_counts"] == {}
    assert records[0]["source_name"] == "hosteddashboard"
