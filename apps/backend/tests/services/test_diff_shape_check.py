"""Unit tests for diff_shape_check (Stage X.1)."""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.diff_shape_check import (  # noqa: E402
    analyze_diff,
    evaluate_patch_shape,
)


DOGFOOD_DESTRUCTIVE_DIFF = """diff --git a/app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt b/app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt
--- a/app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt
+++ b/app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt
@@ -1,11 +0,0 @@
-package com.example.handyman.handyman_pages
-
-import android.widget.Toast
-import androidx.compose.foundation.Image
-import androidx.compose.foundation.clickable
-import androidx.compose.foundation.layout.*
-import androidx.compose.foundation.text.KeyboardOptions
-import androidx.compose.material3.*
-import androidx.compose.runtime.*
-import androidx.compose.ui.Alignment
-import androidx.compose.ui.Modifier
diff --git a/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt b/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
--- a/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
+++ b/app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt
@@ -1,11 +0,0 @@
-package com.example.handyman.customer_pages
-
-import android.util.Log
-import android.widget.Toast
-import androidx.compose.foundation.Image
-import androidx.compose.foundation.clickable
-import androidx.compose.foundation.layout.*
-import androidx.compose.foundation.text.KeyboardOptions
-import androidx.compose.material3.*
-import androidx.compose.runtime.*
-import androidx.compose.ui.Alignment
"""


def test_dogfood_destructive_diff_is_rejected():
    """The actual P69-19 bbbc898b diff must trigger via must_touch rule."""
    result = evaluate_patch_shape(
        DOGFOOD_DESTRUCTIVE_DIFF,
        must_touch_files=[
            "app/src/main/java/com/example/handyman/handyman_pages/HandymanSignup.kt",
            "app/src/main/java/com/example/handyman/customer_pages/CustomerSignup.kt",
        ],
    )
    assert result.destructive is True
    assert "must_touch" in result.reason
    assert result.totals["added"] == 0
    assert result.totals["removed"] >= 20


def test_normal_implementation_diff_passes():
    diff = """diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,5 @@
 def hello():
-    return "hi"
+    return "hello world"
+
+def bye():
+    return "bye"
"""
    result = evaluate_patch_shape(diff, must_touch_files=["foo.py"])
    assert result.destructive is False
    assert result.totals["added"] == 4
    assert result.totals["removed"] == 1


def test_pure_addition_diff_passes():
    diff = """diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+print("hi")
+print("bye")
"""
    result = evaluate_patch_shape(diff)
    assert result.destructive is False


def test_must_touch_pure_deletion_rejected_even_when_other_files_added():
    diff = """diff --git a/required.py b/required.py
--- a/required.py
+++ b/required.py
@@ -1,3 +0,0 @@
-def x():
-    return 1
-
diff --git a/extra.py b/extra.py
--- /dev/null
+++ b/extra.py
@@ -0,0 +1,1 @@
+print("hello")
"""
    result = evaluate_patch_shape(diff, must_touch_files=["required.py"])
    assert result.destructive is True
    assert "must_touch" in result.reason


def test_must_touch_with_source_name_prefix_tolerance():
    diff = """diff --git a/myrepo/src/foo.py b/myrepo/src/foo.py
--- a/myrepo/src/foo.py
+++ b/myrepo/src/foo.py
@@ -1,2 +0,0 @@
-old line 1
-old line 2
"""
    result = evaluate_patch_shape(diff, must_touch_files=["src/foo.py"])
    assert result.destructive is True


def test_empty_diff_not_destructive():
    result = evaluate_patch_shape("", must_touch_files=["x"])
    assert result.destructive is False


def test_analyze_diff_counts_added_and_removed():
    diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -1,2 +1,3 @@
 keep
-old
+new1
+new2
"""
    per_file = analyze_diff(diff)
    assert per_file == {"a.py": {"added": 2, "removed": 1}}


def test_analyze_diff_skips_file_headers_not_counted_as_lines():
    diff = """diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -0,0 +1,1 @@
+only_real_added
"""
    per_file = analyze_diff(diff)
    assert per_file["x.py"]["added"] == 1
    assert per_file["x.py"]["removed"] == 0
