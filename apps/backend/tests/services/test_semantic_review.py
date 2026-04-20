"""Tests for the semantic_review service."""

from __future__ import annotations

import json

import pytest

from app.services.semantic_review import (
    ReviewGap,
    SemanticReviewReport,
    _build_review_prompt,
    _parse_review_response,
)


class TestParseReviewResponse:
    def test_plain_json(self):
        raw = json.dumps({
            "verdict": "pass",
            "covered": ["role renaming"],
            "gaps": [],
            "summary": "All good.",
        })
        result = _parse_review_response(raw)
        assert result["verdict"] == "pass"
        assert result["covered"] == ["role renaming"]
        assert result["gaps"] == []

    def test_json_with_markdown_fences(self):
        raw = '```json\n{"verdict": "iterate", "covered": [], "gaps": [{"category": "edge_case", "description": "missing try/catch", "severity": "required"}], "summary": "Needs work."}\n```'
        result = _parse_review_response(raw)
        assert result["verdict"] == "iterate"
        assert len(result["gaps"]) == 1
        assert result["gaps"][0]["description"] == "missing try/catch"

    def test_json_embedded_in_prose(self):
        raw = 'Here is my review:\n{"verdict": "pass", "covered": ["a"], "gaps": [], "summary": "ok"}\nEnd.'
        result = _parse_review_response(raw)
        assert result["verdict"] == "pass"

    def test_invalid_json_returns_empty(self):
        raw = "This is not JSON at all."
        result = _parse_review_response(raw)
        assert result == {}

    def test_empty_string(self):
        result = _parse_review_response("")
        assert result == {}


class TestBuildReviewPrompt:
    def test_basic_prompt(self):
        prompt = _build_review_prompt(
            task_description="Rename Master Admin to Admin",
            normalized_request=None,
            diff="diff --git a/file.js b/file.js\n-Master Admin\n+Admin",
            source_files=None,
            previous_gaps=None,
        )
        assert "## Task Description" in prompt
        assert "Rename Master Admin to Admin" in prompt
        assert "## Generated Diff" in prompt
        assert "-Master Admin" in prompt

    def test_with_normalized_request(self):
        prompt = _build_review_prompt(
            task_description="P69-10",
            normalized_request="Rename role values from Master Admin/Staff Member to Admin/Staff",
            diff="diff",
            source_files=None,
            previous_gaps=None,
        )
        assert "## Normalized Request" in prompt
        assert "Rename role values" in prompt

    def test_with_source_files(self):
        prompt = _build_review_prompt(
            task_description="task",
            normalized_request=None,
            diff="diff",
            source_files={"src/Login.js": "import React from 'react';"},
            previous_gaps=None,
        )
        assert "## Original Source Files" in prompt
        assert "src/Login.js" in prompt

    def test_with_previous_gaps(self):
        prompt = _build_review_prompt(
            task_description="task",
            normalized_request=None,
            diff="diff",
            source_files=None,
            previous_gaps=["handleEdit missing normalize", "filter not case-insensitive"],
        )
        assert "## Previous Review Feedback" in prompt
        assert "handleEdit missing normalize" in prompt


class TestSemanticReviewReport:
    def test_to_payload(self):
        report = SemanticReviewReport(
            verdict="iterate",
            covered=["role renaming"],
            gaps=[
                ReviewGap(
                    category="edge_case",
                    description="handleEdit missing normalize",
                    file_hint="AdminSettings.js",
                    severity="required",
                ),
                ReviewGap(
                    category="defensive_coding",
                    description="JSON.parse should have try/catch",
                    severity="suggested",
                ),
            ],
            summary="Core renaming done but edge cases missed.",
        )
        payload = report.to_payload()
        assert payload["verdict"] == "iterate"
        assert len(payload["gaps"]) == 2
        assert payload["gaps"][0]["file_hint"] == "AdminSettings.js"

    def test_gap_feedback_lines_only_required(self):
        report = SemanticReviewReport(
            verdict="iterate",
            covered=[],
            gaps=[
                ReviewGap(
                    category="edge_case",
                    description="handleEdit missing",
                    file_hint="AdminSettings.js",
                    severity="required",
                ),
                ReviewGap(
                    category="defensive_coding",
                    description="nice to have",
                    severity="suggested",
                ),
            ],
            summary="",
        )
        lines = report.gap_feedback_lines()
        assert len(lines) == 1
        assert "[REQUIRED]" in lines[0]
        assert "handleEdit missing" in lines[0]
        assert "AdminSettings.js" in lines[0]

    def test_pass_report_no_gaps(self):
        report = SemanticReviewReport(
            verdict="pass",
            covered=["everything"],
            gaps=[],
            summary="All requirements met.",
        )
        assert report.to_payload()["verdict"] == "pass"
        assert report.gap_feedback_lines() == []
