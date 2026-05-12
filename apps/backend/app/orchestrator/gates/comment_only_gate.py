"""CommentOnlyGate: escalate "unjustified + comment-only" files to block.

Wraps the existing classifier (``services.comment_only_detector``) into
the Gate ABC. The orchestrator currently calls
``classify_diff(diff)`` inline inside its goal_decomposition block;
this class is the migration target so future gate orchestration can
treat comment-only escalation as just another Gate in the registry.

Note: this gate operates on goal_decomposition's ``unjustified_files``
output, so the orchestrator must run goal_decomposition first and
pass that list via ctx.extra["unjustified_files"] before invoking
this gate. The migration plan moves the goal_decomposition call into
its own Gate subclass and chains them through a GateRunner that
respects declared dependencies.
"""

from __future__ import annotations

from app.orchestrator.gates.base import (
    Finding,
    Gate,
    GateContext,
    GateReport,
    GateVerdict,
)


class CommentOnlyGate(Gate):
    """Block when goal_decomposition flagged a file as unjustified AND
    that file's diff hunks are comment-only (all + lines are comments
    or whitespace).

    Catches the "CLI agent added a self-documenting note to placate
    the review" pattern observed on task 5de6b5d3 (a P69-8 patch that
    shipped with nothing but a comment claiming the work was done).
    """

    GATE_NAME = "comment_only.escalation"

    def run(self, ctx: GateContext) -> GateReport:
        from app.services.comment_only_detector import classify_diff

        unjustified = list(ctx.extra.get("unjustified_files") or [])
        if not unjustified:
            return GateReport(
                gate_name=self.name,
                verdict=GateVerdict.PASS,
                metadata={"unjustified_count": 0},
            )

        comment_reports = classify_diff(ctx.diff)
        comment_only_unjustified: list[str] = []
        findings: list[Finding] = []

        for unjf in unjustified:
            for rep_path, rep in comment_reports.items():
                if not rep.is_comment_only:
                    continue
                if (
                    rep_path == unjf
                    or rep_path.endswith("/" + unjf)
                    or unjf.endswith("/" + rep_path)
                ):
                    comment_only_unjustified.append(rep_path)
                    findings.append(
                        Finding(
                            file=rep_path,
                            severity="block",
                            rule="comment_only_unjustified",
                            message=(
                                f"File '{rep_path}' was modified but the changes "
                                f"are comment-only (no executable code touched), "
                                f"and goal_decomposition could not justify the "
                                f"file against any task goal."
                            ),
                            evidence={
                                "added_lines": rep.added_lines,
                                "added_comment_lines": rep.added_comment_lines,
                                "added_code_lines": rep.added_code_lines,
                                "removed_lines": rep.removed_lines,
                            },
                        )
                    )
                    break

        return GateReport(
            gate_name=self.name,
            verdict=Gate.derive_verdict(findings),
            findings=findings,
            metadata={
                "unjustified_count": len(unjustified),
                "comment_only_unjustified": comment_only_unjustified,
            },
        )
