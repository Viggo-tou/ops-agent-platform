from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codegen_react_loop import (  # noqa: E402
    SymbolPlan,
    react_codegen_call,
    verify_symbol_plan,
)


def test_symbolplan_parse_valid_json() -> None:
    text = (
        '{"referenced_symbols": [{"name": "foo", "source_file": "Bar.kt", "kind": "field"}], '
        '"files_to_modify": ["Bar.kt"], "rationale": "demo"}'
    )

    plan = SymbolPlan.parse(text)

    assert plan is not None
    assert len(plan.referenced_symbols) == 1
    assert plan.referenced_symbols[0]["name"] == "foo"
    assert plan.files_to_modify == ["Bar.kt"]
    assert plan.rationale == "demo"


def test_symbolplan_parse_with_markdown_fence() -> None:
    text = """```json
{"referenced_symbols": [], "files_to_modify": [], "rationale": ""}
```"""

    plan = SymbolPlan.parse(text)

    assert plan is not None
    assert plan.referenced_symbols == []
    assert plan.files_to_modify == []


def test_symbolplan_parse_invalid_returns_none() -> None:
    assert SymbolPlan.parse("this is just prose, not json") is None


def test_verify_finds_symbol_in_declared_file() -> None:
    plan = SymbolPlan(
        referenced_symbols=[{"name": "foo", "source_file": "A.kt", "kind": "method"}],
        files_to_modify=["A.kt"],
        rationale="demo",
    )

    result = verify_symbol_plan(plan, {"A.kt": "fun foo() {}"})

    assert [s["name"] for s in result.verified] == ["foo"]
    assert result.hallucinated == []
    assert result.misattributed == []


def test_verify_flags_hallucinated_symbol() -> None:
    plan = SymbolPlan(
        referenced_symbols=[{"name": "baz", "source_file": "A.kt", "kind": "method"}],
        files_to_modify=["A.kt"],
        rationale="demo",
    )

    result = verify_symbol_plan(plan, {"A.kt": "fun foo() {}"})

    assert result.verified == []
    assert [s["name"] for s in result.hallucinated] == ["baz"]
    assert result.misattributed == []


def test_verify_flags_misattributed_symbol() -> None:
    plan = SymbolPlan(
        referenced_symbols=[{"name": "foo", "source_file": "A.kt", "kind": "method"}],
        files_to_modify=["A.kt"],
        rationale="demo",
    )

    result = verify_symbol_plan(
        plan,
        {"A.kt": "fun nothing() {}", "B.kt": "fun foo() {}"},
    )

    assert result.verified == []
    assert result.hallucinated == []
    assert [s["name"] for s in result.misattributed] == ["foo"]
    assert result.misattributed[0]["actually_in"] == "B.kt"


def test_verify_suffix_tolerant_path_match() -> None:
    plan = SymbolPlan(
        referenced_symbols=[{"name": "foo", "source_file": "app/src/A.kt", "kind": "method"}],
        files_to_modify=["app/src/A.kt"],
        rationale="demo",
    )

    result = verify_symbol_plan(plan, {"A.kt": "fun foo() {}"})

    assert [s["name"] for s in result.verified] == ["foo"]
    assert result.hallucinated == []
    assert result.misattributed == []


def test_react_codegen_call_returns_augmented_when_plan_parses() -> None:
    task_description = "Wire saved address into JobPostingFragment."
    plan_text = json.dumps(
        {
            "referenced_symbols": [
                {"name": "foo", "source_file": "A.kt", "kind": "method"},
            ],
            "files_to_modify": ["A.kt"],
            "rationale": "demo",
        }
    )

    result = react_codegen_call(
        task_description=task_description,
        plan_json={},
        context_files={"A.kt": "fun foo() {}"},
        once_call=lambda _: plan_text,
    )

    assert "Verified symbols you may use:" in result
    assert "foo (in A.kt)" in result
    assert task_description in result


def test_react_codegen_call_falls_back_when_plan_unparsable() -> None:
    task_description = "Make a small change."

    result = react_codegen_call(
        task_description=task_description,
        plan_json={},
        context_files={"A.kt": "fun foo() {}"},
        once_call=lambda _: "this is just prose",
    )

    assert result == task_description


def test_react_codegen_call_falls_back_on_too_many_hallucinations() -> None:
    task_description = "Make a small change."
    plan_text = json.dumps(
        {
            "referenced_symbols": [
                {"name": f"sym{i}", "source_file": "A.kt", "kind": "field"}
                for i in range(5)
            ],
            "files_to_modify": ["A.kt"],
            "rationale": "demo",
        }
    )

    result = react_codegen_call(
        task_description=task_description,
        plan_json={},
        context_files={"A.kt": "fun existing() {}"},
        once_call=lambda _: plan_text,
    )

    assert result == task_description


def test_disk_grep_fallback_recovers_misattributed_symbol(tmp_path: Path) -> None:
    # Symbol Foo is not in the prefetched context bundle but DOES exist
    # in another file under repo_root. Without disk-grep fallback this
    # would be flagged as hallucinated; with the fallback it should be
    # marked misattributed (i.e. real but in a different file).
    (tmp_path / "Real.kt").write_text("class Foo { fun bar() {} }\n", encoding="utf-8")
    (tmp_path / "Decoy.kt").write_text("class Bar\n", encoding="utf-8")

    plan = SymbolPlan(
        referenced_symbols=[{"name": "Foo", "source_file": "Missing.kt", "kind": "class"}],
        files_to_modify=["X.kt"],
        rationale="",
    )

    verification = verify_symbol_plan(
        plan,
        context_files={"X.kt": "// unrelated\n"},
        repo_root=tmp_path,
    )

    assert not verification.hallucinated
    assert len(verification.misattributed) == 1
    assert "Real.kt" in verification.misattributed[0]["actually_in"]


def test_disk_grep_no_repo_root_keeps_legacy_hallucination(tmp_path: Path) -> None:
    # Without repo_root, behavior is the same as before this change.
    plan = SymbolPlan(
        referenced_symbols=[{"name": "Foo", "source_file": "Missing.kt", "kind": "class"}],
        files_to_modify=["X.kt"],
        rationale="",
    )

    verification = verify_symbol_plan(plan, context_files={"X.kt": "// nope"})

    assert len(verification.hallucinated) == 1
    assert verification.misattributed == []


def test_disk_grep_truly_invented_symbol_still_hallucinated(tmp_path: Path) -> None:
    (tmp_path / "Real.kt").write_text("class Foo\n", encoding="utf-8")

    plan = SymbolPlan(
        referenced_symbols=[
            {"name": "TotallyInventedThingXYZ", "source_file": "Foo.kt", "kind": "method"}
        ],
        files_to_modify=["X.kt"],
        rationale="",
    )

    verification = verify_symbol_plan(
        plan,
        context_files={"X.kt": "// nope"},
        repo_root=tmp_path,
    )

    assert len(verification.hallucinated) == 1
    assert verification.misattributed == []

