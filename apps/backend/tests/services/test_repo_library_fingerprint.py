"""Unit tests for the repo library fingerprinter."""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.repo_library_fingerprint import (  # noqa: E402
    fingerprint_repository,
    render_library_hints_block,
)


def _make_app_repo(tmp: Path, build_gradle: str, kt_files: dict[str, str] | None = None) -> Path:
    repo = tmp / "repo"
    repo.mkdir()
    (repo / "app").mkdir()
    (repo / "app" / "build.gradle").write_text(build_gradle, encoding="utf-8")
    if kt_files:
        src = repo / "app" / "src" / "main" / "java"
        src.mkdir(parents=True)
        for name, content in kt_files.items():
            (src / name).write_text(content, encoding="utf-8")
    return repo


def test_handymanapp_style_osmdroid_firebase(tmp_path: Path) -> None:
    """Reproduce HandymanApp's actual fingerprint: OSMDroid + Firebase RTDB."""
    build = dedent(
        """\
        dependencies {
            implementation "org.osmdroid:osmdroid-android:6.1.18"
            implementation "com.google.firebase:firebase-database"
            implementation "com.google.firebase:firebase-auth"
            implementation "androidx.compose.material3:material3"
        }
        """
    )
    kt = {
        "Foo.kt": dedent(
            """\
            package com.example
            import org.osmdroid.views.MapView
            import org.osmdroid.util.GeoPoint
            import com.google.firebase.database.FirebaseDatabase
            import androidx.compose.material3.Text
            import kotlinx.coroutines.launch
            import android.location.Geocoder
            """
        ),
    }
    repo = _make_app_repo(tmp_path, build, kt)

    hints = fingerprint_repository(repo)
    categories = {(h.category, h.use) for h in hints}
    use_strings = {h.use for h in hints}

    assert any("osmdroid" in u for u in use_strings)
    assert any("Firebase Realtime Database" in u for u in use_strings)
    assert any("Material3" in u for u in use_strings)
    assert any("Geocoder" in u for u in use_strings)
    # Should NOT detect Google Maps for this repo.
    assert not any("Google Maps" in u for u in use_strings)


def test_renders_hints_with_avoid_list(tmp_path: Path) -> None:
    build = dedent(
        """\
        dependencies {
            implementation "org.osmdroid:osmdroid-android:6.1.18"
        }
        """
    )
    repo = _make_app_repo(tmp_path, build, {"x.kt": "import org.osmdroid.views.MapView"})
    hints = fingerprint_repository(repo)
    block = render_library_hints_block(hints)

    assert "REPO LIBRARY HINTS" in block
    assert "osmdroid" in block
    assert "Google Maps" in block  # in the AVOID list
    assert "harness will reject it" in block


def test_empty_repo_returns_no_hints(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    hints = fingerprint_repository(repo)
    assert hints == []
    assert render_library_hints_block(hints) == ""


def test_google_maps_alternative_repo(tmp_path: Path) -> None:
    """A repo using Google Maps should fingerprint as Google Maps."""
    build = dedent(
        """\
        dependencies {
            implementation "com.google.android.gms:play-services-maps:18.0.0"
        }
        """
    )
    kt = {"Map.kt": "import com.google.android.gms.maps.GoogleMap\n"}
    repo = _make_app_repo(tmp_path, build, kt)

    hints = fingerprint_repository(repo)
    use_strings = {h.use for h in hints}
    assert any("Google Maps" in u for u in use_strings)
    # The OSMDroid rule must NOT match this repo.
    assert not any("osmdroid" in u for u in use_strings)


def test_react_vite_frontend(tmp_path: Path) -> None:
    repo = tmp_path / "web"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"dependencies": {"react": "^18", "vite": "^5"}}',
        encoding="utf-8",
    )
    (repo / "src").mkdir()
    (repo / "src" / "App.tsx").write_text(
        "import React from 'react'\nexport default function App() {}\n",
        encoding="utf-8",
    )

    hints = fingerprint_repository(repo)
    use_strings = {h.use for h in hints}
    assert any("React" in u for u in use_strings)
    assert any("Vite" in u for u in use_strings)


def test_missing_repo_returns_empty(tmp_path: Path) -> None:
    nonexistent = tmp_path / "does-not-exist"
    assert fingerprint_repository(nonexistent) == []
