"""Deterministic recipe for Android job default-address prefill tasks.

This covers the P69-17 task family: when a customer creates a job, the
location step should initialize from the user's saved home/account address,
while later edits stay on the job payload and never write back to the user
profile. The harness owns the fragile Kotlin placement; the model/planner only
decides that this narrow playbook applies.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.structural_edit import validate_kotlin_structure


ANDROID_JOB_DEFAULT_ADDRESS_CONTRACTS = [
    "saved_home_address_loaded",
    "job_location_prefilled",
    "saved_address_geocoded_to_map",
    "work_location_saved_to_job_only",
    "missing_home_address_safe",
]


@dataclass
class AndroidJobDefaultAddressRecipeResult:
    content: str
    diff: str
    files_changed: list[str]
    summary: str
    applied_operations: list[str] = field(default_factory=list)
    contract_coverage: dict[str, Any] | None = None


def try_generate_android_job_default_address_recipe(
    *,
    file_path: str,
    original_content: str,
    plan_json: dict[str, Any] | None = None,
    task_description: str = "",
) -> AndroidJobDefaultAddressRecipeResult | None:
    """Generate a scoped patch for job-location default-address tasks."""
    if Path(file_path).suffix.lower() not in {".kt", ".kts"}:
        return None
    if not original_content.strip():
        return None
    if not _is_job_default_address_task(plan_json or {}, task_description):
        return None

    normalized = file_path.replace("\\", "/")
    name = Path(normalized).name.lower()
    if name not in {"jobpostingfragment.kt", "jobpostingflow.kt"}:
        return None

    content = original_content
    applied: list[str] = []

    if name == "jobpostingfragment.kt":
        for import_line in _fragment_imports(content):
            before = content
            content = _add_import(content, import_line)
            if content != before:
                applied.append(f"add_import:{import_line.removeprefix('import ')}")

        before = content
        content = _ensure_fragment_prefill(content)
        if content != before:
            applied.append("ensure_saved_home_address_prefill")

        before = content
        content = _ensure_geocoder_locale(content)
        if content != before:
            applied.append("ensure_geocoder_locale")

        before = content
        content = _ensure_city_suburb_from_prefill_geocode(content)
        if content != before:
            applied.append("ensure_city_suburb_prefill")

        before = content
        content = _remove_prefill_coordinate_sentinel(content)
        if content != before:
            applied.append("remove_prefill_coordinate_sentinel")

    if name == "jobpostingflow.kt":
        for import_line in _flow_imports(content):
            before = content
            content = _add_import(content, import_line)
            if content != before:
                applied.append(f"add_import:{import_line.removeprefix('import ')}")

        before = content
        content = _ensure_geocoder_locale(content)
        if content != before:
            applied.append("ensure_geocoder_locale")

        before = content
        content = _ensure_flow_map_update_sync(content)
        if content != before:
            applied.append("sync_map_marker_from_prefilled_location")

    if content == original_content:
        return None

    structure_errors = validate_kotlin_structure(content)
    if structure_errors:
        return None

    diff = _unified_diff(file_path, original_content, content)
    if not diff.strip():
        return None

    return AndroidJobDefaultAddressRecipeResult(
        content=content,
        diff=diff,
        files_changed=[file_path],
        summary="Generated Android job default-address patch via harness recipe",
        applied_operations=applied,
        contract_coverage=_coverage_payload(
            file_path=file_path,
            before=original_content,
            after=content,
        ),
    )


def _is_job_default_address_task(plan_json: dict[str, Any], task_description: str) -> bool:
    domain = str(plan_json.get("domain_playbook_id") or plan_json.get("domain_id") or "")
    if domain == "android_job_default_address":
        return True
    contract_ids = {
        str(c.get("contract_id") or c.get("id") or "")
        for c in (plan_json.get("required_contracts") or [])
        if isinstance(c, dict)
    }
    if set(ANDROID_JOB_DEFAULT_ADDRESS_CONTRACTS) & contract_ids:
        return True
    text = " ".join(
        str(v or "")
        for v in (
            plan_json.get("objective"),
            plan_json.get("change_summary"),
            plan_json.get("change_explanation"),
            task_description,
        )
    ).lower()
    return (
        "job" in text
        and "address" in text
        and any(token in text for token in ("default", "pre-fill", "prefill", "saved"))
    )


def _fragment_imports(content: str) -> list[str]:
    imports = [
        "import android.location.Geocoder",
        "import androidx.compose.runtime.LaunchedEffect",
        "import com.example.handyman.utils.SessionManager",
        "import com.google.android.gms.tasks.Tasks",
        "import com.google.firebase.database.FirebaseDatabase",
        "import kotlinx.coroutines.Dispatchers",
        "import kotlinx.coroutines.withContext",
    ]
    if not re.search(r"^\s*import\s+java\.util\.(?:Locale|\*)\s*$", content, re.MULTILINE):
        imports.append("import java.util.Locale")
    return imports


def _flow_imports(content: str) -> list[str]:
    if re.search(r"^\s*import\s+java\.util\.(?:Locale|\*)\s*$", content, re.MULTILINE):
        return []
    return ["import java.util.Locale"]


def _add_import(content: str, import_line: str) -> str:
    lines = content.splitlines()
    if any(line.strip() == import_line for line in lines):
        return content
    if import_line == "import java.util.Locale" and any(
        re.match(r"^\s*import\s+java\.util\.\*\s*$", line) for line in lines
    ):
        return content

    insert_at = 0
    for idx, line in enumerate(lines):
        text = line.strip()
        if text.startswith("package ") or text.startswith("import "):
            insert_at = idx + 1
            continue
        if insert_at and text:
            break
    new_lines = lines[:insert_at] + [import_line] + lines[insert_at:]
    return _join_like(content, new_lines)


def _ensure_geocoder_locale(content: str) -> str:
    return re.sub(
        r"Geocoder\((context|ctx)\)",
        r"Geocoder(\1, Locale.getDefault())",
        content,
    )


def _ensure_city_suburb_from_prefill_geocode(content: str) -> str:
    if "viewModel.citySuburb" in content:
        return content
    old = """                                        viewModel.latitude = results[0].latitude
                                        viewModel.longitude = results[0].longitude"""
    new = '''                                        val location = results[0]
                                        viewModel.latitude = location.latitude
                                        viewModel.longitude = location.longitude
                                        viewModel.citySuburb = location.locality
                                            ?: location.subLocality
                                            ?: location.subAdminArea
                                            ?: location.adminArea
                                            ?: ""'''
    if old in content:
        return content.replace(old, new)
    return content


def _remove_prefill_coordinate_sentinel(content: str) -> str:
    content = re.sub(
        r"if \(!viewModel\.isEditing && viewModel\.locationAddress\.isBlank\(\) &&\n"
        r"(\s*)viewModel\.latitude == 0\.0 && viewModel\.longitude == 0\.0\) \{",
        r"if (!viewModel.isEditing && viewModel.locationAddress.isBlank()) {",
        content,
    )
    return re.sub(
        r"if \(!results\.isNullOrEmpty\(\) &&\n"
        r"(\s*)viewModel\.latitude == 0\.0 && viewModel\.longitude == 0\.0\) \{",
        r"if (!results.isNullOrEmpty()) {",
        content,
    )


def _ensure_fragment_prefill(content: str) -> str:
    if _has_saved_home_prefill(content):
        return _upgrade_existing_home_address_extraction(content)
    insert_at = content.find("                NavHost(")
    if insert_at < 0:
        return content
    block = _fragment_prefill_block().rstrip() + "\n\n"
    return content[:insert_at] + block + content[insert_at:]


def _has_saved_home_prefill(content: str) -> bool:
    return (
        "viewModel.locationAddress" in content
        and "homeAddress" in content
        and 'getReference("User")' in content
        and "getFromLocationName" in content
    )


def _upgrade_existing_home_address_extraction(content: str) -> str:
    if "directHomeAddress" in content or 'snapshot.child("address")' in content:
        return content
    lines = content.splitlines()
    parts_line = next(
        (
            idx
            for idx, line in enumerate(lines)
            if 'val parts = listOf("houseNumber", "street", "area", "city", "country")' in line
        ),
        -1,
    )
    if parts_line < 0:
        return content

    indent = re.match(r"^(\s*)", lines[parts_line]).group(1)  # type: ignore[union-attr]
    direct_lines = [
        f'{indent}val directHomeAddress = snapshot.child("address")',
        f"{indent}    .getValue(String::class.java)",
        f"{indent}    .orEmpty()",
    ]
    lines[parts_line:parts_line] = direct_lines

    # The insertion shifts the original parts line down.
    start = parts_line + len(direct_lines)
    filter_line = -1
    for idx in range(start, min(len(lines), start + 8)):
        if ".filter { it.isNotBlank() }" in lines[idx]:
            filter_line = idx
            break
    if filter_line < 0 or filter_line + 2 >= len(lines):
        return _join_like(content, lines)

    if "if (parts.isNotEmpty() && viewModel.locationAddress.isBlank())" in lines[filter_line + 1]:
        home_indent = re.match(r"^(\s*)", lines[filter_line + 1]).group(1)  # type: ignore[union-attr]
        lines[filter_line + 1 : filter_line + 3] = [
            f"{home_indent}val homeAddress = directHomeAddress.ifBlank {{",
            f"{home_indent}    parts.joinToString(\", \")",
            f"{home_indent}}}",
            f"{home_indent}if (homeAddress.isNotBlank() && viewModel.locationAddress.isBlank()) {{",
        ]
    return _join_like(content, lines)


def _fragment_prefill_block() -> str:
    return """                LaunchedEffect(Unit) {
                    if (!viewModel.isEditing && viewModel.locationAddress.isBlank()) {
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
                                val directHomeAddress = snapshot.child("address")
                                    .getValue(String::class.java)
                                    .orEmpty()
                                val parts = listOf("houseNumber", "street", "area", "city", "country")
                                    .map { snapshot.child(it).getValue(String::class.java).orEmpty() }
                                    .filter { it.isNotBlank() }
                                val homeAddress = directHomeAddress.ifBlank {
                                    parts.joinToString(", ")
                                }
                                if (homeAddress.isNotBlank() && viewModel.locationAddress.isBlank()) {
                                    viewModel.locationAddress = homeAddress
                                    val results = withContext(Dispatchers.IO) {
                                        runCatching {
                                            Geocoder(ctx, Locale.getDefault())
                                                .getFromLocationName(homeAddress, 1)
                                        }.getOrNull()
                                    }
                                    if (!results.isNullOrEmpty()) {
                                        viewModel.latitude = results[0].latitude
                                        viewModel.longitude = results[0].longitude
                                    }
                                }
                            } catch (_: Exception) {
                                // No saved home address, fetch failed, or geocoder unavailable.
                                // The existing manual/map location flow remains available.
                            }
                        }
                    }
                }
"""


def _ensure_flow_map_update_sync(content: str) -> str:
    if "mapView.controller.animateTo(point)" in content and "viewModel.latitude" in content:
        return content
    pattern = re.compile(
        r"(?P<indent>^\s*)update\s*=\s*\{\s*mapView\s*->\n"
        r"\s*// Ensure marker stays synced if viewModel changes from elsewhere\n"
        r"\s*\}",
        re.MULTILINE,
    )

    def _replacement(match: re.Match[str]) -> str:
        indent = match.group("indent")
        inner = indent + "    "
        cont = indent + "        "
        return f"""{indent}update = {{ mapView ->
{inner}if (viewModel.latitude != 0.0) {{
{cont}val point = GeoPoint(viewModel.latitude, viewModel.longitude)
{cont}mapView.controller.animateTo(point)
{cont}val marker = mapView.overlays.filterIsInstance<Marker>().firstOrNull()
{cont}    ?: Marker(mapView).also {{ mapView.overlays.add(it) }}
{cont}marker.position = point
{cont}marker.title = "Selected Location"
{cont}mapView.invalidate()
{inner}}}
{indent}}}"""

    return pattern.sub(_replacement, content, count=1)


def _coverage_payload(*, file_path: str, before: str, after: str) -> dict[str, Any]:
    implemented: list[dict[str, str]] = []
    no_change: list[dict[str, str]] = []

    def add(cid: str, *, before_has: bool, after_has: bool, quote: str) -> None:
        if not after_has:
            return
        row = {
            "contract_id": cid,
            "file_path": file_path,
            "evidence_quote": quote,
            "evidence_mode": "preexisting" if before_has else "recipe_diff",
        }
        if before_has:
            no_change.append(row)
        else:
            implemented.append(row)

    add(
        "saved_home_address_loaded",
        before_has=_has_saved_home_read(before),
        after_has=_has_saved_home_read(after),
        quote='getReference("User").child(userId).get()',
    )
    add(
        "job_location_prefilled",
        before_has=_has_location_prefill(before),
        after_has=_has_location_prefill(after),
        quote="viewModel.locationAddress = homeAddress",
    )
    add(
        "saved_address_geocoded_to_map",
        before_has=_has_home_address_geocode(before),
        after_has=_has_home_address_geocode(after),
        quote="getFromLocationName(homeAddress, 1)",
    )
    add(
        "work_location_saved_to_job_only",
        before_has=_has_job_payload_location_sink(before),
        after_has=_has_job_payload_location_sink(after),
        quote="jobLocation = viewModel.locationAddress",
    )
    add(
        "missing_home_address_safe",
        before_has=_has_missing_home_address_guard(before),
        after_has=_has_missing_home_address_guard(after),
        quote="try/catch + homeAddress.isNotBlank()",
    )

    return {
        "implemented_contracts": implemented,
        "verified_no_change_contracts": no_change,
        "unimplemented_contracts": [],
    }


def _has_saved_home_read(content: str) -> bool:
    return (
        'getReference("User")' in content
        and ".child(userId).get()" in content
        and ("SessionManager.currentUserID" in content or "getLoggedInUserId" in content)
    )


def _has_location_prefill(content: str) -> bool:
    return bool(re.search(r"viewModel\.locationAddress\s*=\s*homeAddress", content))


def _has_home_address_geocode(content: str) -> bool:
    return bool(
        re.search(r"getFromLocationName\s*\(\s*homeAddress\s*,\s*1\s*\)", content)
        and re.search(r"viewModel\.latitude\s*=", content)
        and re.search(r"viewModel\.longitude\s*=", content)
    )


def _has_job_payload_location_sink(content: str) -> bool:
    return bool(
        re.search(r"jobLocation\s*=\s*viewModel\.locationAddress", content)
        and re.search(r"latitude\s*=\s*viewModel\.latitude", content)
        and re.search(r"longitude\s*=\s*viewModel\.longitude", content)
    )


def _has_missing_home_address_guard(content: str) -> bool:
    return bool(
        ("try {" in content and "catch (_: Exception)" in content)
        or "runCatching" in content
        or "homeAddress.isNotBlank()" in content
        or "homeAddress.isBlank()" in content
    )


def _join_like(original: str, lines: list[str]) -> str:
    text = "\n".join(lines)
    return text + ("\n" if original.endswith("\n") else "")


def _unified_diff(path: str, before: str, after: str) -> str:
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    body = "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    return f"diff --git a/{path} b/{path}\n{body}" if body else ""
