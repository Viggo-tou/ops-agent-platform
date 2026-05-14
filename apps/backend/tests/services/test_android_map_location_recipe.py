from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.android_map_location_recipe import (  # noqa: E402
    try_generate_android_map_location_recipe,
)
from app.services.codegen import CodeGenerator  # noqa: E402
from app.services.structural_edit import validate_kotlin_structure  # noqa: E402


PLAN = {
    "domain_playbook_id": "android_map_location",
    "required_contracts": [
        {"contract_id": "map_ui_present"},
        {"contract_id": "user_can_select_location"},
        {"contract_id": "location_updates_form_state"},
        {"contract_id": "persisted_to_storage"},
        {"contract_id": "installed_library_only"},
        {"contract_id": "geocoder_lifecycle_safe"},
    ],
}


SIGNUP_SOURCE = """\
package demo

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import com.google.firebase.database.FirebaseDatabase
import java.util.*

@Composable
fun CustomerSignup() {
    var firstName by remember { mutableStateOf("") }
    var confirmPassword by remember { mutableStateOf("") }

    Column {
        Button(
            onClick = {
                val userData = mapOf(
                    "firstName" to firstName,
                    "houseNumber" to "",
                    "street" to "",
                    "area" to "",
                    "division" to "",
                    "district" to "",
                    "thana" to "",
                    "city" to "",
                    "country" to "",
                    "postcode" to "",
                    "notes" to "",
                    "latitude" to 0.0,
                    "longitude" to 0.0
                )
                FirebaseDatabase.getInstance().getReference("User").setValue(userData)
            }
        ) {
            Text("Create")
        }
    }
}
"""


KYC_SOURCE = """\
package demo

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import com.google.firebase.database.FirebaseDatabase

@Composable
fun HandymanKYCAddressForm() {
    var houseNumber by remember { mutableStateOf("") }
    var street by remember { mutableStateOf("") }
    var area by remember { mutableStateOf("") }
    var postCode by remember { mutableStateOf("") }
    var division by remember { mutableStateOf("") }
    var district by remember { mutableStateOf("") }
    var thana by remember { mutableStateOf("") }
    var city by remember { mutableStateOf("") }
    var country by remember { mutableStateOf("") }
    var note by remember { mutableStateOf("") }

    Column {
        Button(
            onClick = {
                val addressData = mapOf(
                    "houseNumber" to houseNumber,
                    "street" to street,
                    "area" to area,
                    "postCode" to postCode,
                    "division" to division,
                    "district" to district,
                    "thana" to thana,
                    "city" to city,
                    "country" to country,
                    "notes" to note
                )
                FirebaseDatabase.getInstance().getReference("Handyman").updateChildren(addressData)
            }
        ) {
            Text("Save")
        }
    }
}
"""


EXISTING_MAP_SOURCE = """\
package demo

import android.location.Geocoder
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import com.google.firebase.database.FirebaseDatabase
import org.osmdroid.events.MapEventsReceiver
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.MapEventsOverlay

@Composable
fun CustomerKYCAddressForm() {
    val context = LocalContext.current
    val geocoder = remember { Geocoder(context) }
    var latitude by remember { mutableStateOf(0.0) }
    var longitude by remember { mutableStateOf(0.0) }
    val coroutineScope = rememberCoroutineScope()

    Column {
        AndroidView(
            factory = { ctx ->
                MapView(ctx).apply {
                    overlays.add(MapEventsOverlay(object : MapEventsReceiver {
                        override fun singleTapConfirmedHelper(p: GeoPoint?): Boolean {
                            latitude = p?.latitude ?: latitude
                            longitude = p?.longitude ?: longitude
                            return true
                        }

                        override fun longPressHelper(p: GeoPoint?): Boolean = false
                    }))
                }
            }
        )

        Button(
            onClick = {
                val addressData = mapOf(
                    "latitude" to latitude,
                    "longitude" to longitude
                )
                FirebaseDatabase.getInstance().getReference("User").updateChildren(addressData)
            }
        ) {
            Text("Save")
        }
    }
}
"""


def test_recipe_adds_osmdroid_map_picker_and_wires_signup_payload():
    result = try_generate_android_map_location_recipe(
        file_path="CustomerSignup.kt",
        original_content=SIGNUP_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert "AndroidView(" in result.content
    assert "MapEventsOverlay(object : MapEventsReceiver" in result.content
    assert "singleTapConfirmedHelper" in result.content
    assert "Geocoder(ctx, Locale.getDefault())" in result.content
    assert "withContext(Dispatchers.IO)" in result.content
    assert '"houseNumber" to houseNumber' in result.content
    assert '"latitude" to latitude' in result.content
    assert '"longitude" to longitude' in result.content
    assert "eventsOverlay" not in result.content
    assert "streetNumber" not in result.content
    assert result.contract_coverage is not None
    assert {
        row["contract_id"]
        for row in result.contract_coverage["implemented_contracts"]
    } >= {
        "map_ui_present",
        "user_can_select_location",
        "location_updates_form_state",
        "persisted_to_storage",
    }


def test_recipe_adds_coordinates_to_existing_kyc_payload_with_valid_commas():
    result = try_generate_android_map_location_recipe(
        file_path="HandymanKYCAddressForm.kt",
        original_content=KYC_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert "var latitude by remember { mutableStateOf(0.0) }" in result.content
    assert "var longitude by remember { mutableStateOf(0.0) }" in result.content
    assert '"longitude" to longitude,' in result.content
    assert '"latitude" to latitude,' in result.content
    assert '"notes" to note,' in result.content
    assert "updateChildren(addressData)" in result.content


def test_recipe_preserves_existing_map_and_only_repairs_geocoder_locale():
    result = try_generate_android_map_location_recipe(
        file_path="CustomerKYCAddressForm.kt",
        original_content=EXISTING_MAP_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert "Geocoder(context, Locale.getDefault())" in result.content
    assert result.content.count("singleTapConfirmedHelper") == 1
    assert result.content.count("MapEventsOverlay(object : MapEventsReceiver") == 1


def test_codegen_uses_recipe_before_agent_loop_or_provider_paths():
    settings = SimpleNamespace(
        codegen_agent_mode="loop",
        codegen_provider="deepseek",
        primary_agent_provider="deepseek",
        codegen_output_format="auto",
        codegen_structural_kotlin_enabled=True,
        deepseek_api_key="test",
        deepseek_model="deepseek-test",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
    )
    generator = CodeGenerator(settings)
    generator._run_agent_loop = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("agent loop should not run")
    )
    generator._try_provider = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("provider path should not run")
    )

    result = generator.generate_patch(
        task_id="task-p69-19",
        plan_json={
            **PLAN,
            "must_touch_files": ["CustomerSignup.kt"],
            "allowed_paths": ["CustomerSignup.kt"],
        },
        context_files={"CustomerSignup.kt": SIGNUP_SOURCE},
    )

    assert result.provider_name == "harness:android_map_location_recipe"
    assert result.model_name == "deterministic-v1"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.files_changed == ["CustomerSignup.kt"]
    assert "+        AndroidView(" in result.diff
    assert result.contract_coverage is not None


def test_codegen_recipe_handles_single_file_batch_with_global_must_touch_plan():
    settings = SimpleNamespace(
        codegen_agent_mode="static",
        codegen_provider="deepseek",
        primary_agent_provider="deepseek",
        codegen_output_format="auto",
        codegen_structural_kotlin_enabled=True,
        deepseek_api_key="test",
        deepseek_model="deepseek-test",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
    )
    generator = CodeGenerator(settings)
    generator._try_provider = lambda **_: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("provider path should not run for recipe batch")
    )

    result = generator.generate_patch(
        task_id="task-p69-19-batch",
        plan_json={
            **PLAN,
            "must_touch_files": [
                "CustomerSignup.kt",
                "HandymanSignup.kt",
                "HandymanKYCAddressForm.kt",
            ],
            "allowed_paths": [
                "CustomerSignup.kt",
                "HandymanSignup.kt",
                "HandymanKYCAddressForm.kt",
            ],
        },
        context_files={"CustomerSignup.kt": SIGNUP_SOURCE},
    )

    assert result.provider_name == "harness:android_map_location_recipe"
    assert result.files_changed == ["CustomerSignup.kt"]
    assert "+        AndroidView(" in result.diff
