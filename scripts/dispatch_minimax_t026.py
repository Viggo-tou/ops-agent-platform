"""
Dispatch MiniMax for T-026 M1-M4 subtasks.

Usage:
    python scripts/dispatch_minimax_t026.py m1
    python scripts/dispatch_minimax_t026.py m3
    python scripts/dispatch_minimax_t026.py m2
    python scripts/dispatch_minimax_t026.py m4

M1 (JSON fixture) and M3 (ADR markdown) are pure file-generation.
M2 (schema docstrings) and M4 (HTTPException detail text) rewrite existing files.
All calls return full content; the script writes to the target path.

Each call is validated before the target file is touched:
- M1: json.loads must succeed.
- M3: must contain "## Decision" and all 9 numbered controls.
- M2: must be valid Python (ast.parse) and keep the same class set.
- M4: must be valid Python (ast.parse) and same set of raise HTTPException sites.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "apps" / "backend" / ".env")

API_KEY = os.getenv("OPS_AGENT_MINIMAX_API_KEY")
if not API_KEY:
    print("ERROR: OPS_AGENT_MINIMAX_API_KEY not set")
    sys.exit(1)

URL = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
MODEL = "MiniMax-M2.7-highspeed"


def call(system: str, user: str, max_tokens: int = 8192, temperature: float = 0.1) -> str:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = httpx.post(
        URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=180,
    )
    data = resp.json()
    if "choices" not in data:
        print("API error:", json.dumps(data, indent=2, ensure_ascii=False))
        sys.exit(2)
    content = data["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:python|json|markdown|md)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content).strip()
    return content


def read_spec(name: str) -> str:
    return (ROOT / "docs" / "ai" / "tasks" / name).read_text(encoding="utf-8")


# ---------------- M1 ----------------


def do_m1() -> None:
    spec = read_spec("T-026-M1-rbac-expected-matrix.md")
    system = "You output ONLY valid JSON. No markdown fences, no commentary."
    user = (
        "Produce the exact JSON fixture described in this spec. Output ONLY the JSON content, "
        "no explanation, no markdown. Follow every rule literally.\n\n"
        f"SPEC:\n{spec}"
    )
    content = call(system, user, max_tokens=6144)
    data = json.loads(content)  # validates
    assert data["roles"] == ["admin", "operator", "member", "viewer"]
    assert len(data["endpoints"]) >= 20
    target = ROOT / "apps" / "backend" / "tests" / "fixtures" / "rbac_expected_matrix.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"OK M1: wrote {target} ({len(data['endpoints'])} endpoints)")


# ---------------- M3 ----------------


def do_m3() -> None:
    spec = read_spec("T-026-M3-adr-zip-import-security.md")
    system = "You output ONLY the markdown document, no preamble."
    user = (
        "Produce the ADR file exactly per this spec. Fill Context and Consequences with 3-5 sentences. "
        "Keep every MUST phrasing. Output ONLY the markdown body.\n\n"
        f"SPEC:\n{spec}"
    )
    content = call(system, user, max_tokens=4096, temperature=0.2)
    must_contain = [
        "## Decision",
        "## Context",
        "## Consequences",
        "Path traversal",
        "Size bounds",
        "Compression-ratio",
        "Symlink rejection",
        "Filename normalization",
        "MIME",
        "Permission gating",
        "Atomicity",
        "Error reporting",
    ]
    missing = [m for m in must_contain if m not in content]
    if missing:
        print("ERROR M3 missing sections:", missing)
        sys.exit(3)
    target = ROOT / "docs" / "adr" / "0001-zip-import-security.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content + "\n", encoding="utf-8")
    print(f"OK M3: wrote {target}")


# ---------------- M2 ----------------

M2_FILES = [
    "apps/backend/app/schemas/memory.py",
    "apps/backend/app/schemas/model_config.py",
    "apps/backend/app/schemas/knowledge.py",
]


def _classes_in(src: str) -> set[str]:
    tree = ast.parse(src)
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}


def do_m2() -> None:
    spec = read_spec("T-026-M2-schema-docstrings.md")
    system = "You output ONLY valid Python source, no markdown fences, no commentary."
    for rel in M2_FILES:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        original_classes = _classes_in(original)
        user = (
            "Rewrite the following Python file per the spec below. Add Field(..., description=...) "
            "to every BaseModel field following the strict rules. Do not rename, reorder, or retype. "
            "Output ONLY the full new file contents.\n\n"
            f"SPEC:\n{spec}\n\n"
            f"FILE PATH: {rel}\n"
            f"CURRENT CONTENTS:\n{original}"
        )
        content = call(system, user, max_tokens=8192)
        try:
            new_classes = _classes_in(content)
        except SyntaxError as exc:
            print(f"ERROR M2 {rel}: syntax error {exc}")
            sys.exit(4)
        if new_classes != original_classes:
            print(f"ERROR M2 {rel}: class set changed. before={original_classes} after={new_classes}")
            sys.exit(4)
        if "description=" not in content:
            print(f"ERROR M2 {rel}: no description= added")
            sys.exit(4)
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        print(f"OK M2: rewrote {rel}")


# ---------------- M4 ----------------


def _httpexception_lines(src: str) -> int:
    return len(re.findall(r"raise\s+HTTPException\s*\(", src))


def do_m4() -> None:
    spec = read_spec("T-026-M4-httpexception-text-normalization.md")
    system = "You output ONLY valid Python source, no markdown fences, no commentary."
    api_dir = ROOT / "apps" / "backend" / "app" / "api"
    files = sorted(p for p in api_dir.glob("*.py") if p.name != "__init__.py")
    for path in files:
        original = path.read_text(encoding="utf-8")
        if "HTTPException" not in original:
            continue
        n_before = _httpexception_lines(original)
        if n_before == 0:
            continue
        rel = path.relative_to(ROOT).as_posix()
        user = (
            "Rewrite the following Python file, normalizing ONLY the `detail=` strings inside "
            "`raise HTTPException(...)` calls per the spec below. Do not change status_code, "
            "control flow, or anything outside those strings. Output ONLY the full new file.\n\n"
            f"SPEC:\n{spec}\n\n"
            f"FILE PATH: {rel}\n"
            f"CURRENT CONTENTS:\n{original}"
        )
        content = call(system, user, max_tokens=8192)
        try:
            ast.parse(content)
        except SyntaxError as exc:
            print(f"ERROR M4 {rel}: syntax error {exc}")
            sys.exit(5)
        n_after = _httpexception_lines(content)
        if n_after != n_before:
            print(f"ERROR M4 {rel}: HTTPException count changed {n_before} -> {n_after}")
            sys.exit(5)
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        print(f"OK M4: rewrote {rel} ({n_before} HTTPException sites)")


TASKS = {"m1": do_m1, "m2": do_m2, "m3": do_m3, "m4": do_m4}


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python scripts/dispatch_minimax_t026.py <m1|m2|m3|m4|all>")
        sys.exit(1)
    arg = sys.argv[1].lower()
    if arg == "all":
        for k in ["m1", "m3", "m2", "m4"]:
            TASKS[k]()
    elif arg in TASKS:
        TASKS[arg]()
    else:
        print(f"Unknown task: {arg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
