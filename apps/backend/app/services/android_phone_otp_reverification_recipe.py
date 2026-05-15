"""Deterministic recipe for Android phone OTP re-verification flows.

This covers the repeated P69-21 shape: request-code screens must not query a
Firebase profile row or write ``phoneNumber`` before the OTP credential has
been verified. The safe edit is structural and tiny: normalize ``onCodeSent``
so it only clears loading state and navigates to the OTP entry screen.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.structural_edit import validate_kotlin_structure


ANDROID_PHONE_OTP_REVERIFICATION_CONTRACTS = [
    "customer_no_preverification_phone_write",
    "handyman_no_preverification_phone_write",
]


@dataclass
class AndroidPhoneOtpReverificationRecipeResult:
    content: str
    diff: str
    files_changed: list[str]
    summary: str
    applied_operations: list[str] = field(default_factory=list)
    contract_coverage: dict[str, Any] | None = None


def try_generate_android_phone_otp_reverification_recipe(
    *,
    file_path: str,
    original_content: str,
    plan_json: dict[str, Any] | None = None,
    task_description: str = "",
) -> AndroidPhoneOtpReverificationRecipeResult | None:
    """Generate a deterministic patch for phone OTP request-code screens."""
    if Path(file_path).suffix.lower() not in {".kt", ".kts"}:
        return None
    if not original_content.strip():
        return None
    if not _is_phone_otp_task(plan_json or {}, task_description):
        return None

    route = _otp_route_for_file(file_path)
    contract_id = _contract_for_file(file_path)
    if not route or not contract_id:
        return None
    if "onCodeSent" not in original_content or "navController.navigate" not in original_content:
        return None

    content, changed = _replace_on_code_sent_body(
        original_content,
        route=route,
    )
    if not changed or content == original_content:
        return None
    if _has_preverification_phone_write(content):
        return None

    structure_errors = validate_kotlin_structure(content)
    if structure_errors:
        return None

    diff = _unified_diff(file_path, original_content, content)
    if not diff.strip():
        return None

    return AndroidPhoneOtpReverificationRecipeResult(
        content=content,
        diff=diff,
        files_changed=[file_path],
        summary="Generated Android phone OTP re-verification patch via harness recipe",
        applied_operations=["replace_on_code_sent_body"],
        contract_coverage=_coverage_payload(
            file_path=file_path,
            contract_id=contract_id,
            route=route,
        ),
    )


def _is_phone_otp_task(plan_json: dict[str, Any], task_description: str) -> bool:
    domain = str(plan_json.get("domain_playbook_id") or plan_json.get("domain_id") or "")
    if domain == "android_phone_otp_reverification":
        return True
    contract_ids = {
        str(c.get("contract_id") or c.get("id") or "")
        for c in (plan_json.get("required_contracts") or [])
        if isinstance(c, dict)
    }
    if set(ANDROID_PHONE_OTP_REVERIFICATION_CONTRACTS) & contract_ids:
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
    return "otp" in text and "phone" in text and (
        "verification" in text or "verify" in text or "re-request" in text
    )


def _otp_route_for_file(file_path: str) -> str:
    normalized = file_path.replace("\\", "/").lower()
    name = Path(normalized).name
    if name != "customerkycphonenumber.kt" and name != "handymankycphonenumber.kt":
        return ""
    if "/customer_pages/" in normalized or name.startswith("customer"):
        return "customerKycCodeOTP"
    if "/handyman_pages/" in normalized or name.startswith("handyman"):
        return "handymanKycCodeOTP"
    return ""


def _contract_for_file(file_path: str) -> str:
    route = _otp_route_for_file(file_path)
    if route == "customerKycCodeOTP":
        return "customer_no_preverification_phone_write"
    if route == "handymanKycCodeOTP":
        return "handyman_no_preverification_phone_write"
    return ""


def _replace_on_code_sent_body(content: str, *, route: str) -> tuple[str, bool]:
    lines = content.splitlines()
    start = next(
        (idx for idx, line in enumerate(lines) if "override fun onCodeSent" in line),
        -1,
    )
    if start < 0:
        return content, False

    open_idx = -1
    for idx in range(start, min(len(lines), start + 12)):
        if "{" in lines[idx]:
            open_idx = idx
            break
    if open_idx < 0:
        return content, False

    depth = _brace_delta(lines[open_idx])
    close_idx = -1
    for idx in range(open_idx + 1, len(lines)):
        depth += _brace_delta(lines[idx])
        if depth <= 0:
            close_idx = idx
            break
    if close_idx <= open_idx:
        return content, False

    indent = _body_indent(lines, open_idx, close_idx)
    new_body = [
        f"{indent}isLoading = false",
        f'{indent}navController.navigate("{route}/$verificationId/$phoneNumber")',
    ]
    new_lines = lines[: open_idx + 1] + new_body + lines[close_idx:]
    new_content = _join_like(content, new_lines)
    return new_content, new_content != content


def _body_indent(lines: list[str], open_idx: int, close_idx: int) -> str:
    for idx in range(open_idx + 1, close_idx):
        if lines[idx].strip():
            return re.match(r"\s*", lines[idx]).group(0)  # type: ignore[union-attr]
    parent_indent = re.match(r"\s*", lines[open_idx]).group(0)  # type: ignore[union-attr]
    return parent_indent + "    "


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _has_preverification_phone_write(content: str) -> bool:
    return bool(
        re.search(r'child\("phoneNumber"\)\.setValue\s*\(', content)
        or re.search(r'orderByChild\("email"\)\.equalTo\(currentEmail\)', content)
    )


def _coverage_payload(*, file_path: str, contract_id: str, route: str) -> dict[str, Any]:
    return {
        "implemented_contracts": [
            {
                "contract_id": contract_id,
                "file_path": file_path,
                "evidence_quote": f'navController.navigate("{route}/$verificationId/$phoneNumber")',
                "evidence_mode": "recipe_diff",
            }
        ],
        "verified_no_change_contracts": [],
        "unimplemented_contracts": [],
    }


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
