from __future__ import annotations

import pytest

from app.services.structural_edit import (
    apply_kotlin_diagnostic_fast_fixes,
    apply_structural_edit_plan,
    locate_kotlin_regions,
    parse_structural_edit_response,
    validate_kotlin_structure,
)


KOTLIN_SOURCE = """\
package com.example.handyman

import androidx.compose.runtime.Composable
import androidx.compose.runtime.mutableStateOf

@Composable
fun CustomerSignup() {
    var showMap = false
    if (showMap) {
        mapView.setOnMapClickListener { point ->
            selectedGeoPoint = point
        }
    }
    saveButton()
}
"""


def test_parse_structural_edit_response_accepts_json_fence():
    parsed = parse_structural_edit_response(
        """```json
{"status":"repair_patch","file":"Screen.kt","edits":[]}
```"""
    )
    assert parsed["status"] == "repair_patch"
    assert parsed["file"] == "Screen.kt"


def test_locate_kotlin_regions_finds_imports_function_and_block():
    loc = locate_kotlin_regions(
        KOTLIN_SOURCE,
        line=10,
        anchor_substring="setOnMapClickListener",
    )
    assert loc.imports_region.start_line == 3
    assert loc.imports_region.end_line == 4
    assert loc.nearest_function is not None
    assert loc.nearest_function.name == "CustomerSignup"
    assert loc.nearest_function.start_line == 7
    assert loc.nearest_function.end_line == 15
    assert loc.nearest_block is not None
    # The nearest region is the inner callback block; the enclosing if block
    # is deliberately wider and less precise for repair.
    assert loc.nearest_block.start_line == 10
    assert loc.nearest_block.end_line == 12


def test_apply_structural_edit_adds_import_and_replaces_call_block():
    plan = {
        "status": "repair_patch",
        "file": "CustomerSignup.kt",
        "edits": [
            {
                "operation": "add_import",
                "content": "import org.osmdroid.views.overlay.MapEventsOverlay",
            },
            {
                "operation": "replace_call_expression",
                "anchor_line": 10,
                "anchor_substring": "setOnMapClickListener",
                "content": (
                    "mapView.overlays.add(MapEventsOverlay(object : MapEventsReceiver {\n"
                    "    override fun singleTapConfirmedHelper(p: GeoPoint?): Boolean {\n"
                    "        selectedGeoPoint = p\n"
                    "        return true\n"
                    "    }\n"
                    "    override fun longPressHelper(p: GeoPoint?): Boolean = false\n"
                    "}))"
                ),
            },
        ],
    }
    result = apply_structural_edit_plan(
        file_path="CustomerSignup.kt",
        original_content=KOTLIN_SOURCE,
        plan=plan,
        protected_symbols=["showMap"],
    )
    assert result.ok, result.errors
    assert "import org.osmdroid.views.overlay.MapEventsOverlay" in result.content
    assert "setOnMapClickListener" not in result.content
    assert "singleTapConfirmedHelper" in result.content
    assert "showMap" in result.content
    assert "diff --git a/CustomerSignup.kt b/CustomerSignup.kt" in result.diff


def test_apply_structural_edit_rejects_ambiguous_anchor_without_line_pin():
    source = "fun A(){\n    call()\n    call()\n}\n"
    plan = {
        "file": "A.kt",
        "edits": [
            {"operation": "replace_block", "anchor_substring": "call()", "content": "other()"}
        ],
    }
    result = apply_structural_edit_plan(
        file_path="A.kt",
        original_content=source,
        plan=plan,
    )
    assert not result.ok
    assert any("anchor not found or ambiguous" in e.reason for e in result.errors)


def test_apply_structural_edit_rejects_missing_protected_symbol():
    plan = {
        "file": "CustomerSignup.kt",
        "edits": [
            {
                "operation": "replace_block",
                "anchor_line": 11,
                "anchor_substring": "selectedGeoPoint = point",
                "content": "otherPoint = point",
            }
        ],
    }
    result = apply_structural_edit_plan(
        file_path="CustomerSignup.kt",
        original_content=KOTLIN_SOURCE,
        plan=plan,
        protected_symbols=["selectedGeoPoint"],
    )
    assert not result.ok
    assert any("protected symbol disappeared: selectedGeoPoint" in e.reason for e in result.errors)


@pytest.mark.parametrize(
    "source, expected",
    [
        ("fun A() {\n    println(1)\n", "unclosed brace"),
        ("fun A() {\n}\nimport x.y.Z\n", "import outside import region"),
    ],
)
def test_validate_kotlin_structure_catches_basic_damage(source: str, expected: str):
    errors = validate_kotlin_structure(source)
    assert any(expected in error for error in errors)


def test_kotlin_fast_fix_adds_coroutines_launch_import():
    source = """\
package com.example

import android.util.Log

fun Screen() {
    kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO).launch {
        Log.d("x", "run")
    }
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Screen.kt",
        original_content=source,
        error_text="Unresolved reference 'launch'.",
        line=6,
    )

    assert result is not None
    assert result.ok, result.errors
    assert "import kotlinx.coroutines.launch" in result.content
    assert ".launch {" in result.content


def test_kotlin_fast_fix_adds_remember_coroutine_scope_import():
    source = """\
package com.example

import androidx.compose.runtime.Composable

@Composable
fun Screen() {
    val coroutineScope = rememberCoroutineScope()
    Button(onClick = { coroutineScope.launch { save() } }) {}
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Screen.kt",
        original_content=source,
        error_text="Unresolved reference 'rememberCoroutineScope'.",
        line=7,
    )

    assert result is not None
    assert result.ok, result.errors
    assert "import androidx.compose.runtime.rememberCoroutineScope" in result.content


def test_kotlin_fast_fix_wraps_broken_firebase_snapshot_children_loop():
    source = """\
package com.example

import com.google.firebase.database.FirebaseDatabase

fun save() {
    val query = FirebaseDatabase.getInstance().getReference("Handyman")
    query.get().addOnSuccessListener { snapshot ->
            child.ref.updateChildren(addressData)
                .addOnSuccessListener {
                    done()
                }
                .addOnFailureListener { e ->
                    log(e.message)
                }
        }
        if (!snapshot.exists()) {
            log("missing")
        }
    }.addOnFailureListener { e ->
        log(e.message)
    }
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Save.kt",
        original_content=source,
        error_text="No value passed for parameter 'content'.",
        line=7,
        protected_symbols=["updateChildren", "addOnFailureListener"],
    )

    assert result is not None
    assert result.ok, result.errors
    assert "if (snapshot.exists())" in result.content
    assert "for (child in snapshot.children)" in result.content
    assert "} else {" in result.content
    assert "if (!snapshot.exists())" not in result.content
    assert "updateChildren(addressData)" in result.content


def test_kotlin_fast_fix_inserts_missing_try_for_isolated_catch_in_launch():
    source = """\
package com.example

import android.location.Geocoder
import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

fun reverseGeocode(geocoder: Geocoder, lat: Double, lng: Double) {
    coroutineScope.launch(Dispatchers.IO) {
            val results = geocoder.getFromLocation(lat, lng, 1)
            if (!results.isNullOrEmpty()) {
                withContext(Dispatchers.Main) {
                    Log.d("Geo", "ok")
                }
            }
        } catch (e: Exception) {
            Log.e("Geo", "Reverse geocode failed: ${e.message}")
        }
    }
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Geo.kt",
        original_content=source,
        error_text="Unresolved reference 'catch'. Syntax error: Expecting ')'.",
        line=16,
        protected_symbols=["getFromLocation", "withContext"],
    )

    assert result is not None
    assert result.ok, result.errors
    assert "coroutineScope.launch(Dispatchers.IO) {\n        try {" in result.content
    assert "} catch (e: Exception) {" in result.content
    assert "getFromLocation(lat, lng, 1)" in result.content


def test_kotlin_fast_fix_makes_geocoder_addresses_nullable_safe():
    source = """\
package com.example

import android.location.Geocoder
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

fun reverseGeocode() {
    coroutineScope.launch(Dispatchers.IO) {
        try {
            val addresses = geocoder.getFromLocation(point.latitude, point.longitude, 1)
            if (addresses.isNotEmpty()) {
                val addr = addresses[0]
                street = addr.thoroughfare ?: ""
            }
        } catch (e: Exception) {
            log(e.message)
        }
    }
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Geo.kt",
        original_content=source,
        error_text=(
            "Only safe (?.) or non-null asserted (!!.) calls are allowed on a "
            "nullable receiver of type 'kotlin.collections.(Mutable)List<android.location.Address!>?'."
        ),
        line=11,
        protected_symbols=["getFromLocation"],
    )

    assert result is not None
    assert result.ok, result.errors
    assert "val addr = addresses?.firstOrNull()" in result.content
    assert "if (addr != null) {" in result.content
    assert "addresses.isNotEmpty()" not in result.content
    assert "addresses[0]" not in result.content


def test_kotlin_fast_fix_does_not_treat_valid_catch_as_missing_try_for_other_syntax_errors():
    source = """\
package com.example

import android.location.Geocoder
import com.google.firebase.database.FirebaseDatabase
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

fun save() {
    coroutineScope.launch(Dispatchers.IO) {
        try {
            val addresses = geocoder.getFromLocation(point.latitude, point.longitude, 1)
            if (addresses.isNotEmpty()) {
                val addr = addresses[0]
                street = addr.thoroughfare ?: ""
            }
        } catch (e: Exception) {
            log(e.message)
        }
    }
    val query = FirebaseDatabase.getInstance().getReference("Handyman")
    query.get().addOnSuccessListener { snapshot ->
            child.ref.updateChildren(addressData)
                .addOnFailureListener { e ->
                    log(e.message)
                }
        }
        if (!snapshot.exists()) {
            log("missing")
        }
    }.addOnFailureListener { e ->
        log(e.message)
    }
}
"""

    result = apply_kotlin_diagnostic_fast_fixes(
        file_path="Save.kt",
        original_content=source,
        error_text=(
            "No value passed for parameter 'content'. "
            "Syntax error: Expecting ')'. "
            "Only safe calls are allowed on a nullable receiver of type "
            "'kotlin.collections.(Mutable)List<android.location.Address!>?'."
        ),
        line=21,
        protected_symbols=["getFromLocation", "updateChildren", "addOnFailureListener"],
    )

    assert result is not None
    assert result.ok, result.errors
    assert "insert_missing_try_for_catch" not in result.applied_operations
    assert "make_geocoder_addresses_nullable_safe" in result.applied_operations
    assert "wrap_firebase_snapshot_children" in result.applied_operations
    assert "val addr = addresses?.firstOrNull()" in result.content
    assert "for (child in snapshot.children)" in result.content


def test_structural_plan_supports_firebase_snapshot_children_operation():
    source = """\
package com.example

import com.google.firebase.database.FirebaseDatabase

fun save() {
    val query = FirebaseDatabase.getInstance().getReference("Handyman")
    query.get().addOnSuccessListener { snapshot ->
            child.ref.updateChildren(addressData)
        }
        if (!snapshot.exists()) {
            log("missing")
        }
    }.addOnFailureListener { e ->
        log(e.message)
    }
}
"""

    result = apply_structural_edit_plan(
        file_path="Save.kt",
        original_content=source,
        plan={
            "file": "Save.kt",
            "edits": [
                {
                    "operation": "wrap_firebase_snapshot_children",
                    "anchor_line": 7,
                }
            ],
        },
        protected_symbols=["updateChildren"],
    )

    assert result.ok, result.errors
    assert "for (child in snapshot.children)" in result.content
