from __future__ import annotations

from app.services.planner_context import render_planner_context_packet


def test_render_planner_context_packet_has_stable_sections() -> None:
    packet = render_planner_context_packet(
        original_request="finish Jira P69-19",
        translation_document={
            "normalized_request": "Implement map-based address selection.",
            "intent": "develop_jira_issue",
            "search_queries": ["CustomerSignup map address"],
        },
        issue_context={
            "issue_key": "P69-19",
            "summary": "Map based address selection",
            "description": "Use OSMDroid in signup and KYC address forms.",
            "acceptance_criteria": "No Google Maps APIs.",
        },
        planning_knowledge_context={
            "answer": "CustomerSignup.kt contains the signup form.",
            "citations": [
                {
                    "relative_path": "app/src/main/java/com/example/CustomerSignup.kt",
                    "line_start": 12,
                    "line_end": 40,
                    "snippet": "fun CustomerSignup(...)",
                }
            ],
            "answer_trace": {"selected_sources": ["handymanapp"]},
        },
    )

    assert packet.startswith('<planner_context version="1">')
    assert "<user_request>" in packet
    assert '<semantic_translation format="json">' in packet
    assert '<jira_issue_context format="json">' in packet
    assert '<repository_context role="read_only_grounding" format="json">' in packet
    assert "<planner_focus>" in packet
    assert "P69-19" in packet
    assert "CustomerSignup.kt" in packet
    assert "Do not invent helper files" in packet


def test_render_planner_context_packet_trims_large_issue_description() -> None:
    packet = render_planner_context_packet(
        original_request="implement issue",
        issue_context={
            "issue_key": "OPS-1",
            "description": "x" * 3000,
        },
    )

    assert "x" * 1500 not in packet
    assert "..." in packet
    assert "OPS-1" in packet

