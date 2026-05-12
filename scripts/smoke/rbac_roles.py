"""
T-026-05 end-to-end RBAC smoke.

Runs a fixed matrix of (role, endpoint) checks against a locally running
backend and asserts the expected HTTP status. Exits 0 on full PASS, non-zero
otherwise. Prints a per-case line and a final summary.

Usage:
    python scripts/smoke/rbac_roles.py --base-url http://127.0.0.1:8000

The script assumes the backend has been started separately (via
`scripts/start-backend.ps1`) and that the model catalog / governance seeds
have run at least once (they run on lifespan startup).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass


REQUIRED_MODEL_ID = "claude-opus-4-6"

ROLE_HEADERS = {
    "admin": {"X-Actor-Role": "admin", "X-Actor-App-Role": "admin"},
    "operator": {"X-Actor-Role": "team_lead", "X-Actor-App-Role": "operator"},
    "member": {"X-Actor-Role": "employee", "X-Actor-App-Role": "member"},
    "viewer": {"X-Actor-Role": "employee", "X-Actor-App-Role": "viewer"},
    "anonymous": {},
}

ROLES = ("admin", "operator", "member", "viewer", "anonymous")


@dataclass(frozen=True)
class Case:
    name: str
    method: str
    path: str
    role: str
    expected_status: int
    body: dict[str, object] | None = None


def request_json(
    base_url: str,
    method: str,
    path: str,
    role: str,
    body: dict[str, object] | None = None,
) -> tuple[int | str, str]:
    headers = {"Accept": "application/json"}
    headers.update(ROLE_HEADERS[role])
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        return f"ERROR: {error.reason}", ""
    except OSError as error:
        return f"ERROR: {error}", ""


def parse_json_response(text: str) -> object | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def ensure_required_model(base_url: str) -> bool:
    status, text = request_json(
        base_url=base_url,
        method="GET",
        path="/api/model-config/providers",
        role="anonymous",
    )
    if status != 200:
        print(
            "[FAIL] preflight GET /api/model-config/providers as anonymous "
            f"-> {status} (expected 200)"
        )
        return False

    providers = parse_json_response(text)
    if not isinstance(providers, list):
        print("[FAIL] preflight model catalog response was not a JSON list.")
        return False

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if isinstance(model, dict) and model.get("id") == REQUIRED_MODEL_ID:
                return True

    print(
        "[FAIL] required seeded model_id 'claude-opus-4-6' was not found. "
        "Start the backend so lifespan startup seeds the model catalog, "
        "or run the catalog seed path before this smoke."
    )
    return False


def read_memory_settings(base_url: str) -> dict[str, object] | None:
    status, text = request_json(
        base_url=base_url,
        method="GET",
        path="/api/memory/settings",
        role="anonymous",
    )
    if status != 200:
        print(
            "[FAIL] preflight GET /api/memory/settings as anonymous "
            f"-> {status} (expected 200)"
        )
        return None

    payload = parse_json_response(text)
    if not isinstance(payload, dict):
        print("[FAIL] preflight memory settings response was not a JSON object.")
        return None

    return {
        "enabled": bool(payload.get("enabled", False)),
        "allow_list": str(payload.get("allow_list", "")),
        "block_list": str(payload.get("block_list", "")),
    }


def read_selected_model(base_url: str) -> dict[str, object] | None:
    status, text = request_json(
        base_url=base_url,
        method="GET",
        path="/api/model-config/selected",
        role="anonymous",
    )
    if status != 200:
        print(
            "[FAIL] preflight GET /api/model-config/selected as anonymous "
            f"-> {status} (expected 200)"
        )
        return None

    payload = parse_json_response(text)
    if not isinstance(payload, dict):
        print("[FAIL] preflight selected model response was not a JSON object.")
        return None

    model_id = payload.get("model_id")
    if model_id is not None and not isinstance(model_id, str):
        print("[FAIL] preflight selected model_id was not null or a string.")
        return None
    return {"model_id": model_id}


def build_cases(suffix: str) -> list[Case]:
    fake_document_id = f"rbac-smoke-document-{suffix}"
    fake_approval_id = f"rbac-smoke-approval-{suffix}"
    memory_item_expected = {
        "admin": 201,
        "operator": 201,
        "member": 201,
        "viewer": 403,
        "anonymous": 401,
    }
    memory_settings_expected = {
        "admin": 200,
        "operator": 200,
        "member": 200,
        "viewer": 403,
        "anonymous": 401,
    }
    knowledge_delete_expected = {
        "admin": 404,
        "operator": 403,
        "member": 403,
        "viewer": 403,
        "anonymous": 401,
    }
    knowledge_sync_expected = {
        "admin": 200,
        "operator": 200,
        "member": 403,
        "viewer": 403,
        "anonymous": 401,
    }
    model_config_expected = {
        "admin": 200,
        "operator": 200,
        "member": 403,
        "viewer": 403,
        "anonymous": 401,
    }
    approval_grant_expected = {
        "admin": 404,
        "operator": 404,
        "member": 403,
        "viewer": 403,
        "anonymous": 401,
    }
    read_expected = {
        "admin": 200,
        "operator": 200,
        "member": 200,
        "viewer": 200,
        "anonymous": 200,
    }

    row_specs = [
        (
            "memory item create",
            "POST",
            "/api/memory/items",
            memory_item_expected,
            lambda role: {
                "title": f"RBAC smoke {suffix} {role}",
                "body": f"Created by T-026-05 RBAC smoke for role {role}.",
                "topic": f"rbac-smoke-{suffix}",
            },
        ),
        (
            "memory settings update",
            "PATCH",
            "/api/memory/settings",
            memory_settings_expected,
            lambda role: {
                "enabled": True,
                "allow_list": f"rbac-smoke-{suffix}-{role}",
                "block_list": "",
            },
        ),
        (
            "knowledge document delete",
            "DELETE",
            f"/api/knowledge/documents/{fake_document_id}",
            knowledge_delete_expected,
            None,
        ),
        (
            "knowledge sync",
            "POST",
            "/api/knowledge/sync",
            knowledge_sync_expected,
            None,
        ),
        (
            "selected model update",
            "PATCH",
            "/api/model-config/selected",
            model_config_expected,
            lambda role: {"model_id": REQUIRED_MODEL_ID},
        ),
        (
            "approval grant",
            "POST",
            f"/api/approvals/{fake_approval_id}/grant",
            approval_grant_expected,
            lambda role: {
                "actor_name": f"rbac-smoke-{role}",
                "actor_role": "team_lead",
                "notes": f"T-026-05 RBAC smoke {suffix}",
            },
        ),
        ("memory item read", "GET", "/api/memory/items", read_expected, None),
        ("model providers read", "GET", "/api/model-config/providers", read_expected, None),
    ]

    cases = []
    for name, method, path, expected_by_role, body_factory in row_specs:
        for role in ROLES:
            body = body_factory(role) if body_factory is not None else None
            cases.append(
                Case(
                    name=name,
                    method=method,
                    path=path,
                    role=role,
                    expected_status=expected_by_role[role],
                    body=body,
                )
            )
    return cases


def cleanup_memory_items(base_url: str, item_ids: list[str]) -> bool:
    ok = True
    for item_id in item_ids:
        path = f"/api/memory/items/{item_id}"
        status, _ = request_json(
            base_url=base_url,
            method="DELETE",
            path=path,
            role="admin",
        )
        if status == 200:
            print(f"[PASS] teardown DELETE {path} as admin -> {status} (expected 200)")
        else:
            print(f"[FAIL] teardown DELETE {path} as admin -> {status} (expected 200)")
            ok = False
    return ok


def restore_memory_settings(base_url: str, original: dict[str, object]) -> bool:
    status, _ = request_json(
        base_url=base_url,
        method="PATCH",
        path="/api/memory/settings",
        role="admin",
        body=original,
    )
    if status == 200:
        print("[PASS] teardown PATCH /api/memory/settings as admin -> 200 (expected 200)")
        return True
    print(f"[FAIL] teardown PATCH /api/memory/settings as admin -> {status} (expected 200)")
    return False


def restore_selected_model(base_url: str, original: dict[str, object]) -> bool:
    status, _ = request_json(
        base_url=base_url,
        method="PATCH",
        path="/api/model-config/selected",
        role="admin",
        body=original,
    )
    if status == 200:
        print("[PASS] teardown PATCH /api/model-config/selected as admin -> 200 (expected 200)")
        return True
    print(f"[FAIL] teardown PATCH /api/model-config/selected as admin -> {status} (expected 200)")
    return False


def run_matrix(base_url: str) -> int:
    suffix = uuid.uuid4().hex[:8]
    print("T-026-05 RBAC smoke")
    print(f"Base URL: {base_url.rstrip('/')}")
    print(f"Run suffix: {suffix}")
    print(
        "Matrix note: PERMISSION_MAP and frontend rolePermissions both grant "
        "member memory:edit, so PATCH /api/memory/settings expects member -> 200."
    )
    print(
        "Matrix note: operator lacks knowledge:delete, so DELETE "
        "/api/knowledge/documents/<fake-id> expects operator -> 403."
    )

    if not ensure_required_model(base_url):
        print("PASSED: 0/40")
        return 1

    original_memory_settings = read_memory_settings(base_url)
    if original_memory_settings is None:
        print("PASSED: 0/40")
        return 1

    original_selected_model = read_selected_model(base_url)
    if original_selected_model is None:
        print("PASSED: 0/40")
        return 1

    cases = build_cases(suffix)
    passed = 0
    capture_ok = True
    created_memory_item_ids: list[str] = []

    for index, case in enumerate(cases, start=1):
        status, text = request_json(
            base_url=base_url,
            method=case.method,
            path=case.path,
            role=case.role,
            body=case.body,
        )
        is_pass = status == case.expected_status
        if is_pass:
            passed += 1
        label = "PASS" if is_pass else "FAIL"
        print(
            f"[{label}] #{index} {case.method} {case.path} as {case.role} "
            f"-> {status} (expected {case.expected_status})"
        )

        if case.name == "memory item create" and status == 201:
            payload = parse_json_response(text)
            if isinstance(payload, dict) and isinstance(payload.get("id"), str):
                created_memory_item_ids.append(payload["id"])
            else:
                print(
                    f"[FAIL] teardown capture memory item id for #{index} "
                    "-> missing JSON id"
                )
                capture_ok = False

    teardown_ok = capture_ok
    teardown_ok = cleanup_memory_items(base_url, created_memory_item_ids) and teardown_ok
    teardown_ok = restore_memory_settings(base_url, original_memory_settings) and teardown_ok
    teardown_ok = restore_selected_model(base_url, original_selected_model) and teardown_ok

    total = len(cases)
    print(f"PASSED: {passed}/{total}")
    return 0 if passed == total and teardown_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the T-026-05 RBAC smoke matrix.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    return run_matrix(args.base_url)


if __name__ == "__main__":
    sys.exit(main())
