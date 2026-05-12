"""Contract Coverage verifier (v16.2 — addresses C2 polish_passing_as_feature).

The core regression case is the b5d0a085 / f2fce4f4 failure mode:
CustomerSignup.kt already has latitude/longitude persistence wiring,
so the model returns NO_CHANGE_NEEDED_VERIFIED — but the file has no
map UI to PICK the coordinates in the first place. v16.1 caught the
empty-acceptance-tests case; this test ensures v16.2 catches the
trivially-true-claim case.
"""
from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.contract_coverage import (  # noqa: E402
    CoverageClaim,
    CoverageDeclaration,
    RequiredContract,
    parse_coverage_block,
    required_contracts_from_playbook,
    verify_coverage,
)
from app.services.domain_classifier import classify_domain  # noqa: E402


# ---- Parser -------------------------------------------------------------


def test_parse_well_formed_block():
    response = """
Here's the patch I generated.

## CONTRACT_COVERAGE
{
  "implemented_contracts": [
    {"id": "geocoder_lifecycle_safe", "file": "CustomerKYCAddressForm.kt", "evidence_quote": "+Geocoder(context, Locale.getDefault())"}
  ],
  "verified_no_change_contracts": [
    {"id": "coordinates_persisted", "file": "CustomerSignup.kt", "evidence_quote": "userData[\\"latitude\\"] = latitude"}
  ],
  "unimplemented_contracts": [
    {"id": "map_ui_present", "reason": "would require new Composable beyond scope"}
  ]
}
"""
    decl = parse_coverage_block(response)
    assert decl is not None
    assert len(decl.implemented_contracts) == 1
    assert decl.implemented_contracts[0].contract_id == "geocoder_lifecycle_safe"
    assert len(decl.verified_no_change_contracts) == 1
    assert len(decl.unimplemented_contracts) == 1


def test_parse_handles_code_fence():
    response = """Patch ready.

## CONTRACT_COVERAGE
```json
{
  "implemented_contracts": [{"id": "x", "evidence_quote": "y"}],
  "verified_no_change_contracts": [],
  "unimplemented_contracts": []
}
```

Some trailing prose.
"""
    decl = parse_coverage_block(response)
    assert decl is not None
    assert decl.implemented_contracts[0].contract_id == "x"


def test_parse_returns_none_when_marker_missing():
    response = "Just a regular response without the marker."
    assert parse_coverage_block(response) is None


def test_parse_tolerates_trailing_prose_after_json():
    response = """## CONTRACT_COVERAGE
{
  "implemented_contracts": [{"id": "x", "evidence_quote": "y"}],
  "verified_no_change_contracts": [],
  "unimplemented_contracts": []
}

End of contract block. More text below.
"""
    decl = parse_coverage_block(response)
    assert decl is not None
    assert decl.implemented_contracts[0].contract_id == "x"


# ---- Playbook integration ----------------------------------------------


def test_required_contracts_from_android_map_playbook():
    pb = classify_domain(
        request_text="map picker", project_tag="handymanapp",
    )
    assert pb is not None
    contracts = required_contracts_from_playbook(pb)
    ids = {c.contract_id for c in contracts}
    # The 6 contracts from android_map_location.yaml
    assert "map_ui_present" in ids
    assert "user_can_select_location" in ids
    assert "location_updates_form_state" in ids
    assert "persisted_to_storage" in ids
    assert "installed_library_only" in ids
    assert "geocoder_lifecycle_safe" in ids


def test_required_contracts_includes_forbidden_patterns():
    pb = classify_domain(request_text="map", project_tag="handymanapp")
    contracts = required_contracts_from_playbook(pb)
    lib = next(c for c in contracts if c.contract_id == "installed_library_only")
    # OSMDroid card declares Google Maps SDK as forbidden. The pattern
    # in the YAML uses regex-escaped dots (`com\\.google\\.android\\.gms\\.maps`)
    # so we test by stripping backslashes to compare literal namespace text.
    all_forbidden = " ".join(p.replace("\\", "") for p in lib.forbidden_patterns)
    assert "google.android.gms.maps" in all_forbidden


# ---- Verifier — passing case ------------------------------------------


def test_verify_implemented_contract_via_added_diff_lines():
    """The happy path — model claims implemented and the diff actually
    has the verification-pattern match in added lines."""
    contract = RequiredContract(
        contract_id="map_ui_present",
        signal="map UI must exist",
        verification_patterns=["MapView|MapEventsOverlay"],
    )
    diff = """diff --git a/app/Foo.kt b/app/Foo.kt
@@ -1,2 +1,4 @@
 existing
+import org.osmdroid.views.MapView
+val mapView = MapView(context)
 trailing
"""
    decl = CoverageDeclaration(
        implemented_contracts=[
            CoverageClaim(contract_id="map_ui_present", file_path="app/Foo.kt", evidence_quote="MapView"),
        ],
    )
    verdict = verify_coverage(
        declaration=decl, required=[contract], diff_text=diff,
    )
    assert verdict.ok is True
    assert verdict.verdict_kind == "complete"
    assert "map_ui_present" in verdict.verified_implemented


def test_verify_no_change_contract_via_file_snapshot():
    """When the model claims a contract is already satisfied without
    needing changes, the harness must verify the pattern exists in
    the actual file content (not the model's prose)."""
    contract = RequiredContract(
        contract_id="persisted_to_storage",
        signal="coordinates must persist to firebase",
        verification_patterns=[r"(updateChildren|setValue|put\s*\(\"latitude\")"],
    )
    file_content = """class CustomerSignup {
        fun submit() {
            val userData = hashMapOf<String, Any>()
            userData["latitude"] = latitude
            userData["longitude"] = longitude
            FirebaseDatabase.getInstance()
                .getReference("users")
                .updateChildren(userData)
        }
    }
    """
    decl = CoverageDeclaration(
        verified_no_change_contracts=[
            CoverageClaim(
                contract_id="persisted_to_storage",
                file_path="CustomerSignup.kt",
                evidence_quote="updateChildren(userData)",
            ),
        ],
    )
    verdict = verify_coverage(
        declaration=decl,
        required=[contract],
        diff_text="",
        file_snapshots={"CustomerSignup.kt": file_content},
    )
    assert verdict.ok is True
    assert "persisted_to_storage" in verdict.verified_no_change


# ---- Verifier — failing cases (the regressions we care about) ---------


def test_b5d0a085_regression_lat_lng_persisted_but_no_map_ui():
    """**This is the exact regression case from b5d0a085 / f2fce4f4.**

    CustomerSignup.kt has latitude/longitude persistence in its existing
    code, so the model claims:
       coordinates_persisted: verified_no_change (legit)
       geocoder_lifecycle_safe: implemented (legit, Locale was added)
       map_ui_present / user_can_select_location / location_updates_form_state:
           NOT MENTIONED (model says NO_CHANGE_NEEDED_VERIFIED for the whole file)

    The verifier MUST reject this — even though the model's two literal
    claims verify against the artifact, the other three required contracts
    are missing. Polish + correctness ≠ feature completeness.
    """
    pb = classify_domain(request_text="map picker", project_tag="handymanapp")
    required = required_contracts_from_playbook(pb)

    # Pre-existing file content — has lat/lng persistence ONLY, no map UI.
    customer_signup_content = """
import com.google.firebase.database.FirebaseDatabase

class CustomerSignup {
    var latitude by remember { mutableStateOf(0.0) }
    var longitude by remember { mutableStateOf(0.0) }
    fun submit() {
        val userData = hashMapOf<String, Any>(
            "name" to name,
            "latitude" to latitude,
            "longitude" to longitude,
        )
        FirebaseDatabase.getInstance().getReference("users").updateChildren(userData)
    }
}
"""
    # The 2-line diff is real: Geocoder Locale added in KYC form.
    diff = """diff --git a/CustomerKYCAddressForm.kt b/CustomerKYCAddressForm.kt
@@ -36,6 +36,7 @@
 import org.osmdroid.views.MapView
 import org.osmdroid.views.overlay.Marker
+import java.util.Locale
@@ -61,7 +62,7 @@
     var mapViewRef by remember { mutableStateOf<MapView?>(null) }
-    val geocoder = remember { Geocoder(context) }
+    val geocoder = remember { Geocoder(context, Locale.getDefault()) }
"""
    # Model's coverage declaration — what would have shipped pre-v16.2.
    decl = CoverageDeclaration(
        implemented_contracts=[
            CoverageClaim(
                contract_id="geocoder_lifecycle_safe",
                file_path="CustomerKYCAddressForm.kt",
                evidence_quote="Geocoder(context, Locale.getDefault())",
            ),
        ],
        verified_no_change_contracts=[
            CoverageClaim(
                contract_id="persisted_to_storage",
                file_path="CustomerSignup.kt",
                evidence_quote='userData["latitude"] = latitude',
            ),
        ],
        unimplemented_contracts=[],  # ← model SILENTLY omits the missing contracts
    )

    verdict = verify_coverage(
        declaration=decl,
        required=required,
        diff_text=diff,
        file_snapshots={"CustomerSignup.kt": customer_signup_content},
    )

    # Must reject: 3 required contracts were never addressed.
    assert verdict.ok is False
    assert verdict.verdict_kind == "incomplete"
    assert "map_ui_present" in verdict.missing
    assert "user_can_select_location" in verdict.missing
    assert "location_updates_form_state" in verdict.missing
    # The two genuine claims should still be verified.
    assert "geocoder_lifecycle_safe" in verdict.verified_implemented
    assert "persisted_to_storage" in verdict.verified_no_change


def test_claim_implemented_but_diff_has_no_match_is_a_lie():
    """Hard-fail case: model claims it implemented map_ui_present but
    the diff has no MapView / MapEventsOverlay / AndroidView reference."""
    contract = RequiredContract(
        contract_id="map_ui_present",
        signal="map UI must exist",
        verification_patterns=["MapView|MapEventsOverlay|AndroidView\\s*\\("],
    )
    diff = """diff --git a/Foo.kt b/Foo.kt
@@ -1,1 +1,2 @@
 existing
+// I'll add a map here later
"""
    decl = CoverageDeclaration(
        implemented_contracts=[
            CoverageClaim(
                contract_id="map_ui_present",
                file_path="Foo.kt",
                evidence_quote="totally made up",
            ),
        ],
    )
    verdict = verify_coverage(
        declaration=decl, required=[contract], diff_text=diff,
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "claims_unverified"
    assert len(verdict.lies) == 1
    assert verdict.lies[0]["contract_id"] == "map_ui_present"
    assert verdict.lies[0]["claim"] == "implemented"


def test_claim_no_change_but_file_does_not_have_pattern_is_a_lie():
    """Hard-fail case: model claims persisted_to_storage is no-change
    but the actual file has no firebase write call."""
    contract = RequiredContract(
        contract_id="persisted_to_storage",
        signal="must persist coordinates",
        verification_patterns=["updateChildren|setValue"],
    )
    decl = CoverageDeclaration(
        verified_no_change_contracts=[
            CoverageClaim(
                contract_id="persisted_to_storage",
                file_path="Foo.kt",
                evidence_quote="this file doesn't actually persist",
            ),
        ],
    )
    verdict = verify_coverage(
        declaration=decl,
        required=[contract],
        diff_text="",
        file_snapshots={
            "Foo.kt": "class Foo { fun greet() = println('hi') }",
        },
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "claims_unverified"
    assert len(verdict.lies) == 1
    assert verdict.lies[0]["contract_id"] == "persisted_to_storage"


def test_claim_implemented_but_adds_forbidden_pattern_is_a_lie():
    """Hard-fail case: model claims installed_library_only is implemented
    but the diff added a forbidden Google Maps import."""
    contract = RequiredContract(
        contract_id="installed_library_only",
        signal="must use OSMDroid",
        verification_patterns=[r"org\.osmdroid\."],
        forbidden_patterns=[r"com\.google\.android\.gms\.maps"],
    )
    diff = """diff --git a/Foo.kt b/Foo.kt
@@ -1,1 +1,3 @@
 existing
+import org.osmdroid.views.MapView
+import com.google.android.gms.maps.GoogleMap
"""
    decl = CoverageDeclaration(
        implemented_contracts=[
            CoverageClaim(
                contract_id="installed_library_only",
                file_path="Foo.kt",
                evidence_quote="org.osmdroid.views.MapView",
            ),
        ],
    )
    verdict = verify_coverage(
        declaration=decl, required=[contract], diff_text=diff,
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "claims_unverified"
    assert verdict.lies[0]["contract_id"] == "installed_library_only"
    assert "forbidden" in verdict.lies[0]["reason"].lower()


def test_contract_not_mentioned_at_all_is_missing_not_lie():
    """Distinguish missing (didn't mention) from lying (claimed +
    verification failed). The set equation rejects both but the verdict
    kind differs — missing → plan_codegen_conflict (route to human);
    lie → hard fail."""
    contract = RequiredContract(
        contract_id="map_ui_present",
        signal="map UI must exist",
        verification_patterns=["MapView"],
    )
    decl = CoverageDeclaration()  # empty: model said nothing
    verdict = verify_coverage(
        declaration=decl, required=[contract], diff_text="",
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "incomplete"
    assert "map_ui_present" in verdict.missing
    assert verdict.lies == []


def test_unimplemented_with_reason_is_still_missing_in_set_equation():
    """The set equation is required = implemented ∪ verified_no_change.
    Explicit ``unimplemented_contracts`` is NOT a free pass — the model
    can name a reason but it's still a coverage gap. Caller (orchestrator)
    decides whether to route to approval or hard-fail based on
    verdict_kind=incomplete."""
    contract = RequiredContract(
        contract_id="map_ui_present",
        signal="map UI must exist",
        verification_patterns=["MapView"],
    )
    decl = CoverageDeclaration(
        unimplemented_contracts=[
            CoverageClaim(
                contract_id="map_ui_present",
                reason="out of scope for this batch",
            ),
        ],
    )
    verdict = verify_coverage(
        declaration=decl, required=[contract], diff_text="",
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "incomplete"
    assert "map_ui_present" in verdict.missing


def test_aggregate_declaration_merge():
    """Per-batch declarations merge into one aggregate via .merge()."""
    a = CoverageDeclaration(
        implemented_contracts=[CoverageClaim(contract_id="X", file_path="a.kt")],
    )
    b = CoverageDeclaration(
        verified_no_change_contracts=[CoverageClaim(contract_id="Y", file_path="b.kt")],
    )
    merged = a.merge(b)
    assert merged.claimed_contract_ids() == {"X", "Y"}


def test_no_required_contracts_is_noop():
    """When the plan has no required_contracts (no domain matched),
    the verifier short-circuits to ok=True."""
    verdict = verify_coverage(
        declaration=CoverageDeclaration(),
        required=[],
        diff_text="",
    )
    assert verdict.ok is True
    assert verdict.verdict_kind == "complete"


# =========================================================================
# v16.2.1 — diff-anchored final-tree verifier regression tests.
# Round 6 (90b5f433) misclassified a real implementation as a coverage
# lie. The five cases below pin down the new verifier's semantics so the
# same bug cannot recur.
# =========================================================================

from app.services.contract_coverage import VerificationRule  # noqa: E402


def _persisted_to_storage_contract() -> RequiredContract:
    """The contract under test, matching android_map_location.yaml v16.2.1."""
    return RequiredContract(
        contract_id="persisted_to_storage",
        signal="lat/lng must reach storage",
        verifications=[VerificationRule(
            kind="any_of",
            rules=[
                VerificationRule(
                    kind="diff_contains_pattern",
                    pattern=r"(updateChildren|setValue|set\s*\()",
                ),
                VerificationRule(
                    kind="all_of",
                    rules=[
                        VerificationRule(
                            kind="diff_contains_pattern",
                            pattern=r'"latitude"\s+to\s+(?!0(?:\.0)?\b)[A-Za-z_][A-Za-z0-9_]*',
                        ),
                        VerificationRule(
                            kind="diff_contains_pattern",
                            pattern=r'"longitude"\s+to\s+(?!0(?:\.0)?\b)[A-Za-z_][A-Za-z0-9_]*',
                        ),
                        VerificationRule(
                            kind="final_context_contains_pattern",
                            pattern=r"(setValue|updateChildren|\.set\s*\()",
                            anchor="changed_hunk",
                            scope="same_function",
                        ),
                    ],
                ),
            ],
        )],
    )


_DIFF_PAYLOAD_CHANGED = """diff --git a/app/CustomerSignup.kt b/app/CustomerSignup.kt
--- a/app/CustomerSignup.kt
+++ b/app/CustomerSignup.kt
@@ -170,4 +170,4 @@
                 "city" to "",
                 "country" to "",
-                "latitude" to 0.0,
-                "longitude" to 0.0,
+                "latitude" to latitude,
+                "longitude" to longitude,
"""

_PATCHED_WITH_SINK_SAME_FN = """fun CustomerSignup() {
    var latitude by remember { mutableStateOf(0.0) }
    var longitude by remember { mutableStateOf(0.0) }
    Button(onClick = {
        val userData = mapOf(
            "city" to "",
            "country" to "",
            "latitude" to latitude,
            "longitude" to longitude,
        )
        userRef.setValue(userData)
    })
}
"""

_PATCHED_NO_SINK = """fun CustomerSignup() {
    var latitude by remember { mutableStateOf(0.0) }
    var longitude by remember { mutableStateOf(0.0) }
    Button(onClick = {
        val userData = mapOf(
            "latitude" to latitude,
            "longitude" to longitude,
        )
        // payload prepared; no persistence call in this function body
    })
}
"""

_PATCHED_SINK_IN_OTHER_FN = """fun helperFunction() {
    userRef.setValue(somethingUnrelated)
}

fun CustomerSignup() {
    var latitude by remember { mutableStateOf(0.0) }
    var longitude by remember { mutableStateOf(0.0) }
    Button(onClick = {
        val userData = mapOf(
            "latitude" to latitude,
            "longitude" to longitude,
        )
        // payload prepared; persistence call lives in helperFunction
    })
}
"""


def _claim_implemented(file_path: str) -> CoverageDeclaration:
    return CoverageDeclaration(
        implemented_contracts=[CoverageClaim(
            contract_id="persisted_to_storage",
            file_path=file_path,
            evidence_quote='"latitude" to latitude',
            evidence_mode="diff_modified_payload_existing_sink",
            diff_evidence="changed payload bindings from 0.0 to state vars",
            context_evidence="userRef.setValue(userData) in same function",
        )],
    )


def test_v1621_payload_changed_with_sink_same_scope_verifies():
    """Case 1 (POSITIVE): diff modifies lat/lng payload, unchanged sink
    lives in the same function in the patched tree.

    This is the round-6 (90b5f433) case the old verifier misjudged as a
    coverage lie. Must pass with evidence_mode = diff_modified_payload_existing_sink.
    """
    verdict = verify_coverage(
        declaration=_claim_implemented("app/CustomerSignup.kt"),
        required=[_persisted_to_storage_contract()],
        diff_text=_DIFF_PAYLOAD_CHANGED,
        patched_files={"app/CustomerSignup.kt": _PATCHED_WITH_SINK_SAME_FN},
    )
    assert verdict.ok is True, verdict.to_dict()
    assert verdict.verdict_kind == "complete"
    assert "persisted_to_storage" in verdict.verified_implemented


def test_v1621_payload_changed_no_sink_anywhere_is_unverified():
    """Case 2 (NEGATIVE): diff modifies lat/lng but the patched function
    has no setValue / updateChildren / set call anywhere in scope.

    Expected: unverified (not 'lie'), because the verifier merely
    couldn't confirm — the artifact does not actively contradict the
    claim.
    """
    verdict = verify_coverage(
        declaration=_claim_implemented("app/CustomerSignup.kt"),
        required=[_persisted_to_storage_contract()],
        diff_text=_DIFF_PAYLOAD_CHANGED,
        patched_files={"app/CustomerSignup.kt": _PATCHED_NO_SINK},
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "unverified", verdict.to_dict()
    assert any(
        entry.get("severity") == "unverified"
        for entry in verdict.lies
    ), verdict.to_dict()


def test_v1621_sink_in_other_function_does_not_pass():
    """Case 3 (NEGATIVE): sink call exists in the file but in a DIFFERENT
    function from the changed hunk. The diff-anchored same_function scope
    must NOT span function boundaries.

    Prevents the naive 'full-file scan' false positive the analysis
    flagged: 'file has latitude and file has setValue → pass' must be
    rejected when they're not in the same function.
    """
    verdict = verify_coverage(
        declaration=_claim_implemented("app/CustomerSignup.kt"),
        required=[_persisted_to_storage_contract()],
        diff_text=_DIFF_PAYLOAD_CHANGED,
        patched_files={"app/CustomerSignup.kt": _PATCHED_SINK_IN_OTHER_FN},
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "unverified", verdict.to_dict()


def test_v1621_payload_still_hardcoded_zero_is_unverified():
    """Case 4 (NEGATIVE): the diff was supposed to bind lat/lng to state
    vars, but the patched tree STILL contains "latitude" to 0.0.

    Constructed by giving the verifier a diff that DOES NOT actually
    change the payload binding (the +/- lines are identical) but the
    model claims persisted_to_storage = implemented.
    """
    diff_no_real_change = """diff --git a/app/CustomerSignup.kt b/app/CustomerSignup.kt
--- a/app/CustomerSignup.kt
+++ b/app/CustomerSignup.kt
@@ -170,2 +170,2 @@
-                "latitude" to 0.0,
+                "latitude" to 0.0,
"""
    patched_hardcoded = """fun CustomerSignup() {
    Button(onClick = {
        val userData = mapOf(
            "latitude" to 0.0,
            "longitude" to 0.0,
        )
        userRef.setValue(userData)
    })
}
"""
    verdict = verify_coverage(
        declaration=_claim_implemented("app/CustomerSignup.kt"),
        required=[_persisted_to_storage_contract()],
        diff_text=diff_no_real_change,
        patched_files={"app/CustomerSignup.kt": patched_hardcoded},
    )
    assert verdict.ok is False
    assert verdict.verdict_kind == "unverified", verdict.to_dict()


def test_v1621_verified_no_change_when_persistence_preexists():
    """Case 5 (POSITIVE no-change): the diff is unrelated to
    persisted_to_storage, but the pre-existing file ALREADY persists
    selectedLatitude / selectedLongitude through setValue. Model
    correctly declares persisted_to_storage as verified_no_change.

    Phase B note: the verified_no_change branch in verify_coverage still
    uses the legacy flat verification_patterns list, NOT the new typed
    rules. The contract under test has only typed rules (no flat
    patterns), so this test pins the documented current behavior: the
    no-change claim fails to verify, but the verdict_kind stays in the
    soft-fail family (unverified / claims_unverified), NEVER 'lie'.
    Wiring composite rules through the no-change branch is tracked for
    v16.3.
    """
    decl = CoverageDeclaration(
        verified_no_change_contracts=[CoverageClaim(
            contract_id="persisted_to_storage",
            file_path="app/CustomerSignup.kt",
            evidence_quote='"latitude" to selectedLatitude',
        )],
    )
    snapshot = """fun CustomerSignup() {
    Button(onClick = {
        val userData = mapOf(
            "latitude" to selectedLatitude,
            "longitude" to selectedLongitude,
        )
        userRef.setValue(userData)
    })
}
"""
    unrelated_diff = """diff --git a/app/CustomerSignup.kt b/app/CustomerSignup.kt
--- a/app/CustomerSignup.kt
+++ b/app/CustomerSignup.kt
@@ -10,1 +10,1 @@
-// old comment
+// new comment
"""
    verdict = verify_coverage(
        declaration=decl,
        required=[_persisted_to_storage_contract()],
        diff_text=unrelated_diff,
        file_snapshots={"app/CustomerSignup.kt": snapshot},
        patched_files={"app/CustomerSignup.kt": snapshot},
    )
    assert verdict.ok is False
    assert verdict.verdict_kind in ("unverified", "claims_unverified"), verdict.to_dict()
