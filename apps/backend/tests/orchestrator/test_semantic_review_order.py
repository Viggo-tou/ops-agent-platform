"""Verify semantic_review runs before runtime_validation and diff_reviewer."""
from pathlib import Path

import pytest


SOURCE_PATH = Path(__file__).resolve().parents[2] / "app" / "orchestrator" / "service.py"


class TestSemanticReviewPipelineOrder:
    """Verify the semantic review gate ordering in the pipeline.

    These are structural tests that verify the code ordering, not full
    integration tests.
    """

    @pytest.fixture(autouse=True)
    def _load_source(self) -> None:
        self.source = SOURCE_PATH.read_text(encoding="utf-8")

    def test_semantic_review_before_runtime_validation(self) -> None:
        """Semantic review should be positioned before runtime_validation in the pipeline."""
        sem_review_pos = self.source.find("semantic_review_done")
        runtime_val_pos = self.source.find("runtime_validation_done")

        assert sem_review_pos > 0, "semantic_review_done not found in orchestrator"
        assert runtime_val_pos > 0, "runtime_validation_done not found in orchestrator"
        assert sem_review_pos < runtime_val_pos, (
            "semantic_review should appear BEFORE runtime_validation in the pipeline"
        )

    def test_semantic_review_before_diff_reviewer(self) -> None:
        """Semantic review should be positioned before diff_reviewer."""
        sem_review_pos = self.source.find("semantic_review_done")
        diff_reviewer_pos = self.source.find("diff_reviewer.review")

        assert sem_review_pos > 0
        assert diff_reviewer_pos > 0
        assert sem_review_pos < diff_reviewer_pos, (
            "semantic_review should appear BEFORE diff_reviewer in the pipeline"
        )

    def test_semantic_review_after_compile_gate(self) -> None:
        """Semantic review should be positioned after compile_gate."""
        compile_done_pos = self.source.find('pipeline_state["compile_gate_done"] = True')
        sem_review_pos = self.source.find("semantic_review_done")

        assert compile_done_pos > 0, "compile_gate_done assignment not found"
        assert sem_review_pos > 0
        assert compile_done_pos < sem_review_pos, (
            "semantic_review should appear AFTER compile_gate_done in the pipeline"
        )

    def test_max_semantic_review_attempts_defined(self) -> None:
        """MAX_SEMANTIC_REVIEW_ATTEMPTS constant should be defined."""
        assert "MAX_SEMANTIC_REVIEW_ATTEMPTS" in self.source

    def test_iterate_skipped_mode(self) -> None:
        """Semantic review iterate should log-and-skip, not spawn LLM repair."""
        assert "incremental repair disabled" in self.source, (
            "Semantic review iterate should log that incremental repair is disabled"
        )
        # Verify we do NOT call _reset_for_conformance_retry from semantic review
        # Find the semantic review gate block
        sem_start = self.source.find("# --- Semantic review gate")
        runtime_start = self.source.find("# --- Runtime validation gate")
        assert sem_start > 0
        assert runtime_start > 0
        sem_block = self.source[sem_start:runtime_start]
        assert "_reset_for_conformance_retry" not in sem_block, (
            "Semantic review should not call _reset_for_conformance_retry"
        )
        # Verify no codegen.generate_patch call in the semantic review block
        assert "codegen.generate_patch" not in sem_block, (
            "Semantic review iterate should not invoke codegen.generate_patch"
        )

    def test_semantic_review_clears_on_conformance_retry(self) -> None:
        """When conformance retry resets the pipeline, semantic_review_done should be cleared."""
        reset_pos = self.source.find("def _reset_for_conformance_retry")
        assert reset_pos > 0
        reset_block = self.source[reset_pos:reset_pos + 1500]
        assert '"semantic_review_done"' in reset_block, (
            "semantic_review_done should be cleared by _reset_for_conformance_retry"
        )
