"""T-026-D: lock in that backend PERMISSION_MAP matches frontend rolePermissions.

The comment in apps/backend/app/core/security.py says:
  # Keep in sync with apps/web/src/lib/auth.tsx::rolePermissions.
A comment is not a guarantee. This test parses the TypeScript source and
compares the two maps literally. If they ever drift, CI fails.

Parsing strategy: find the `rolePermissions` object literal, then extract
the permission string arrays per role. Deliberately simple and regex-based
so the test does not need a TS parser dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.core.security import PERMISSION_MAP

FRONTEND_AUTH_PATH = (
    Path(__file__).resolve().parents[4]
    / "apps"
    / "web"
    / "src"
    / "lib"
    / "auth.tsx"
)


_ROLE_BLOCK_PATTERN = re.compile(
    r"(admin|operator|member|viewer)\s*:\s*\[([^\]]*)\]",
    re.DOTALL,
)
_PERMISSION_PATTERN = re.compile(r'"([^"]+)"')


def _parse_frontend_role_permissions() -> dict[str, set[str]]:
    text = FRONTEND_AUTH_PATH.read_text(encoding="utf-8")
    start = text.find("const rolePermissions")
    assert start != -1, "rolePermissions not found in auth.tsx"
    # Take a window large enough to cover the map definition.
    window = text[start : start + 2000]

    result: dict[str, set[str]] = {}
    for match in _ROLE_BLOCK_PATTERN.finditer(window):
        role = match.group(1)
        body = match.group(2)
        perms = set(_PERMISSION_PATTERN.findall(body))
        result[role] = perms
    return result


def test_frontend_and_backend_permission_maps_are_identical() -> None:
    frontend = _parse_frontend_role_permissions()
    backend = {role: set(perms) for role, perms in PERMISSION_MAP.items()}

    assert set(frontend.keys()) == set(backend.keys()), (
        f"Role sets differ. frontend={sorted(frontend.keys())} "
        f"backend={sorted(backend.keys())}"
    )

    for role in sorted(backend.keys()):
        assert frontend[role] == backend[role], (
            f"Permissions for role '{role}' differ.\n"
            f"  frontend only: {sorted(frontend[role] - backend[role])}\n"
            f"  backend only:  {sorted(backend[role] - frontend[role])}"
        )
