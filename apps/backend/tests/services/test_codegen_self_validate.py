"""Stage A: codegen self-validation unit tests."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.codegen_self_validate import (  # noqa: E402
    self_validate,
    validate_diff_applies,
    validate_diff_parses,
)


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
