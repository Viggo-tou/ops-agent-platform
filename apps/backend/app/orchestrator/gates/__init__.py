"""Gate abstractions for the develop pipeline review stage.

A *gate* is a single check applied to a generated diff (or to the
sandbox state after the diff is applied) that produces a verdict and a
list of findings. Gates are deliberately small and composable — the
orchestrator iterates a registry of gates rather than open-coding each
check inline.

See ``base.py`` for the contract and ``../gates/README.md`` for the
migration plan.
"""

from app.orchestrator.gates.base import (
    Finding,
    Gate,
    GateContext,
    GateReport,
    GateVerdict,
)

__all__ = [
    "Finding",
    "Gate",
    "GateContext",
    "GateReport",
    "GateVerdict",
]
