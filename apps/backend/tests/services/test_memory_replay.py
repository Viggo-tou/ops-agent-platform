from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.codegen import CodeGenerator, MEMORY_PROMPT_INSTRUCTION


SCENARIOS = [
    (
        "P69-19-v1",
        "Fix evidence-chain closure for modified files.",
        "Evidence chain failed when a changed file had no citation.",
        "Add citations for every changed file before evidence_chain.check runs.",
    ),
    (
        "P69-19-v2",
        "Repair compile gate after generated package changes.",
        "Compile gate failed because package.json was malformed after codegen.",
        "Validate package.json syntax before rerunning compile_gate.",
    ),
    (
        "P69-10",
        "Keep must_touch_files enforced during retry.",
        "Codegen retry modified a file outside must_touch_files.",
        "Retry prompts must repeat the allowed file set and avoid helper rewrites.",
    ),
    (
        "P69-8",
        "Avoid comment-only patches for destructive requests.",
        "Goal decomposition blocked a comment-only change on an unjustified file.",
        "Make the smallest behavior change in the declared target file.",
    ),
    (
        "P69-7",
        "Handle runtime validation findings without broad rewrites.",
        "Runtime validation failed after a broad generated rewrite changed unrelated behavior.",
        "Apply a narrow semantic repair to only the failing branch.",
    ),
]


def _memory_context(observation: str, resolution: str) -> str:
    blocks: list[str] = []
    for index in range(1, 4):
        blocks.extend(
            [
                (
                    "[memory:gate_failure_resolution / scope:gate:compile_gate / "
                    f"used {index}x / confidence 1.0 / from task replay-{index}]"
                ),
                f"Observation: {observation}",
                f"Resolution: {resolution}",
            ]
        )
    return "\n".join(blocks)


def _prompt(*, memory_context: str | None) -> str:
    plan_json = {
        "objective": "Implement the replay task.",
        "change_summary": "Modify the declared target file.",
        "must_touch_files": ["src/App.js"],
        "expected_new_files": [],
        "steps": [{"title": "Patch target", "expected_output": "src/App.js updated"}],
    }
    if memory_context:
        plan_json["memory_context"] = memory_context
    return CodeGenerator(Settings(memory_max_lines_in_prompt=30))._build_prompt(
        plan_json=plan_json,
        context_files={"src/App.js": "export function App() { return null; }\n"},
        task_description="Update src/App.js only.",
        json_mode=True,
    )


@pytest.mark.parametrize(
    ("scenario_id", "task_request", "observation", "resolution"),
    SCENARIOS,
)
def test_replay_prompt_injects_expected_memory_patterns(
    scenario_id: str,
    task_request: str,
    observation: str,
    resolution: str,
) -> None:
    del scenario_id, task_request
    without_memory = _prompt(memory_context=None)
    with_memory = _prompt(memory_context=_memory_context(observation, resolution))

    assert with_memory != without_memory
    assert "===== Prior gate failure patterns =====" in with_memory
    assert "===== End memory =====" in with_memory
    assert MEMORY_PROMPT_INSTRUCTION in with_memory
    assert observation in with_memory
    assert resolution in with_memory
    assert observation not in without_memory
    assert with_memory.index("=== ALLOWED FILES") < with_memory.index("===== Prior gate failure patterns =====")
    assert with_memory.index("===== Prior gate failure patterns =====") < with_memory.index("=== PLAN STEPS ===")
