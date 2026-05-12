"""Stage A: codegen self-validation unit tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import codegen as codegen_module  # noqa: E402
from app.services.codegen import CodeGenerator, CodegenError  # noqa: E402
from app.services.codegen_self_validate import (  # noqa: E402
    self_validate,
    validate_diff_applies,
    validate_diff_parses,
    validate_imports_preserved,
    validate_no_rewrite_of_existing,
)


# ---- L4a: import preservation tests ---------------------------------------

def test_import_preserved_clean_diff_passes():
    diff = (
        "diff --git a/Foo.kt b/Foo.kt\n"
        "--- a/Foo.kt\n"
        "+++ b/Foo.kt\n"
        "@@ -1,3 +1,4 @@\n"
        " package x\n"
        " import a.B\n"
        "+import c.D\n"
        " class Foo\n"
    )
    ok, err = validate_imports_preserved(diff)
    assert ok is True
    assert err == ""


def test_import_preserved_drops_one_import_passes_within_slack():
    """Single intentional cleanup is within the 2-slack tolerance."""
    diff = (
        "diff --git a/Foo.kt b/Foo.kt\n"
        "--- a/Foo.kt\n"
        "+++ b/Foo.kt\n"
        "@@ -1,3 +1,2 @@\n"
        " package x\n"
        "-import a.UnusedImport\n"
        " class Foo\n"
    )
    ok, err = validate_imports_preserved(diff)
    assert ok is True


def test_import_preserved_v26_failure_mode_caught():
    """The exact P69-17 v26 failure: DeepSeek dropped 4+ imports of
    rememberNavController / JobPostingViewModel / viewModel / etc.
    Validator must catch this and reject."""
    diff = (
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,8 +1,3 @@\n"
        " package com.example.handyman\n"
        "-import androidx.navigation.compose.rememberNavController\n"
        "-import androidx.lifecycle.viewmodel.compose.viewModel\n"
        "-import com.example.handyman.ui.JobPostingViewModel\n"
        "-import androidx.compose.runtime.LaunchedEffect\n"
        "-import androidx.compose.runtime.remember\n"
        " class JobPostingFragment {\n"
        "   fun something() = rememberNavController()\n"
        " }\n"
    )
    ok, err = validate_imports_preserved(diff)
    assert ok is False
    assert "JobPostingFragment.kt" in err
    assert "DeepSeek-style" in err or "import" in err.lower()


def test_import_preserved_skips_non_kotlin_files():
    """Markdown / text files never trigger this check."""
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,3 +1,2 @@\n"
        "-import old\n"
        "-import other\n"
        "-import third\n"
        "-import fourth\n"
        " text\n"
    )
    ok, _ = validate_imports_preserved(diff)
    assert ok is True


def test_import_preserved_replaces_one_for_one_passes():
    """Renaming an import (drop one, add one) nets to zero, OK."""
    diff = (
        "diff --git a/Foo.kt b/Foo.kt\n"
        "--- a/Foo.kt\n"
        "+++ b/Foo.kt\n"
        "@@ -1,3 +1,3 @@\n"
        " package x\n"
        "-import old.path.SymA\n"
        "+import new.path.SymA\n"
        " class Foo\n"
    )
    ok, _ = validate_imports_preserved(diff)
    assert ok is True


def test_import_preserved_empty_diff_passes():
    ok, err = validate_imports_preserved("")
    assert ok is True
    assert err == ""


# ---- L5: no whole-file rewrite of existing must_touch files ----------------

def test_l5_rejects_new_file_mode_for_must_touch():
    diff = (
        "diff --git a/app/src/main/JobPostingFragment.kt b/app/src/main/JobPostingFragment.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/app/src/main/JobPostingFragment.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class JobPostingFragment\n"
    )

    ok, err = validate_no_rewrite_of_existing(
        diff, ["app/src/main/JobPostingFragment.kt"]
    )

    assert ok is False
    assert "JobPostingFragment.kt" in err


def test_l5_allows_minimal_edit_for_must_touch():
    diff = (
        "diff --git a/app/src/main/X.kt b/app/src/main/X.kt\n"
        "--- a/app/src/main/X.kt\n"
        "+++ b/app/src/main/X.kt\n"
        "@@ -1,2 +1,2 @@\n"
        " package example\n"
        "-class X\n"
        "+class X(val enabled: Boolean)\n"
    )

    ok, err = validate_no_rewrite_of_existing(diff, ["app/src/main/X.kt"])

    assert ok is True
    assert err == ""


def test_l5_allows_new_file_mode_for_path_not_in_must_touch():
    diff = (
        "diff --git a/app/src/main/NewFile.kt b/app/src/main/NewFile.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/app/src/main/NewFile.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class NewFile\n"
    )

    ok, err = validate_no_rewrite_of_existing(diff, ["app/src/main/X.kt"])

    assert ok is True
    assert err == ""


def test_l5_handles_suffix_tolerant_paths():
    diff = (
        "diff --git a/X.kt b/X.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/X.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class X\n"
    )

    ok, err = validate_no_rewrite_of_existing(diff, ["app/src/main/X.kt"])

    assert ok is False
    assert "X.kt" in err


def test_l5_handles_mixed_diff():
    diff = (
        "diff --git a/app/src/main/Bad.kt b/app/src/main/Bad.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/app/src/main/Bad.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class Bad\n"
        "diff --git a/app/src/main/Ok.kt b/app/src/main/Ok.kt\n"
        "--- a/app/src/main/Ok.kt\n"
        "+++ b/app/src/main/Ok.kt\n"
        "@@ -1,2 +1,2 @@\n"
        " package example\n"
        "-class Ok\n"
        "+class Ok(val enabled: Boolean)\n"
    )

    ok, err = validate_no_rewrite_of_existing(
        diff, ["app/src/main/Bad.kt", "app/src/main/Ok.kt"]
    )

    assert ok is False
    assert "Bad.kt" in err
    assert "Ok.kt" not in err


def test_l5_skipped_when_must_touch_empty():
    diff = (
        "diff --git a/app/src/main/NewFile.kt b/app/src/main/NewFile.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/app/src/main/NewFile.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class NewFile\n"
    )

    ok, err = validate_no_rewrite_of_existing(diff, [])
    assert ok is True
    assert err == ""

    ok, err = validate_no_rewrite_of_existing(diff, None)  # type: ignore[arg-type]
    assert ok is True
    assert err == ""


def test_l5_skipped_for_empty_diff():
    ok, err = validate_no_rewrite_of_existing(
        "", ["app/src/main/JobPostingFragment.kt"]
    )

    assert ok is True
    assert err == ""


def _make_repo_with_file(tmp_root: Path, rel: str, content: str) -> Path:
    src = tmp_root / "src"
    src.mkdir(parents=True, exist_ok=True)
    target = src / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    import subprocess
    subprocess.run(["git", "init"], cwd=str(src), capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(src), capture_output=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "test"], cwd=str(src), capture_output=True, timeout=10)
    subprocess.run(["git", "add", "."], cwd=str(src), capture_output=True, timeout=10)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(src), capture_output=True, timeout=10)
    return src


def test_clean_applicable_diff_validates():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "foo.py", "def f():\n    return 1\n")
        diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 2
"""
        result = self_validate(diff, src)
        assert result.valid, f"expected valid, got: {result.reason} / {result.error_detail}"


def test_hunk_drift_caught_by_apply_check():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "foo.py", "def f():\n    return 1\n")
        # Diff context references lines that don't exist in actual file
        diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 def g():
-    return 99
+    return 42
+    print('ok')
"""
        result = self_validate(diff, src)
        assert not result.valid
        assert not result.apply_check_passed


def test_python_syntax_error_caught_by_parse_check():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "foo.py", "def f():\n    return 1\n")
        # Diff applies cleanly but produces broken Python
        diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 def f():
     return 1
+def g(:
"""
        result = self_validate(diff, src)
        assert not result.valid
        assert result.apply_check_passed
        assert not result.parse_check_passed


def test_kotlin_diff_skipped_no_parser():
    """Kotlin files have no fast standalone parser; we skip parse check.
    apply_check still runs. With clean apply + no parseable files, valid."""
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "Foo.kt", "package x\nclass Foo\n")
        diff = """diff --git a/Foo.kt b/Foo.kt
--- a/Foo.kt
+++ b/Foo.kt
@@ -1,2 +1,3 @@
 package x
 class Foo
+class Bar
"""
        result = self_validate(diff, src)
        assert result.valid


def test_empty_diff_returns_valid():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "foo.py", "")
        result = self_validate("", src)
        assert result.valid


def test_missing_source_path_skips():
    result = self_validate("diff --git a/x b/x\n@@ -0,0 +1 @@\n+x", Path("/nonexistent/path/zzz"))
    assert result.valid


def test_self_validate_chains_to_l4e():
    diff = (
        "diff --git a/Job.kt b/Job.kt\n"
        "--- a/Job.kt\n"
        "+++ b/Job.kt\n"
        "@@ -1,6 +1,6 @@\n"
        " package example\n"
        " data class Job(\n"
        "-    val jobLocation: String,\n"
        "+    val location: String,\n"
        "     val title: String,\n"
        " )\n"
        "diff --git a/JobPostingFragment.kt b/JobPostingFragment.kt\n"
        "--- a/JobPostingFragment.kt\n"
        "+++ b/JobPostingFragment.kt\n"
        "@@ -1,5 +1,5 @@\n"
        " package example\n"
        " fun bind(job: Job) {\n"
        "     val display = job.jobLocation\n"
        "     println(display)\n"
        " }\n"
    )

    result = self_validate(diff, Path("/nonexistent/path/zzz"))

    assert result.valid is False
    assert (
        "L4e" in result.reason
        or "cross-file" in result.reason
        or "oscillation" in result.reason
    )


def test_self_validate_chains_to_l5():
    diff = (
        "diff --git a/app/src/main/JobPostingFragment.kt b/app/src/main/JobPostingFragment.kt\n"
        "new file mode 100644\n"
        "index 0000000..1234567\n"
        "--- /dev/null\n"
        "+++ b/app/src/main/JobPostingFragment.kt\n"
        "@@ -0,0 +1,2 @@\n"
        "+package example\n"
        "+class JobPostingFragment\n"
    )

    with tempfile.TemporaryDirectory() as tmp:
        result = self_validate(
            diff,
            Path(tmp),
            must_touch_files=["app/src/main/JobPostingFragment.kt"],
        )

    assert result.valid is False
    assert "L5" in result.reason or "new file mode" in result.reason
    assert "JobPostingFragment.kt" in result.error_detail


def _deepseek_settings() -> SimpleNamespace:
    return SimpleNamespace(
        deepseek_api_key="test-key",
        deepseek_model="deepseek-coder",
        deepseek_timeout_seconds=30.0,
    )


def _deepseek_response(content: str) -> Mock:
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
    }
    return response


def test_deepseek_retry_on_wrapper_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapped = "=== FILE foo.kt ===\ndiff --git a/foo.kt b/foo.kt\n=== END FILE foo.kt ==="
    clean = (
        "diff --git a/foo.kt b/foo.kt\n"
        "--- a/foo.kt\n"
        "+++ b/foo.kt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    calls: list[dict] = []
    responses = [_deepseek_response(wrapped), _deepseek_response(clean)]

    def fake_post(**kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(codegen_module, "cached_http_post", fake_post)

    result = CodeGenerator(_deepseek_settings())._call_deepseek("make a change")

    assert result.files_changed == ["foo.kt"]
    assert result.provider_name == "deepseek"
    assert len(calls) == 2
    retry_prompt = calls[1]["json"]["messages"][1]["content"]
    assert "Output ONLY the raw unified diff" in retry_prompt
    assert "Do NOT wrap with === markers" in retry_prompt


def test_deepseek_no_retry_on_unrelated_error(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_post(**kwargs):
        calls.append(kwargs)
        raise httpx.ReadTimeout("network timed out")

    monkeypatch.setattr(codegen_module, "cached_http_post", fake_post)

    with pytest.raises(CodegenError, match="DeepSeek API error"):
        CodeGenerator(_deepseek_settings())._call_deepseek("make a change")

    assert len(calls) == 1


def test_validate_diff_applies_directly():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "a.py", "x = 1\n")
        ok, err = validate_diff_applies(
            "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n",
            src,
        )
        assert ok, err


def test_validate_diff_parses_skipped_for_non_python_non_js():
    with tempfile.TemporaryDirectory() as tmp:
        src = _make_repo_with_file(Path(tmp), "data.txt", "hi\n")
        ok, err = validate_diff_parses(
            "diff --git a/data.txt b/data.txt\n--- a/data.txt\n+++ b/data.txt\n@@ -1 +1 @@\n-hi\n+bye\n",
            src,
        )
        assert ok
