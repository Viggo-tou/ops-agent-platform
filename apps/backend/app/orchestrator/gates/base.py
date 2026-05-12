"""Gate base contract.

Every review-stage check (compile_gate, runtime_validation, spec_conformance,
goal_decomposition, symbol_reference, artifact_existence, comment_only,
diff_shape, ...) follows the same shape:

    inputs  : (diff, plan, sandbox_dir, optional context)
    outputs : a GateReport with verdict (pass | warn | block) + findings

Today most of these live as free functions imported lazily inside
``orchestrator/service.py``. The plan is to migrate them one at a time
to subclasses of ``Gate``, and have the orchestrator iterate a registry
instead of open-coding the calls. Phase 1 (this commit) defines the
contract; Phase 2 migrates gates incrementally without breaking the
existing inline callsites.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class GateVerdict(str, Enum):
    """How a gate result should affect the pipeline."""

    PASS = "pass"
    WARN = "warn"
    BLOCK = "block"


@dataclass(frozen=True)
class Finding:
    """A single issue surfaced by a gate.

    severity drives the verdict aggregation:
    - "block" findings -> gate.verdict = BLOCK
    - "warn" findings only -> gate.verdict = WARN
    - no findings -> gate.verdict = PASS

    Most existing gates already shape findings around (file, severity,
    rule, message); this dataclass is the canonical form.
    """

    file: str | None
    severity: str  # "block" | "warn" | "info"
    rule: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class GateContext:
    """Inputs every gate sees.

    Lifted out of the giant pipeline_state dict so gates have a typed
    handle on what they're allowed to read. Optional fields stay None
    when irrelevant (e.g. compile_gate doesn't need the planner's
    expected_new_files).
    """

    task_id: str
    diff: str
    sandbox_dir: Path | None
    plan: Any | None  # GeneratedPlan; kept Any to avoid circular import
    must_touch_files: list[str] = field(default_factory=list)
    expected_new_files: list[str] = field(default_factory=list)
    diff_touched_paths: set[str] = field(default_factory=set)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateReport:
    """Result of a gate check.

    The verdict is computed from findings (most-severe wins). Persisted
    as the payload of the ``TOOL_SUCCEEDED`` / ``REVIEW_FAILED`` event so
    the frontend can render per-gate detail without bespoke handling.
    """

    gate_name: str
    verdict: GateVerdict
    findings: list[Finding] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "block"]

    @property
    def warn_findings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def to_payload(self) -> dict[str, Any]:
        return {
            "gate": self.gate_name,
            "verdict": self.verdict.value,
            "findings_total": len(self.findings),
            "findings_blocking": len(self.blocking_findings),
            "findings_warn": len(self.warn_findings),
            "findings": [f.to_payload() for f in self.findings],
            "metadata": self.metadata,
        }


class Gate(ABC):
    """Abstract base for a single review-stage gate.

    Implementations override ``name`` (or class attr ``GATE_NAME``) and
    ``run``. The orchestrator's gate dispatcher will:

    1. Build a GateContext once per pipeline run.
    2. For each registered gate, call ``gate.run(ctx)``.
    3. Persist the GateReport as a TOOL_SUCCEEDED / REVIEW_FAILED event.
    4. If verdict is BLOCK, fail the develop pipeline with a clear
       message and the gate's payload.
    """

    GATE_NAME: str = ""  # subclasses set this; falls back to class name

    @property
    def name(self) -> str:
        return self.GATE_NAME or self.__class__.__name__

    @abstractmethod
    def run(self, ctx: GateContext) -> GateReport:
        ...

    @staticmethod
    def derive_verdict(findings: list[Finding]) -> GateVerdict:
        """Most-severe-wins aggregation. Use this in implementations to
        avoid drift between gates on what 'block' means.
        """
        if any(f.severity == "block" for f in findings):
            return GateVerdict.BLOCK
        if any(f.severity == "warn" for f in findings):
            return GateVerdict.WARN
        return GateVerdict.PASS
