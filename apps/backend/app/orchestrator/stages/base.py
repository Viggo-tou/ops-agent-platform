"""Stage base contract for the develop pipeline.

A Stage is a typed phase of the pipeline. Each Stage:

1. Declares its required inputs via ``StageContext`` (typed) — never
   reads the giant shared ``pipeline_state`` directly.
2. Returns a ``StageResult`` with one of three outcomes:
     - CONTINUE: proceed to next stage with updated context
     - PARK: park the task (e.g. AWAITING_APPROVAL) and return control
       to the caller; orchestrator handles persistence
     - FAIL: terminate the pipeline with an error message
3. Does not own ``self.db``. Lifecycle events are emitted by the
   orchestrator wrapping the stage call so DB writes stay on the main
   thread (consistent with today's commit_checkpoint architecture).

Phase 1 (this commit) defines the contract; Phase 2 will migrate
existing pipeline phases (semantic_translation, planning, review,
action, gate_battery, approval, writeback) into Stage subclasses
incrementally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StageOutcome(str, Enum):
    """How a stage result should be handled by the orchestrator."""

    CONTINUE = "continue"
    PARK = "park"  # task reaches an awaiting_approval-style hold
    FAIL = "fail"


@dataclass(frozen=True)
class StageContext:
    """Typed container for everything a stage may read.

    Stages should NOT receive arbitrary state. New keys go through this
    typed surface, which forces author intent into the type system
    (rather than the current pattern of pipeline_state["arbitrary"]).
    """

    task_id: str
    actor_name: str
    request_text: str
    scenario: str
    plan: Any | None = None  # GeneratedPlan; Any to dodge circular import
    diff: str = ""
    files_changed: list[str] = field(default_factory=list)
    sandbox_dir: str | None = None
    pipeline_state: dict[str, Any] = field(default_factory=dict)
    # Stages that read approval state populate these as needed.
    approval_id: str | None = None
    approval_action: str | None = None


@dataclass(frozen=True)
class StageResult:
    """Result of one stage. The orchestrator inspects ``outcome`` and:

    - CONTINUE: merges produced into the running StageContext for the
      next stage.
    - PARK: persists the produced metadata and returns control to the
      caller; resume_after_approval will re-enter the next stage.
    - FAIL: calls _fail_develop_pipeline with the failure message.
    """

    stage_name: str
    outcome: StageOutcome
    produced: dict[str, Any] = field(default_factory=dict)
    fail_message: str = ""
    park_message: str = ""
    park_payload: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage_name,
            "outcome": self.outcome.value,
            "produced_keys": list(self.produced.keys()),
            "fail_message": self.fail_message,
            "park_message": self.park_message,
        }


class Stage(ABC):
    """Abstract base for a develop-pipeline stage.

    Implementations override ``STAGE_NAME`` and ``run``.
    """

    STAGE_NAME: str = ""

    @property
    def name(self) -> str:
        return self.STAGE_NAME or self.__class__.__name__

    @abstractmethod
    def run(self, ctx: StageContext) -> StageResult:
        ...
