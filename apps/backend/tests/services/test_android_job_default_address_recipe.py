from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.android_job_default_address_recipe import (  # noqa: E402
    try_generate_android_job_default_address_recipe,
)
from app.services.codegen import CodeGenerator  # noqa: E402
from app.services.contract_coverage import (  # noqa: E402
    CoverageClaim,
    CoverageDeclaration,
    required_contracts_from_playbook,
    verify_coverage,
)
from app.services.domain_classifier import classify_domain  # noqa: E402
from app.services.structural_edit import validate_kotlin_structure  # noqa: E402


PLAN = {
    "domain_playbook_id": "android_job_default_address",
    "required_contracts": [
        {"contract_id": "saved_home_address_loaded"},
        {"contract_id": "job_location_prefilled"},
        {"contract_id": "saved_address_geocoded_to_map"},
        {"contract_id": "work_location_saved_to_job_only"},
        {"contract_id": "missing_home_address_safe"},
    ],
}


FRAGMENT_WITHOUT_PREFILL = """\
package demo

import android.location.Geocoder
import android.net.Uri
import android.os.Bundle
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.platform.ComposeView
import androidx.fragment.app.Fragment
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.example.handyman.utils.SessionManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.util.UUID

class JobPostingFragment : Fragment() {
    override fun onCreateView(inflater: Any, container: Any?, savedInstanceState: Bundle?): Any {
        val serviceName = "Plumbing"
        return ComposeView(requireContext()).apply {
            setContent {
                val navController = rememberNavController()
                val viewModel: JobPostingViewModel = viewModel()

                LaunchedEffect(serviceName) {
                    viewModel.serviceCategory = serviceName
                }

                NavHost(navController = navController, startDestination = "jobPostingDescription") {
                    composable("jobPostingDescription") {}
                    composable("jobPostingLocation") {}
                }
            }
        }
    }
}
"""


FRAGMENT_WITH_PARTIAL_PREFILL = """\
package demo

import android.location.Geocoder
import android.os.Bundle
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.platform.ComposeView
import androidx.fragment.app.Fragment
import androidx.lifecycle.viewmodel.compose.viewModel
import com.example.handyman.utils.SessionManager
import com.google.android.gms.tasks.Tasks
import com.google.firebase.database.FirebaseDatabase
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class JobPostingFragment : Fragment() {
    override fun onCreateView(inflater: Any, container: Any?, savedInstanceState: Bundle?): Any {
        return ComposeView(requireContext()).apply {
            setContent {
                val viewModel: JobPostingViewModel = viewModel()
                LaunchedEffect(Unit) {
                    if (!viewModel.isEditing && viewModel.locationAddress.isBlank() &&
                        viewModel.latitude == 0.0 && viewModel.longitude == 0.0) {
                        val ctx = requireContext()
                        val userId = SessionManager.currentUserID
                            ?: SessionManager.getLoggedInUserId(ctx)
                        if (userId.isNotBlank()) {
                            try {
                                val snapshot = withContext(Dispatchers.IO) {
                                    Tasks.await(
                                        FirebaseDatabase.getInstance()
                                            .getReference("User").child(userId).get()
                                    )
                                }
                                val parts = listOf("houseNumber", "street", "area", "city", "country")
                                    .map { snapshot.child(it).getValue(String::class.java).orEmpty() }
                                    .filter { it.isNotBlank() }
                                if (parts.isNotEmpty() && viewModel.locationAddress.isBlank()) {
                                    val homeAddress = parts.joinToString(", ")
                                    viewModel.locationAddress = homeAddress
                                    val results = withContext(Dispatchers.IO) {
                                        runCatching {
                                            Geocoder(ctx).getFromLocationName(homeAddress, 1)
                                        }.getOrNull()
                                    }
                                    if (!results.isNullOrEmpty() &&
                                        viewModel.latitude == 0.0 && viewModel.longitude == 0.0) {
                                        viewModel.latitude = results[0].latitude
                                        viewModel.longitude = results[0].longitude
                                    }
                                }
                            } catch (_: Exception) {
                            }
                        }
                    }
                }
            }
        }
    }
}
"""


FLOW_SOURCE = """\
package demo

import android.location.Geocoder
import androidx.compose.runtime.*
import androidx.compose.ui.viewinterop.AndroidView
import org.osmdroid.util.GeoPoint
import org.osmdroid.views.MapView
import org.osmdroid.views.overlay.Marker

@Composable
fun JobPostingLocationScreen(navController: NavController, viewModel: JobPostingViewModel) {
    val context = LocalContext.current
    val geocoder = remember { Geocoder(context) }

    fun searchAddress(address: String, mapView: MapView) {
        val results = geocoder.getFromLocationName(address, 1)
    }

    AndroidView(
        factory = { ctx ->
            MapView(ctx).apply {
                val startPoint = if (viewModel.latitude != 0.0)
                    GeoPoint(viewModel.latitude, viewModel.longitude)
                    else GeoPoint(23.6850, 90.3563)
                controller.setCenter(startPoint)
            }
        },
        update = { mapView ->
            // Ensure marker stays synced if viewModel changes from elsewhere
        }
    )
}

@Composable
fun JobPostingReviewScreen(navController: NavController, viewModel: JobPostingViewModel) {
    val job = Job(
        jobLocation = viewModel.locationAddress,
        latitude = viewModel.latitude,
        longitude = viewModel.longitude
    )
    FirebaseDatabase.getInstance().reference.updateChildren(mapOf("/Job/id" to job))
}
"""


def _claim_rows(coverage: dict, key: str) -> list[CoverageClaim]:
    rows = []
    for item in coverage.get(key) or []:
        rows.append(
            CoverageClaim(
                contract_id=str(item.get("contract_id") or ""),
                file_path=str(item.get("file_path") or ""),
                evidence_quote=str(item.get("evidence_quote") or ""),
                reason=str(item.get("reason") or ""),
            )
        )
    return rows


def test_fragment_recipe_inserts_saved_home_prefill_without_profile_writeback():
    result = try_generate_android_job_default_address_recipe(
        file_path="app/src/main/java/com/example/handyman/JobPostingFragment.kt",
        original_content=FRAGMENT_WITHOUT_PREFILL,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert 'getReference("User").child(userId).get()' in result.content
    assert 'snapshot.child("address")' in result.content
    assert "viewModel.locationAddress = homeAddress" in result.content
    assert "Geocoder(ctx, Locale.getDefault())" in result.content
    assert "getFromLocationName(homeAddress, 1)" in result.content
    assert "viewModel.citySuburb = location.locality" in result.content
    assert "viewModel.latitude == 0.0 && viewModel.longitude == 0.0" not in result.content
    assert "catch (_: Exception)" in result.content
    assert "lastLatitude" not in result.content
    assert "lastLongitude" not in result.content
    assert "lastAddress" not in result.content
    assert "updateChildren" not in result.content


def test_fragment_recipe_repairs_existing_prefill_locale_and_direct_address_source():
    result = try_generate_android_job_default_address_recipe(
        file_path="app/src/main/java/com/example/handyman/JobPostingFragment.kt",
        original_content=FRAGMENT_WITH_PARTIAL_PREFILL,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert "import java.util.Locale" in result.content
    assert 'val directHomeAddress = snapshot.child("address")' in result.content
    assert "val homeAddress = directHomeAddress.ifBlank" in result.content
    assert "Geocoder(ctx, Locale.getDefault()).getFromLocationName(homeAddress, 1)" in result.content
    assert "viewModel.citySuburb = location.locality" in result.content
    assert "viewModel.latitude == 0.0 && viewModel.longitude == 0.0" not in result.content


def test_flow_recipe_syncs_prefilled_coordinates_to_existing_map():
    result = try_generate_android_job_default_address_recipe(
        file_path="app/src/main/java/com/example/handyman/JobPostingFlow.kt",
        original_content=FLOW_SOURCE,
        plan_json=PLAN,
    )

    assert result is not None
    assert validate_kotlin_structure(result.content) == []
    assert "Geocoder(context, Locale.getDefault())" in result.content
    assert "mapView.controller.animateTo(point)" in result.content
    assert "Marker(mapView).also { mapView.overlays.add(it) }" in result.content
    assert "jobLocation = viewModel.locationAddress" in result.content
    assert 'getReference("User")' not in result.content
    assert "lastLatitude" not in result.content


def test_codegen_uses_job_default_recipe_and_contract_coverage_verifies():
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

    fragment_path = "app/src/main/java/com/example/handyman/JobPostingFragment.kt"
    flow_path = "app/src/main/java/com/example/handyman/JobPostingFlow.kt"
    result = generator.generate_patch(
        task_id="task-p69-17",
        plan_json={
            **PLAN,
            "must_touch_files": [fragment_path, flow_path],
            "allowed_paths": [fragment_path, flow_path],
        },
        context_files={
            fragment_path: FRAGMENT_WITHOUT_PREFILL,
            flow_path: FLOW_SOURCE,
        },
    )

    assert result.provider_name == "harness:android_job_default_address_recipe"
    assert result.model_name == "deterministic-v1"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.files_changed == [fragment_path, flow_path]
    assert result.contract_coverage is not None

    playbook = classify_domain(
        request_text=(
            "Load default address when creating jobs; pre-fill job location "
            "from saved home address"
        ),
        project_tag="handymanapp",
    )
    required = required_contracts_from_playbook(playbook)
    declaration = CoverageDeclaration(
        implemented_contracts=_claim_rows(result.contract_coverage, "implemented_contracts"),
        verified_no_change_contracts=_claim_rows(
            result.contract_coverage,
            "verified_no_change_contracts",
        ),
        unimplemented_contracts=[],
    )
    snapshots = {
        fragment_path: FRAGMENT_WITHOUT_PREFILL,
        flow_path: FLOW_SOURCE,
    }
    verdict = verify_coverage(
        declaration=declaration,
        required=required,
        diff_text=result.diff,
        file_snapshots=snapshots,
        patched_files=snapshots,
    )

    assert verdict.ok, verdict.to_dict()
    assert set(verdict.verified_implemented + verdict.verified_no_change) == {
        "saved_home_address_loaded",
        "job_location_prefilled",
        "saved_address_geocoded_to_map",
        "work_location_saved_to_job_only",
        "missing_home_address_safe",
    }
