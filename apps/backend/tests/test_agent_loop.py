"""Tests for the multi-turn agent loop (Tier 4 main course).

Tool protocol parsing, the four core tools (read_file / search_symbol /
list_directory / apply_diff), and the loop driver — using a stub LLM
function so no real network call is made.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.agent_loop import (
    AgentLoopBudget,
    AgentLoopContext,
    ToolCall,
    ToolResult,
    _DEFAULT_TOOLS,
    parse_model_response,
    render_tool_result_for_prompt,
    render_system_prompt,
    run_agent_loop,
)


# --- protocol parsing -------------------------------------------------------


def test_parse_basic_tool_call():
    text = """\
## TOOL_CALL
```json
{"tool": "read_file", "args": {"path": "x.py"}}
```
"""
    call, terminal = parse_model_response(text)
    assert terminal == ""
    assert call is not None
    assert call.tool == "read_file"
    assert call.args == {"path": "x.py"}


def test_parse_done_terminal():
    text = "## DONE — patch applied successfully"
    call, terminal = parse_model_response(text)
    assert terminal == "done"
    assert call is None


def test_parse_cannot_proceed():
    text = "## CANNOT_PROCEED: source already implements behaviour"
    call, terminal = parse_model_response(text)
    assert terminal == "cannot_proceed"


def test_parse_no_tool_call_no_terminal():
    text = "I think we should look at foo.py first."
    call, terminal = parse_model_response(text)
    assert terminal == ""
    assert call is None


def test_parse_invalid_json_returns_none():
    text = "## TOOL_CALL\n```json\n{not valid json\n```"
    call, terminal = parse_model_response(text)
    assert call is None


def test_parse_terminal_takes_precedence_over_tool_call():
    text = """\
## DONE
## TOOL_CALL
```json
{"tool": "read_file", "args": {}}
```
"""
    call, terminal = parse_model_response(text)
    assert terminal == "done"


# --- read_file --------------------------------------------------------------


def test_read_file_from_candidate(tmp_path):
    ctx = AgentLoopContext(
        sandbox_dir=tmp_path,
        repo_root=None,
        candidate_files={"foo.py": "def f():\n    return 1\n"},
    )
    result = _DEFAULT_TOOLS["read_file"].handler({"path": "foo.py"}, ctx)
    assert result.ok
    assert "def f()" in result.content["text"]


def test_read_file_from_disk_when_not_in_candidates(tmp_path):
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "m.py").write_text("def needle(): pass\n", encoding="utf-8")
    ctx = AgentLoopContext(
        sandbox_dir=tmp_path,
        repo_root=tmp_path,
        candidate_files={},
    )
    result = _DEFAULT_TOOLS["read_file"].handler({"path": "pkg/m.py"}, ctx)
    assert result.ok
    assert "def needle()" in result.content["text"]


def test_read_file_path_traversal_blocked(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "secret.py").write_text("API_KEY='leak'\n", encoding="utf-8")
    ctx = AgentLoopContext(
        sandbox_dir=repo,
        repo_root=repo,
        candidate_files={},
    )
    result = _DEFAULT_TOOLS["read_file"].handler({"path": "../secret.py"}, ctx)
    assert not result.ok
    assert "not found" in result.error.lower()


def test_read_file_line_range():
    content = "\n".join(f"line_{i}" for i in range(1, 21)) + "\n"
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"big.py": content},
    )
    result = _DEFAULT_TOOLS["read_file"].handler(
        {"path": "big.py", "line_start": 5, "line_end": 7}, ctx
    )
    assert result.ok
    assert "line_5" in result.content["text"]
    assert "line_7" in result.content["text"]
    assert "line_8" not in result.content["text"]


def test_read_file_truncates_oversize():
    big = "x" * 20_000
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"x.py": big},
        max_read_bytes=1_000,
    )
    result = _DEFAULT_TOOLS["read_file"].handler({"path": "x.py"}, ctx)
    assert result.ok
    assert len(result.content["text"]) <= 1_200  # 1000 + truncation marker
    assert "truncated" in result.content["text"]


# --- search_symbol ----------------------------------------------------------


def test_search_symbol_finds_in_candidates():
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={
            "a.py": "def needle(): pass\n",
            "b.py": "x = 1\n",
        },
    )
    result = _DEFAULT_TOOLS["search_symbol"].handler({"name": "needle"}, ctx)
    assert result.ok
    paths = {h["path"] for h in result.content["hits"]}
    assert "a.py" in paths


def test_search_symbol_missing_arg():
    ctx = AgentLoopContext(sandbox_dir=None, repo_root=None, candidate_files={})
    result = _DEFAULT_TOOLS["search_symbol"].handler({}, ctx)
    assert not result.ok


def test_search_symbol_walks_repo_root(tmp_path):
    sub = tmp_path / "deep"
    sub.mkdir()
    (sub / "m.py").write_text("def found_me(): pass\n", encoding="utf-8")
    ctx = AgentLoopContext(
        sandbox_dir=tmp_path,
        repo_root=tmp_path,
        candidate_files={},
    )
    result = _DEFAULT_TOOLS["search_symbol"].handler({"name": "found_me"}, ctx)
    assert result.ok
    paths = [h["path"] for h in result.content["hits"]]
    assert any("deep/m.py" in p for p in paths)


# --- list_directory ---------------------------------------------------------


def test_list_directory_basic(tmp_path):
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    (tmp_path / "subdir").mkdir()
    ctx = AgentLoopContext(sandbox_dir=tmp_path, repo_root=None, candidate_files={})
    result = _DEFAULT_TOOLS["list_directory"].handler({"path": "."}, ctx)
    assert result.ok
    names = {e["name"] for e in result.content["entries"]}
    assert "a.py" in names
    assert "subdir" in names


def test_list_directory_blocks_traversal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    ctx = AgentLoopContext(sandbox_dir=repo, repo_root=repo, candidate_files={})
    result = _DEFAULT_TOOLS["list_directory"].handler({"path": "../"}, ctx)
    assert not result.ok


# --- apply_diff -------------------------------------------------------------


def test_apply_diff_produces_unified_diff():
    src = "def f():\n    return 1\n"
    blocks_text = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "def f():\n"
        "    return 1\n"
        "=======\n"
        "def f():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"m.py": src},
    )
    result = _DEFAULT_TOOLS["apply_diff"].handler({"blocks": blocks_text}, ctx)
    assert result.ok
    diff = result.content["unified_diff"]
    assert "diff --git a/m.py b/m.py" in diff
    assert "+    return 2" in diff


def test_apply_diff_rejects_anchor_not_found():
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"m.py": "x\n"},
    )
    blocks_text = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "no such region\n"
        "=======\n"
        "y\n"
        ">>>>>>> REPLACE\n"
    )
    result = _DEFAULT_TOOLS["apply_diff"].handler({"blocks": blocks_text}, ctx)
    assert not result.ok
    assert "anchor" in result.error.lower()


# --- loop driver ------------------------------------------------------------


def _stub_llm_factory(scripted_responses: list[str]):
    """Build a stub llm_call that returns scripted responses in order."""
    iterator = iter(scripted_responses)

    def llm_call(system_prompt: str, messages: list[dict[str, str]]) -> str:
        return next(iterator)
    return llm_call


def test_loop_completes_via_apply_diff_in_one_turn():
    src = "def f():\n    return 1\n"
    blocks = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "def f():\n"
        "    return 1\n"
        "=======\n"
        "def f():\n"
        "    return 2\n"
        ">>>>>>> REPLACE\n"
    )
    import json as _json
    response = (
        "## TOOL_CALL\n"
        "```json\n"
        "{\"tool\": \"apply_diff\", \"args\": {\"blocks\": " + _json.dumps(blocks) + "}}\n"
        "```\n"
    )
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"m.py": src},
    )
    result = run_agent_loop(
        task_id="test-1",
        user_prompt="Change return 1 to return 2 in m.py",
        llm_call=_stub_llm_factory([response]),
        ctx=ctx,
    )
    assert result.terminated_reason == "diff_emitted"
    assert "diff --git a/m.py b/m.py" in result.final_diff
    assert "+    return 2" in result.final_diff


def test_loop_handles_read_then_apply_two_turns():
    src = "def f():\n    return 1\n"
    blocks = (
        "m.py\n"
        "<<<<<<< SEARCH\n"
        "def f():\n"
        "    return 1\n"
        "=======\n"
        "def f():\n"
        "    return 99\n"
        ">>>>>>> REPLACE\n"
    )
    import json as _json
    responses = [
        # Turn 1: read the file
        "## TOOL_CALL\n```json\n{\"tool\": \"read_file\", \"args\": {\"path\": \"m.py\"}}\n```\n",
        # Turn 2: apply diff
        "## TOOL_CALL\n```json\n{\"tool\": \"apply_diff\", \"args\": {\"blocks\": "
        + _json.dumps(blocks) + "}}\n```\n",
    ]
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"m.py": src},
    )
    result = run_agent_loop(
        task_id="test-2",
        user_prompt="Change return 1 to return 99",
        llm_call=_stub_llm_factory(responses),
        ctx=ctx,
    )
    assert result.terminated_reason == "diff_emitted"
    assert "+    return 99" in result.final_diff
    assert len([t for t in result.state.turns if t.role == "tool"]) == 2


def test_loop_terminates_on_done_marker():
    response = "## DONE — nothing to change"
    ctx = AgentLoopContext(sandbox_dir=None, repo_root=None, candidate_files={})
    result = run_agent_loop(
        task_id="test-3",
        user_prompt="Verify nothing to do",
        llm_call=_stub_llm_factory([response]),
        ctx=ctx,
    )
    assert result.terminated_reason == "done"
    assert result.final_diff == ""


def test_loop_terminates_on_cannot_proceed():
    response = "## CANNOT_PROCEED: source already implements behaviour"
    ctx = AgentLoopContext(sandbox_dir=None, repo_root=None, candidate_files={})
    result = run_agent_loop(
        task_id="test-4",
        user_prompt="Try a fix",
        llm_call=_stub_llm_factory([response]),
        ctx=ctx,
    )
    assert result.terminated_reason == "cannot_proceed"


def test_loop_hits_turn_budget():
    # Model emits prose forever; we cap at max_turns.
    responses = ["I'm thinking about this..."] * 20
    ctx = AgentLoopContext(sandbox_dir=None, repo_root=None, candidate_files={})
    result = run_agent_loop(
        task_id="test-5",
        user_prompt="Do something",
        llm_call=_stub_llm_factory(responses),
        ctx=ctx,
        budget=AgentLoopBudget(max_turns=3, max_seconds=600.0),
    )
    assert result.terminated_reason == "budget_turns"


def test_loop_handles_unknown_tool_gracefully():
    responses = [
        "## TOOL_CALL\n```json\n{\"tool\": \"made_up_tool\", \"args\": {}}\n```\n",
        "## DONE",
    ]
    ctx = AgentLoopContext(sandbox_dir=None, repo_root=None, candidate_files={})
    result = run_agent_loop(
        task_id="test-6",
        user_prompt="Try invalid tool",
        llm_call=_stub_llm_factory(responses),
        ctx=ctx,
    )
    assert result.terminated_reason == "done"
    # The unknown-tool error reached the model in turn 2's input.


def test_loop_soft_quota_per_tool():
    # Always asks for read_file, never commits to apply.
    responses = [
        "## TOOL_CALL\n```json\n{\"tool\": \"read_file\", \"args\": {\"path\": \"m.py\"}}\n```\n",
    ] * 12
    ctx = AgentLoopContext(
        sandbox_dir=None,
        repo_root=None,
        candidate_files={"m.py": "x = 1\n"},
    )
    result = run_agent_loop(
        task_id="test-7",
        user_prompt="Browse forever",
        llm_call=_stub_llm_factory(responses),
        ctx=ctx,
        budget=AgentLoopBudget(
            max_turns=12, max_seconds=600.0,
            soft_max_calls_per_tool={"read_file": 3, "apply_diff": 1},
        ),
    )
    # We should hit either turn budget OR see the soft-quota message
    # surfaced after 3 read_file calls.
    quota_hits = [
        t for t in result.state.turns
        if t.role == "tool" and t.tool_result and not t.tool_result.ok
        and "soft limit" in (t.tool_result.error or "").lower()
    ]
    assert len(quota_hits) >= 1


def test_render_tool_result_format():
    result = ToolResult(tool="read_file", ok=True, content={"text": "foo"})
    rendered = render_tool_result_for_prompt(result)
    assert "## TOOL_RESULT" in rendered
    assert "\"tool\": \"read_file\"" in rendered
    assert "\"ok\": true" in rendered


def test_render_system_prompt_lists_tools():
    text = render_system_prompt(_DEFAULT_TOOLS)
    for tool_name in ("read_file", "search_symbol", "list_directory", "apply_diff"):
        assert tool_name in text
    assert "## TOOL_CALL" in text
    assert "## DONE" in text
    assert "Aider" in text  # tells the model the diff format
