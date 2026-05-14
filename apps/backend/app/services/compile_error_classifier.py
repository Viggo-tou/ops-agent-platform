"""Classify compile errors into structured repair hints.

T-TYPE-AWARE-COMPILE-REPAIR-V1 (2026-05-11). The compile_repair loop in
the orchestrator currently forwards raw compiler text to the repair
codegen LLM. For "unresolved reference" errors we have a NAME-LOCK
hint (L4f). For everything else — type mismatches, receiver mismatches,
overload failures — the LLM sees only the raw text and often "fixes"
the wrong layer (e.g. ripping out OSMDroid imports when the real issue
is ``IGeoPoint`` vs ``GeoPoint`` at one call site).

This module pattern-matches the compiler text into a structured
``ClassifiedCompileError`` so the repair prompt can render targeted
guidance: "this is a type-contract issue, NOT a missing dependency;
wrap actual in expected's constructor".

V1 covers the four highest-ROI Kotlin/Android error shapes observed
on P69-19 v13. The classifier is pure pattern matching — no parser
state, no LLM, sub-millisecond per error.

Future T-LIBRARY-FINGERPRINT-V2-API-CONTRACTS will replace the
``_KNOWN_CONVERSIONS`` dict with a generated library API card index.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

CompileErrorKind = Literal[
    "type_mismatch",
    "unresolved_reference",
    "receiver_mismatch",
    "overload_mismatch",
    "suspend_misuse",
    "cannot_infer_type",
    "kotlin_structural_breakage",
    "unknown",
]


@dataclass(frozen=True)
class ClassifiedCompileError:
    """Structured form of a single compile error message.

    Always populated for ``kind`` and ``raw_message``. Other fields are
    best-effort and may be empty when the regex didn't capture them.
    """

    kind: CompileErrorKind
    raw_message: str
    actual_type: str = ""
    expected_type: str = ""
    symbol: str = ""  # for unresolved_reference / receiver_mismatch
    receiver: str = ""  # receiver class for receiver_mismatch
    file: str = ""
    line: int = 0
    column: int = 0
    repair_hint: str = ""
    library: str = ""  # detected library namespace if any
    suggested_patterns: tuple[str, ...] = field(default_factory=tuple)


# Pattern 1: Kotlin "Assignment type mismatch: actual type is 'X', but 'Y' was expected."
# Pattern 1b: Kotlin "Type mismatch: inferred type is X but Y was expected"
# Type names can contain `.`, `<>`, `?`, `!`, so the character class
# only excludes the surrounding quote/comma/space delimiters.
_TYPE_MISMATCH_ASSIGN_RE = re.compile(
    r"Assignment\s+type\s+mismatch:\s*actual\s+type\s+is\s+['\"]?([^'\",]+?)['\"]?,?\s+"
    r"but\s+['\"]?([^'\"]+?)['\"]?\s+was\s+expected",
    re.IGNORECASE,
)
_TYPE_MISMATCH_INFER_RE = re.compile(
    r"Type\s+mismatch:\s*inferred\s+type\s+is\s+['\"]?([^'\",\s]+)['\"]?\s+"
    r"but\s+['\"]?([^'\",\s]+)['\"]?\s+was\s+expected",
    re.IGNORECASE,
)

# Pattern 2: Kotlin "Unresolved reference: X" or "Unresolved reference 'X'"
_UNRESOLVED_RE = re.compile(
    r"Unresolved\s+reference\s*:?\s*['\"]?([A-Za-z_][\w.]*)['\"]?",
    re.IGNORECASE,
)

# Pattern 3: Kotlin "Unresolved reference. None of the following candidates is applicable:"
# Or "Type mismatch ... cannot be applied to receiver"
_RECEIVER_RE = re.compile(
    r"(?:cannot\s+be\s+applied\s+to\s+receiver|None\s+of\s+the\s+following\s+candidates).*?"
    r"['\"]?([A-Za-z_][\w.]*)['\"]?",
    re.IGNORECASE | re.DOTALL,
)

# Pattern 4: Kotlin "Overload resolution ambiguity" / "None of the following functions"
_OVERLOAD_RE = re.compile(
    r"(?:Overload\s+resolution\s+ambiguity|None\s+of\s+the\s+following\s+functions)",
    re.IGNORECASE,
)

# Pattern 5: suspend function called from non-coroutine context
_SUSPEND_RE = re.compile(
    r"Suspend\s+function\s+['\"]?([\w.]+)['\"]?\s+should\s+be\s+called\s+only\s+from",
    re.IGNORECASE,
)

# Pattern 6: Kotlin type-inference failure
# "Cannot infer type for this parameter. Please specify it explicitly."
# Frequent companion to Unresolved reference at the same site when a
# lambda parameter or property accessor references a symbol that does
# not resolve. Treat as a hint to add explicit types or fix the missing
# symbol (the unresolved reference is the underlying cause).
_INFER_TYPE_RE = re.compile(
    r"Cannot\s+infer\s+(?:a\s+)?type\s+for\s+this\s+parameter",
    re.IGNORECASE,
)

# Pattern 7: Kotlin parser / lexical-scope explosions. This is distinct
# from a normal unresolved reference: when the parser says "Expecting ')'"
# and then reports "Unresolved reference 'catch'", the problem is almost
# always a misplaced try/catch/lambda/bracket boundary, not a missing symbol.
_KOTLIN_STRUCTURAL_RE = re.compile(
    r"("
    r"Expecting\s+['\"`]?[\w)}\]]+['\"`]?"
    r"|Unexpected\s+tokens?"
    r"|Unexpected\s+symbol"
    r"|Syntax\s+error"
    r"|Expecting\s+an\s+element"
    r"|No\s+value\s+passed\s+for\s+parameter\s+['\"]?content['\"]?"
    r"|Unresolved\s+reference\s*:?\s*['\"]?(?:catch|try|finally|e)['\"]?"
    r")",
    re.IGNORECASE,
)


# Known library-specific conversion patterns. Keys are
# ``(actual_simple_name, expected_simple_name)``. The repair hint uses
# the conversion when the classified types match. Library-aware
# entries are tagged with ``library`` for telemetry. Generic entries
# use ``""`` so they fire for any namespace.
_KNOWN_CONVERSIONS: dict[tuple[str, str], dict[str, str]] = {
    ("IGeoPoint", "GeoPoint"): {
        "library": "osmdroid",
        "conversion": (
            "GeoPoint(actual.latitude, actual.longitude)\n"
            "  // org.osmdroid.api.IGeoPoint is the read-only interface; "
            "org.osmdroid.util.GeoPoint is the concrete class properties "
            "like Marker.position expect. Always wrap when assigning from "
            "an IGeoPoint source (e.g. setOnMapClickListener callback)."
        ),
    },
    ("GeoPoint", "IGeoPoint"): {
        "library": "osmdroid",
        "conversion": (
            "// Implicit upcast OK: GeoPoint implements IGeoPoint. If the "
            "compiler still rejects, an `as IGeoPoint` cast resolves it."
        ),
    },
    ("Address", "String"): {
        "library": "android",
        "conversion": (
            "// android.location.Address has getAddressLine(0) for a "
            "single-line representation, or compose fields manually:\n"
            "actual.getAddressLine(0) ?: \"\""
        ),
    },
    ("LatLng", "GeoPoint"): {
        "library": "maps_cross",
        "conversion": (
            "GeoPoint(actual.latitude, actual.longitude)\n"
            "  // Convert Google Maps com.google.android.gms.maps.model.LatLng "
            "to OSMDroid org.osmdroid.util.GeoPoint."
        ),
    },
    ("GeoPoint", "LatLng"): {
        "library": "maps_cross",
        "conversion": (
            "LatLng(actual.latitude, actual.longitude)\n"
            "  // Convert OSMDroid GeoPoint to Google Maps LatLng."
        ),
    },
}


def _simple_type_name(fully_qualified: str) -> str:
    """``org.osmdroid.api.IGeoPoint!`` -> ``IGeoPoint``."""
    name = fully_qualified.strip().rstrip("!").rstrip("?")
    # Drop generic args: ``List<Foo>`` -> ``List``.
    name = name.split("<", 1)[0]
    # Drop package prefix: ``a.b.c.D`` -> ``D``.
    if "." in name:
        name = name.rsplit(".", 1)[-1]
    return name


def _detect_library_from_type(fq_type: str) -> str:
    """Map a fully-qualified type to a library tag for telemetry."""
    lower = fq_type.lower()
    if "osmdroid" in lower:
        return "osmdroid"
    if "google.android.gms.maps" in lower or "com.google.maps" in lower:
        return "google_maps"
    if "firebase" in lower:
        return "firebase"
    if "androidx.navigation" in lower or "navhost" in lower:
        return "androidx_navigation"
    if "android.location" in lower:
        return "android_location"
    if "androidx.compose" in lower:
        return "compose"
    return ""


def _build_type_mismatch_hint(
    actual: str, expected: str, file: str, line: int
) -> tuple[str, tuple[str, ...], str]:
    """Return (hint_text, suggested_patterns, library_tag)."""
    actual_simple = _simple_type_name(actual)
    expected_simple = _simple_type_name(expected)
    library = _detect_library_from_type(actual) or _detect_library_from_type(expected)

    patterns: list[str] = []
    conversion_block = ""

    # Look up known conversion.
    known = _KNOWN_CONVERSIONS.get((actual_simple, expected_simple))
    if known:
        conversion_block = (
            f"Known conversion ({known.get('library') or 'generic'}):\n"
            f"  {known.get('conversion')}\n\n"
        )
        library = library or str(known.get("library") or "")
        patterns.append(known.get("conversion", ""))
    else:
        # Generic constructor-wrap suggestion when the names look
        # related (e.g. both contain "Point" or share a prefix).
        if actual_simple and expected_simple:
            patterns.append(
                f"{expected_simple}(actual.field1, actual.field2)  "
                f"// wrap with constructor if {expected_simple} has one"
            )
            patterns.append(
                f"actual as? {expected_simple}  "
                f"// cast if {expected_simple} is a subtype of "
                f"{actual_simple}"
            )

    location = f" at {file}:{line}" if file else ""
    hint = (
        "TYPE MISMATCH (NOT a missing dependency, NOT an import error):\n"
        f"  actual type:   {actual}\n"
        f"  expected type: {expected}\n"
        f"  location:      {location or '(unspecified)'}\n\n"
        "This means the LIBRARY IS INSTALLED, the IMPORTS ARE FINE, but "
        "the values at this site have incompatible types. Do NOT add "
        "dependencies, do NOT change imports, do NOT remove the offending "
        "code. Convert the actual value to the expected type at the "
        "assignment/call site.\n\n"
        f"{conversion_block}"
        "If neither known conversion fits, inspect the API: does the "
        f"expected type {expected_simple or 'X'} have a constructor that "
        f"takes fields from {actual_simple or 'Y'}? If yes, wrap. If the "
        "types share a hierarchy, use `as` / `as?`. NEVER drop the "
        "feature to make the compiler happy.\n"
    )
    return hint, tuple(p for p in patterns if p), library


def classify(error_text: str, *, file: str = "", line: int = 0, column: int = 0) -> ClassifiedCompileError:
    """Pattern-match one compile error message into a ClassifiedCompileError.

    ``error_text`` is the compiler-emitted message (without the surrounding
    file:line: prefix when possible). ``file`` / ``line`` / ``column``
    come from the structured payload of the compile gate event.
    Returns ``kind="unknown"`` when no pattern matches; callers can fall
    back to the legacy raw-text repair flow.
    """
    if not error_text:
        return ClassifiedCompileError(kind="unknown", raw_message="")

    text = error_text.strip()

    # Kotlin structural breakage must be detected before the generic
    # unresolved-reference path, otherwise "Unresolved reference 'catch'"
    # is misclassified as a missing symbol and repair tries imports/renames.
    if _KOTLIN_STRUCTURAL_RE.search(text):
        hint = (
            "KOTLIN STRUCTURAL BREAKAGE (C10):\n"
            f"  location: {file}:{line}\n\n"
            "The compiler is reporting a parser or lexical-scope failure "
            "(for example `catch` outside a valid `try`, an unmatched "
            "lambda/callback block, or a broken parenthesis/brace chain). "
            "Do NOT treat this as a missing import or missing dependency. "
            "Repair must stay local to the nearest broken function/block: "
            "restore balanced braces/parentheses, keep callback/listener "
            "methods inside their owner object, keep Compose state at the "
            "Composable scope, and preserve all protected feature symbols.\n"
        )
        return ClassifiedCompileError(
            kind="kotlin_structural_breakage",
            raw_message=text,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
        )

    # Type mismatch (assignment form, the v13 OSMDroid case).
    m = _TYPE_MISMATCH_ASSIGN_RE.search(text)
    if not m:
        m = _TYPE_MISMATCH_INFER_RE.search(text)
    if m:
        actual = m.group(1).strip()
        expected = m.group(2).strip()
        hint, patterns, library = _build_type_mismatch_hint(
            actual, expected, file, line
        )
        return ClassifiedCompileError(
            kind="type_mismatch",
            raw_message=text,
            actual_type=actual,
            expected_type=expected,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
            library=library,
            suggested_patterns=patterns,
        )

    # Unresolved reference.
    m = _UNRESOLVED_RE.search(text)
    if m:
        symbol = m.group(1).strip()
        hint = (
            "UNRESOLVED REFERENCE (NAME-LOCK, L4f):\n"
            f"  symbol: {symbol}\n"
            f"  location: {file}:{line}\n\n"
            "A symbol named in your patch does not exist in the codebase. "
            "Either (a) restore the original spelling in the declaring file "
            "(if you renamed it), or (b) update this reference to use the "
            "declaring file's current name. Do NOT invent a third name. "
            "If the symbol is from an external library, verify the import "
            "path is correct — but do NOT add a dependency unless the "
            "imported package is genuinely missing from build.gradle/"
            "build.gradle.kts.\n"
        )
        return ClassifiedCompileError(
            kind="unresolved_reference",
            raw_message=text,
            symbol=symbol,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
            library=_detect_library_from_type(symbol),
        )

    # Receiver mismatch.
    m = _RECEIVER_RE.search(text)
    if m:
        receiver = m.group(1).strip()
        hint = (
            "RECEIVER MISMATCH:\n"
            f"  inferred receiver: {receiver}\n"
            f"  location: {file}:{line}\n\n"
            "A method/property was called on an object whose declared type "
            "does not expose it. Either (a) cast the receiver to the "
            "correct type before the call, or (b) use a different method "
            "that the receiver's actual type supports. Inspect the "
            "receiver class body to see which members exist.\n"
        )
        return ClassifiedCompileError(
            kind="receiver_mismatch",
            raw_message=text,
            receiver=receiver,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
        )

    # Overload mismatch.
    if _OVERLOAD_RE.search(text):
        hint = (
            "OVERLOAD MISMATCH:\n"
            f"  location: {file}:{line}\n\n"
            "Multiple overloads exist but the argument types don't match "
            "any uniquely. Inspect the listed candidates and pick the "
            "overload whose signature matches your arguments — convert "
            "arguments at the call site if needed (do NOT change the "
            "function signature).\n"
        )
        return ClassifiedCompileError(
            kind="overload_mismatch",
            raw_message=text,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
        )

    # Suspend misuse.
    m = _SUSPEND_RE.search(text)
    if m:
        symbol = m.group(1).strip()
        hint = (
            "SUSPEND FUNCTION MISUSE:\n"
            f"  symbol: {symbol}\n"
            f"  location: {file}:{line}\n\n"
            "Suspend functions can only be called from a coroutine or "
            "another suspend function. Either (a) wrap the call in "
            "LaunchedEffect / rememberCoroutineScope().launch / "
            "lifecycleScope.launch, or (b) mark the calling function "
            "with `suspend`. For Compose UI events, use "
            "rememberCoroutineScope.\n"
        )
        return ClassifiedCompileError(
            kind="suspend_misuse",
            raw_message=text,
            symbol=symbol,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
        )

    # Cannot infer type — usually a companion to Unresolved reference.
    if _INFER_TYPE_RE.search(text):
        hint = (
            "TYPE INFERENCE FAILURE:\n"
            f"  location: {file}:{line}\n\n"
            "Kotlin could not infer a parameter's type, almost always "
            "because a sibling expression at this site is also broken "
            "(see other compile errors on the same line). Fix the "
            "Unresolved reference / type mismatch first; the inference "
            "error usually disappears automatically. If it persists, "
            "annotate the lambda parameter with an explicit type, e.g. "
            "`{ marker: Marker -> ... }`.\n"
        )
        return ClassifiedCompileError(
            kind="cannot_infer_type",
            raw_message=text,
            file=file,
            line=line,
            column=column,
            repair_hint=hint,
        )

    # Fall through.
    return ClassifiedCompileError(
        kind="unknown",
        raw_message=text,
        file=file,
        line=line,
        column=column,
    )


def render_repair_hints(errors: list[ClassifiedCompileError]) -> str:
    """Render a list of classified errors into a single prompt block.

    Returns empty string if all errors are ``unknown`` (callers can
    skip the section entirely in that case).
    """
    sections: list[str] = []
    for idx, err in enumerate(errors, start=1):
        if err.kind == "unknown" or not err.repair_hint:
            continue
        sections.append(f"--- Compile error {idx} ({err.kind}) ---\n{err.repair_hint}")
    if not sections:
        return ""
    return (
        "STRUCTURED COMPILE ERROR ANALYSIS — read before fixing:\n\n"
        + "\n".join(sections)
    )
