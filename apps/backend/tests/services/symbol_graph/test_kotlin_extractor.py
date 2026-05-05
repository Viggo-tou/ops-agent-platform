"""Tests for the Kotlin SymbolExtractor (tree-sitter backed)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Skip the entire module if tree-sitter Kotlin wheel is missing.
pytest.importorskip("tree_sitter_kotlin")

from app.services.symbol_graph.kotlin_extractor import KotlinExtractor  # noqa: E402
from app.services.symbol_graph.registry import (  # noqa: E402
    _clear_registry_for_tests,
    get_extractor_for_path,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _clear_registry_for_tests()
    # Re-import the module to re-run register_extractor (cached otherwise)
    import importlib
    from app.services.symbol_graph import kotlin_extractor as _kt
    importlib.reload(_kt)
    yield
    _clear_registry_for_tests()


def test_kotlin_extractor_registered_for_kt_and_kts():
    assert get_extractor_for_path("Foo.kt") is not None
    assert get_extractor_for_path("build.gradle.kts") is not None


def test_kotlin_extracts_class_decl():
    src = b"package x\nclass MyClass { }\n"
    res = KotlinExtractor().extract(path="A.kt", source=src)
    decl_names = {d.name for d in res.decls}
    decl_kinds = {d.kind for d in res.decls if d.name == "MyClass"}
    assert "MyClass" in decl_names
    assert "class" in decl_kinds


def test_kotlin_extracts_function_decl_with_line():
    src = b"package x\n\nfun loadHomeAddress(): String { return \"\" }\n"
    res = KotlinExtractor().extract(path="A.kt", source=src)
    fns = [d for d in res.decls if d.kind == "function"]
    assert len(fns) == 1
    assert fns[0].name == "loadHomeAddress"
    # Line is 1-indexed; "fun ..." is on line 3.
    assert fns[0].line == 3


def test_kotlin_extracts_import_as_ref():
    src = b"package x\n\nimport com.example.SessionManager\nimport com.utils.Helper\n"
    res = KotlinExtractor().extract(path="A.kt", source=src)
    ref_names = {r.name for r in res.refs}
    assert ref_names == {"SessionManager", "Helper"}
    # Each ref carries the qualified path in metadata for blast-radius use.
    qualifieds = {r.metadata.get("qualified") for r in res.refs}
    assert "com.example.SessionManager" in qualifieds


def test_kotlin_class_inside_class_extracted_recursively():
    src = b"""package x
class Outer {
    class Inner { fun innerFn() {} }
}
"""
    res = KotlinExtractor().extract(path="A.kt", source=src)
    names = {d.name for d in res.decls}
    assert {"Outer", "Inner", "innerFn"}.issubset(names)


def test_kotlin_top_level_property_extracted_as_variable():
    src = b"package x\n\nval homeAddress = \"123 Main\"\n"
    res = KotlinExtractor().extract(path="A.kt", source=src)
    vars_ = [d for d in res.decls if d.kind == "variable"]
    assert any(d.name == "homeAddress" for d in vars_)


def test_kotlin_invalid_source_returns_empty_no_crash():
    # Tree-sitter is recover-mode tolerant but we accept whatever it
    # returns. Either way, no crash.
    src = b"this is { not valid kotlin } @@##"
    res = KotlinExtractor().extract(path="A.kt", source=src)
    # Should not crash; decls/refs may be empty or partial
    assert isinstance(res.decls, tuple)
    assert isinstance(res.refs, tuple)


def test_kotlin_skips_external_sdk_imports():
    """Imports from android.* / androidx.* / kotlin.* / java.* are NOT
    emitted as Refs because their decls live outside the project source
    tree. Without this filter every Compose file would produce 50+
    no_decl_found violations from android.util.Log, Toast, Image, etc."""
    src = b"""package com.example

import android.util.Log
import androidx.compose.foundation.Image
import androidx.compose.foundation.clickable
import com.google.firebase.auth.FirebaseAuth
import kotlin.collections.List
import java.util.UUID
import com.example.MyInternalUtil

class A { fun f() {} }
"""
    res = KotlinExtractor().extract(path="A.kt", source=src)
    ref_names = {r.name for r in res.refs}
    # 5 SDK / library imports are filtered.
    assert "Log" not in ref_names
    assert "Image" not in ref_names
    assert "clickable" not in ref_names
    assert "FirebaseAuth" not in ref_names
    assert "List" not in ref_names
    assert "UUID" not in ref_names
    # Internal project import IS kept.
    assert "MyInternalUtil" in ref_names


def test_kotlin_object_declaration_emits_decl():
    """`object Foo { ... }` is a Kotlin singleton — tree-sitter-kotlin
    uses node type `object_declaration` (NOT class_declaration). Without
    handling it, callers writing `import com.x.SessionManager` would
    false-positive ref-validity because no Decl named `SessionManager`
    exists in the graph (only its inner members get extracted)."""
    src = b"package com.example\n\nobject SessionManager {\n  fun getHomeAddress(): String = \"\"\n}\n"
    res = KotlinExtractor().extract(path="utils/SessionManager.kt", source=src)
    decl_names_kinds = {(d.name, d.kind) for d in res.decls}
    assert ("SessionManager", "object") in decl_names_kinds


def test_kotlin_realistic_combined():
    """Smoke: a realistic file should produce both class decl and import refs."""
    src = b"""package com.example

import com.example.SessionManager
import com.example.utils.Formatter

class JobPostingFlow {
    fun loadHomeAddress(): String {
        return SessionManager.getHomeAddress()
    }
}
"""
    res = KotlinExtractor().extract(path="JobPostingFlow.kt", source=src)
    decl_names = {d.name for d in res.decls}
    ref_names = {r.name for r in res.refs}
    assert "JobPostingFlow" in decl_names
    assert "loadHomeAddress" in decl_names
    assert "SessionManager" in ref_names
    assert "Formatter" in ref_names
