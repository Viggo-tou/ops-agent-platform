"""Stage abstractions for the develop pipeline.

The develop pipeline today is a single ~3000-line method
``_execute_develop_pipeline`` that manipulates a shared
``pipeline_state`` dict through several procedural phases (planning,
review, action, gates, approval, writeback). Each phase is implicit;
adding a new check or reordering a step requires reading the entire
method.

A *stage* is a typed boundary between phases. Each stage:
  * declares its prerequisites and produced artefacts
  * receives a typed StageContext and returns a typed StageResult
  * may emit lifecycle events but does not own ``self.db``

Phase 1 (this commit) defines the contract. Phase 2 will migrate
phases out of the monolith one at a time, validating that an
identical run produces identical events / DB state at every step.

See ``base.py`` for the contract.
"""

from app.orchestrator.stages.base import (
    Stage,
    StageContext,
    StageOutcome,
    StageResult,
)

__all__ = [
    "Stage",
    "StageContext",
    "StageOutcome",
    "StageResult",
]
