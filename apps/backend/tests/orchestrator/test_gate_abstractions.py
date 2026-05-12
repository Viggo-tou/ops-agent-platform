"""Tests for Gate / Stage abstractions and the two ported exemplar gates."""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.orchestrator.gates import (  # noqa: E402
    Finding,
    Gate,
    GateContext,
    GateReport,
    GateVerdict,
)
from app.orchestrator.gates.artifact_existence_gate import ArtifactExistenceGate  # noqa: E402
from app.orchestrator.gates.comment_only_gate import CommentOnlyGate  # noqa: E402
from app.orchestrator.stages import (  # noqa: E402
    Stage,
    StageContext,
    StageOutcome,
    StageResult,
)


def test_derive_verdict_block_wins() -> None:
    findings = [
        Finding(file=None, severity="warn", rule="x", message="w"),
        Finding(file=None, severity="block", rule="y", message="b"),
        Finding(file=None, severity="info", rule="z", message="i"),
    ]
    assert Gate.derive_verdict(findings) == GateVerdict.BLOCK


def test_derive_verdict_warn_when_no_block() -> None:
    findings = [
        Finding(file=None, severity="warn", rule="x", message="w"),
        Finding(file=None, severity="info", rule="z", message="i"),
    ]
    assert Gate.derive_verdict(findings) == GateVerdict.WARN


def test_derive_verdict_pass_on_no_findings() -> None:
    assert Gate.derive_verdict([]) == GateVerdict.PASS


def test_gate_report_to_payload_shape() -> None:
    report = GateReport(
        gate_name="x.check",
        verdict=GateVerdict.WARN,
        findings=[
            Finding(file="a.py", severity="warn", rule="r1", message="m1"),
            Finding(file="b.py", severity="block", rule="r2", message="m2"),
        ],
    )
    p = report.to_payload()
    assert p["gate"] == "x.check"
    assert p["verdict"] == "warn"
    assert p["findings_total"] == 2
    assert p["findings_blocking"] == 1
    assert p["findings_warn"] == 1
    assert len(p["findings"]) == 2


def test_artifact_existence_gate_skips_when_no_sandbox(tmp_path: Path) -> None:
    ctx = GateContext(
        task_id="t1",
        diff="",
        sandbox_dir=None,  # no sandbox -> skip
        plan=None,
        must_touch_files=["src/a.py"],
        expected_new_files=["src/b.py"],
    )
    report = ArtifactExistenceGate().run(ctx)
    assert report.verdict == GateVerdict.PASS
    assert report.findings == []
    assert report.metadata["skipped_reason"] == "sandbox_dir not provided"


def test_artifact_existence_gate_blocks_on_missing_new_file(tmp_path: Path) -> None:
    ctx = GateContext(
        task_id="t1",
        diff="",
        sandbox_dir=tmp_path,
        plan=None,
        must_touch_files=[],
        expected_new_files=["database.rules.json"],
    )
    report = ArtifactExistenceGate().run(ctx)
    assert report.verdict == GateVerdict.BLOCK
    assert any(
        f.rule == "missing_expected_new_file" for f in report.findings
    ), [f.rule for f in report.findings]


def test_comment_only_gate_passes_when_unjustified_empty() -> None:
    ctx = GateContext(
        task_id="t1",
        diff="",
        sandbox_dir=None,
        plan=None,
        extra={},  # no unjustified_files
    )
    report = CommentOnlyGate().run(ctx)
    assert report.verdict == GateVerdict.PASS
    assert report.metadata["unjustified_count"] == 0


def test_comment_only_gate_blocks_on_comment_only_unjustified() -> None:
    diff = """diff --git a/src/a.js b/src/a.js
--- a/src/a.js
+++ b/src/a.js
@@ -1,2 +1,3 @@
 const x = 1;
+// just a self-documenting comment
 const y = 2;
"""
    ctx = GateContext(
        task_id="t1",
        diff=diff,
        sandbox_dir=None,
        plan=None,
        extra={"unjustified_files": ["src/a.js"]},
    )
    report = CommentOnlyGate().run(ctx)
    assert report.verdict == GateVerdict.BLOCK
    assert any(f.rule == "comment_only_unjustified" for f in report.findings)


def test_stage_result_payload_shape() -> None:
    res = StageResult(
        stage_name="planning",
        outcome=StageOutcome.CONTINUE,
        produced={"plan": "x", "review": "y"},
    )
    p = res.to_payload()
    assert p["stage"] == "planning"
    assert p["outcome"] == "continue"
    assert sorted(p["produced_keys"]) == ["plan", "review"]


def test_stage_subclass_must_implement_run() -> None:
    class Concrete(Stage):
        STAGE_NAME = "test_stage"

        def run(self, ctx: StageContext) -> StageResult:
            return StageResult(stage_name=self.name, outcome=StageOutcome.CONTINUE)

    s = Concrete()
    ctx = StageContext(
        task_id="t1", actor_name="tester", request_text="x", scenario="process_question"
    )
    res = s.run(ctx)
    assert res.outcome == StageOutcome.CONTINUE
    assert res.stage_name == "test_stage"
