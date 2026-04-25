"""ArtifactExistenceGate: planner-declared files must really land in sandbox.

Wraps the existing functional service (``services.artifact_existence``)
in the new Gate ABC. The orchestrator can keep calling the function
directly today; this class is the migration target — once a few more
gates are ported, the orchestrator's review-stage block will iterate a
gate registry instead of open-coding each call.
"""

from __future__ import annotations

from app.orchestrator.gates.base import (
    Finding,
    Gate,
    GateContext,
    GateReport,
    GateVerdict,
)


class ArtifactExistenceGate(Gate):
    """Verify that ``must_touch_files`` and ``expected_new_files`` from
    the planner actually exist in the sandbox after the patch was
    applied. Closes the gap where scope-lock filtering or merge logic
    silently drops a core deliverable file (e.g. database.rules.json
    for the P69-8 task) and every other gate passes on the remaining
    cosmetic changes.

    Behaviour matches the existing
    ``services.artifact_existence.check_artifact_existence`` function;
    this class just adapts the report to the canonical Gate shape.
    """

    GATE_NAME = "artifact_existence.check"

    def run(self, ctx: GateContext) -> GateReport:
        from app.services.artifact_existence import check_artifact_existence

        if ctx.sandbox_dir is None:
            return GateReport(
                gate_name=self.name,
                verdict=GateVerdict.PASS,
                metadata={"skipped_reason": "sandbox_dir not provided"},
            )

        report = check_artifact_existence(
            sandbox_dir=ctx.sandbox_dir,
            must_touch_files=ctx.must_touch_files,
            expected_new_files=ctx.expected_new_files,
            diff_touched_paths=ctx.diff_touched_paths,
        )
        findings = [
            Finding(
                file=f.file,
                severity=f.severity,
                rule=f.rule,
                message=f.message,
            )
            for f in report.findings
        ]
        return GateReport(
            gate_name=self.name,
            verdict=Gate.derive_verdict(findings),
            findings=findings,
            metadata={
                "checked_must_touch": report.checked_must_touch,
                "checked_expected_new": report.checked_expected_new,
            },
        )
