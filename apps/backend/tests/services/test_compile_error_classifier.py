"""Tests for T-TYPE-AWARE-COMPILE-REPAIR-V1 (2026-05-11).

Covers all five recognised Kotlin/Android compile-error shapes plus the
unknown-fallthrough case, and verifies the OSMDroid IGeoPoint -> GeoPoint
known-conversion lookup wires up correctly (P69-19 v13 driver case).
"""
from __future__ import annotations

from app.services.compile_error_classifier import (
    ClassifiedCompileError,
    classify,
    render_repair_hints,
)


# --- type_mismatch -----------------------------------------------------------


def test_classifies_assignment_type_mismatch_osmdroid():
    """The v13 actual error: IGeoPoint vs GeoPoint at Marker.position."""
    err = classify(
        "Assignment type mismatch: actual type is 'org.osmdroid.api.IGeoPoint!', "
        "but 'org.osmdroid.util.GeoPoint!' was expected.",
        file="CustomerSignup.kt",
        line=171,
    )
    assert err.kind == "type_mismatch"
    assert "IGeoPoint" in err.actual_type
    assert "GeoPoint" in err.expected_type
    assert err.library == "osmdroid"
    assert err.file == "CustomerSignup.kt"
    assert err.line == 171
    assert "TYPE MISMATCH" in err.repair_hint
    assert "GeoPoint(actual.latitude, actual.longitude)" in err.repair_hint
    # The hint must say NOT a dependency issue so DeepSeek doesn't rip imports.
    assert "NOT a missing dependency" in err.repair_hint
    assert any("GeoPoint(actual" in p for p in err.suggested_patterns)


def test_classifies_inferred_type_mismatch_simple():
    err = classify(
        "Type mismatch: inferred type is String but Int was expected",
        file="Foo.kt",
        line=10,
    )
    assert err.kind == "type_mismatch"
    assert err.actual_type == "String"
    assert err.expected_type == "Int"
    # No library tag for plain Kotlin primitives.
    assert err.library == ""


def test_type_mismatch_generic_suggestion_when_no_known_conversion():
    """Unrecognised type pair still gets a generic wrap suggestion."""
    err = classify(
        "Assignment type mismatch: actual type is 'com.example.Foo', "
        "but 'com.example.Bar' was expected.",
        file="Baz.kt",
        line=42,
    )
    assert err.kind == "type_mismatch"
    assert err.actual_type == "com.example.Foo"
    assert err.expected_type == "com.example.Bar"
    assert err.library == ""
    # Generic patterns: constructor wrap + cast suggestion.
    patterns_str = "\n".join(err.suggested_patterns)
    assert "Bar(" in patterns_str
    assert "as?" in patterns_str


# --- unresolved_reference ----------------------------------------------------


def test_classifies_unresolved_reference():
    err = classify("Unresolved reference: viewModel", file="Screen.kt", line=8)
    assert err.kind == "unresolved_reference"
    assert err.symbol == "viewModel"
    assert "NAME-LOCK" in err.repair_hint
    assert "viewModel" in err.repair_hint


def test_unresolved_reference_with_dotted_symbol():
    err = classify("Unresolved reference: com.google.maps.LatLng")
    assert err.kind == "unresolved_reference"
    assert err.symbol == "com.google.maps.LatLng"


# --- overload_mismatch -------------------------------------------------------


def test_classifies_overload_resolution_ambiguity():
    err = classify("Overload resolution ambiguity. All these functions match:")
    assert err.kind == "overload_mismatch"
    assert "OVERLOAD MISMATCH" in err.repair_hint


# --- suspend_misuse ----------------------------------------------------------


def test_classifies_suspend_function_misuse():
    err = classify(
        "Suspend function 'fetchData' should be called only from a coroutine "
        "or another suspend function"
    )
    assert err.kind == "suspend_misuse"
    assert err.symbol == "fetchData"
    assert "rememberCoroutineScope" in err.repair_hint


# --- unknown fallthrough -----------------------------------------------------


def test_unknown_compile_error_returns_unknown_kind():
    err = classify("Completely novel error text the classifier has not seen")
    assert err.kind == "unknown"
    assert err.repair_hint == ""


def test_empty_error_text_returns_unknown():
    err = classify("")
    assert err.kind == "unknown"
    assert err.raw_message == ""


# --- render_repair_hints -----------------------------------------------------


def test_render_concatenates_known_hints_skips_unknown():
    errs = [
        classify(
            "Assignment type mismatch: actual type is 'String', "
            "but 'Int' was expected.",
        ),
        classify("Unresolved reference: foo"),
        classify("gibberish"),
    ]
    rendered = render_repair_hints(errs)
    assert "STRUCTURED COMPILE ERROR ANALYSIS" in rendered
    assert "type_mismatch" in rendered
    assert "unresolved_reference" in rendered
    # The unknown one should not appear.
    assert "unknown" not in rendered.split("---")[0].lower()


def test_render_returns_empty_when_all_unknown():
    errs = [classify("gibberish 1"), classify("gibberish 2")]
    assert render_repair_hints(errs) == ""


# --- cross-library conversion (Google Maps <-> OSMDroid) ---------------------


def test_classifies_latlng_to_geopoint_cross_library():
    err = classify(
        "Assignment type mismatch: actual type is "
        "'com.google.android.gms.maps.model.LatLng', "
        "but 'org.osmdroid.util.GeoPoint' was expected.",
    )
    assert err.kind == "type_mismatch"
    assert err.library in ("google_maps", "osmdroid", "maps_cross")
    assert "GeoPoint(actual.latitude, actual.longitude)" in err.repair_hint


# --- Address -> String (Geocoder pattern) ------------------------------------


def test_classifies_address_to_string_geocoder():
    err = classify(
        "Assignment type mismatch: actual type is 'android.location.Address', "
        "but 'String' was expected.",
    )
    assert err.kind == "type_mismatch"
    assert "getAddressLine" in err.repair_hint


# --- cannot_infer_type (v14 P69-19 case) --------------------------------------


def test_classifies_cannot_infer_type_for_parameter():
    """v14 case: companion error to Unresolved reference at same line."""
    err = classify(
        "Cannot infer type for this parameter. Please specify it explicitly.",
        file="HandymanKYCAddressForm.kt",
        line=245,
    )
    assert err.kind == "cannot_infer_type"
    assert "TYPE INFERENCE FAILURE" in err.repair_hint
    # Hint must point at the underlying unresolved reference, not at types.
    assert "Unresolved reference" in err.repair_hint


# --- kotlin_structural_breakage (Round11f C10) -------------------------------


def test_classifies_kotlin_parser_expectation_as_structural_breakage():
    err = classify(
        "Expecting ')' ; Unexpected tokens (use ';' to separate expressions)",
        file="CustomerKYCAddressForm.kt",
        line=83,
    )
    assert err.kind == "kotlin_structural_breakage"
    assert "KOTLIN STRUCTURAL BREAKAGE" in err.repair_hint
    assert "missing import" in err.repair_hint


def test_classifies_unresolved_catch_as_structural_before_unresolved_reference():
    """Round11f first error shape: `catch` parsed as an identifier."""
    err = classify(
        "Unresolved reference 'catch'; Unresolved reference 'e'; Expecting ')'",
        file="CustomerKYCAddressForm.kt",
        line=83,
    )
    assert err.kind == "kotlin_structural_breakage"
    assert err.symbol == ""
    assert "try" in err.repair_hint


def test_classifies_missing_compose_content_as_structural_breakage():
    """Broken Compose/Firebase braces often surface first at Button(content)."""
    err = classify(
        "No value passed for parameter 'content'.",
        file="HandymanKYCAddressForm.kt",
        line=330,
    )

    assert err.kind == "kotlin_structural_breakage"
    assert "KOTLIN STRUCTURAL BREAKAGE" in err.repair_hint
