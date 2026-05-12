"""Repository library/framework fingerprinting.

Scans the target repository and produces a compact ``REPO LIBRARY HINTS``
string that gets prepended to planner/codegen prompts. This kills the
biggest source of provider variance: when the LLM has to *guess* which
mapping library / database / UI framework the repo uses, it picks
randomly. Fingerprinting pins the choice up-front:

  REPO LIBRARY HINTS (use these, do NOT introduce alternates):
  - Maps: org.osmdroid (NOT Google Maps, NOT Mapbox)
  - Database: Firebase Realtime Database via FirebaseDatabase
  - UI: Jetpack Compose Material3
  - Auth: Firebase Auth via FirebaseAuth

Detection is cheap: check for presence in ``build.gradle*`` (manifest of
truth) plus a frequency count of top imports across .kt/.kts files
(manifest of actual usage). When manifest+usage agree, emit a hint.

Provider-agnostic. Output is a string, fed to whatever LLM is the
configured planner/codegen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Skip these directories anywhere in the path.
_SKIP_DIRS = {
    ".git", "node_modules", "build", ".gradle", ".idea", "dist",
    "__pycache__", ".venv", "venv", "target", "out",
}

# Map fingerprint -> (display-name, alternates-to-warn-against).
# When the fingerprint is detected, planner is told "use X, NOT Y/Z".
_LIBRARY_RULES: list[dict[str, object]] = [
    {
        "category": "Maps",
        "detect_dep": [r"\borg\.osmdroid\b", r"\bosmdroid-android\b"],
        "detect_import": [r"\borg\.osmdroid\."],
        "use": "org.osmdroid (MapView, GeoPoint, Marker, MapEventsOverlay)",
        "avoid": ["Google Maps (com.google.android.gms.maps)", "Mapbox", "HERE Maps"],
    },
    {
        "category": "Maps",
        "detect_dep": [r"\bcom\.google\.android\.gms[:.]play-services-maps\b"],
        "detect_import": [r"\bcom\.google\.android\.gms\.maps\."],
        "use": "Google Maps (com.google.android.gms.maps.GoogleMap)",
        "avoid": ["org.osmdroid", "Mapbox"],
    },
    {
        "category": "Database",
        "detect_dep": [r"\bfirebase-database\b", r"\bcom\.google\.firebase[:.]firebase-database\b"],
        "detect_import": [r"\bcom\.google\.firebase\.database\."],
        "use": "Firebase Realtime Database (FirebaseDatabase.getInstance().getReference())",
        "avoid": ["Firestore", "Room", "Realm"],
    },
    {
        "category": "Database",
        "detect_dep": [r"\bfirebase-firestore\b"],
        "detect_import": [r"\bcom\.google\.firebase\.firestore\."],
        "use": "Firebase Firestore (FirebaseFirestore.getInstance())",
        "avoid": ["Firebase Realtime Database", "Room"],
    },
    {
        "category": "Auth",
        "detect_dep": [r"\bfirebase-auth\b"],
        "detect_import": [r"\bcom\.google\.firebase\.auth\."],
        "use": "Firebase Auth (FirebaseAuth.getInstance())",
        "avoid": ["Auth0", "OAuth2 directly"],
    },
    {
        "category": "UI",
        "detect_dep": [r"\bcompose\.material3\b"],
        "detect_import": [r"\bandroidx\.compose\.material3\."],
        "use": "Jetpack Compose Material3",
        "avoid": ["Material2 (androidx.compose.material)", "View XML layouts"],
    },
    {
        "category": "Geocoder",
        "detect_dep": [],
        "detect_import": [r"\bandroid\.location\.Geocoder\b"],
        "use": "android.location.Geocoder (forward + reverse geocoding)",
        "avoid": ["Google Places API direct", "Mapbox Geocoding"],
    },
    {
        "category": "Async",
        "detect_dep": [r"\bkotlinx-coroutines\b"],
        "detect_import": [r"\bkotlinx\.coroutines\."],
        "use": "Kotlin Coroutines (CoroutineScope.launch / withContext)",
        "avoid": ["RxJava", "raw threads"],
    },
    # ---- Web / Node frontends ----
    {
        "category": "Web Framework",
        "detect_dep": [r'"react"\s*:'],
        "detect_import": [r'\bfrom\s+["\']react["\']'],
        "use": "React + JSX/TSX",
        "avoid": ["Vue", "Svelte"],
    },
    {
        "category": "Web Build",
        "detect_dep": [r'"vite"\s*:'],
        "detect_import": [],
        "use": "Vite (vite.config.ts)",
        "avoid": ["webpack from scratch", "create-react-app"],
    },
]

# Hard cap on files scanned during import-frequency phase.
_MAX_IMPORT_SCAN_FILES = 800
_SOURCE_EXTENSIONS_FOR_IMPORTS = {".kt", ".kts", ".java", ".ts", ".tsx", ".js", ".jsx"}


@dataclass(frozen=True)
class LibraryHint:
    category: str
    use: str
    avoid: list[str]
    confidence: str  # "manifest+usage" | "manifest" | "usage"


def _read_text_safe(path: Path, *, max_bytes: int = 200_000) -> str:
    try:
        return path.read_bytes()[:max_bytes].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _collect_dep_text(repo_root: Path) -> str:
    """Concatenate all top-level dep-manifest files into one string."""
    candidates = [
        "build.gradle", "build.gradle.kts",
        "app/build.gradle", "app/build.gradle.kts",
        "settings.gradle", "settings.gradle.kts",
        "gradle/libs.versions.toml",
        "package.json",
        "Cargo.toml",
        "pyproject.toml", "requirements.txt",
        "go.mod",
    ]
    chunks: list[str] = []
    for name in candidates:
        p = repo_root / name
        if p.is_file():
            chunks.append(_read_text_safe(p))
    return "\n".join(chunks)


def _walk_source_files(repo_root: Path) -> list[Path]:
    out: list[Path] = []
    for path in repo_root.rglob("*"):
        if len(out) >= _MAX_IMPORT_SCAN_FILES:
            break
        if not path.is_file():
            continue
        if path.suffix.lower() not in _SOURCE_EXTENSIONS_FOR_IMPORTS:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        out.append(path)
    return out


def _import_text_blob(repo_root: Path) -> str:
    """Concatenate the first 50 lines of each source file (where imports live)."""
    blob_chunks: list[str] = []
    for path in _walk_source_files(repo_root):
        text = _read_text_safe(path, max_bytes=4096)
        # Just the first 50 lines is enough — imports cluster at the top.
        lines = text.splitlines()[:50]
        blob_chunks.append("\n".join(lines))
    return "\n".join(blob_chunks)


def fingerprint_repository(repo_root: Path) -> list[LibraryHint]:
    """Return the list of LibraryHints detected in ``repo_root``."""
    if not repo_root or not repo_root.exists() or not repo_root.is_dir():
        return []
    dep_text = _collect_dep_text(repo_root)
    imp_text = _import_text_blob(repo_root) if dep_text or True else ""

    hints: list[LibraryHint] = []
    seen_categories: set[tuple[str, str]] = set()
    for rule in _LIBRARY_RULES:
        category = str(rule["category"])
        use_label = str(rule["use"])
        manifest_match = any(
            re.search(p, dep_text) for p in (rule.get("detect_dep") or [])
        )
        usage_match = any(
            re.search(p, imp_text) for p in (rule.get("detect_import") or [])
        )
        if not manifest_match and not usage_match:
            continue
        # Same category + same use_label → already added.
        sig = (category, use_label)
        if sig in seen_categories:
            continue
        seen_categories.add(sig)
        if manifest_match and usage_match:
            confidence = "manifest+usage"
        elif manifest_match:
            confidence = "manifest"
        else:
            confidence = "usage"
        hints.append(
            LibraryHint(
                category=category,
                use=use_label,
                avoid=list(rule.get("avoid") or []),
                confidence=confidence,
            )
        )
    return hints


def render_library_hints_block(hints: list[LibraryHint]) -> str:
    """Render hints as a planner/codegen prompt block. Empty if no hints."""
    if not hints:
        return ""
    lines = [
        "REPO LIBRARY HINTS (use these libraries — DO NOT introduce alternates):",
    ]
    # Group by category so multi-category hints read clearly.
    by_cat: dict[str, list[LibraryHint]] = {}
    for h in hints:
        by_cat.setdefault(h.category, []).append(h)
    for cat, items in by_cat.items():
        for h in items:
            avoid_str = "; ".join(h.avoid) if h.avoid else "(no known alternates)"
            lines.append(
                f"  - {cat}: USE {h.use}. AVOID: {avoid_str}. "
                f"[detected via {h.confidence}]"
            )
    lines.append(
        "If a different library appears in your plan or diff, the harness "
        "will reject it. Stick to the list above."
    )
    return "\n".join(lines)
