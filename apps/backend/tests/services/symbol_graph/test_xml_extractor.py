"""Tests for the XML SymbolExtractor (Android resources)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

pytest.importorskip("lxml")

from app.services.symbol_graph.xml_extractor import XmlExtractor  # noqa: E402
from app.services.symbol_graph.registry import (  # noqa: E402
    _clear_registry_for_tests,
    get_extractor_for_path,
)


@pytest.fixture(autouse=True)
def _isolate_registry():
    _clear_registry_for_tests()
    import importlib
    from app.services.symbol_graph import xml_extractor as _xml
    importlib.reload(_xml)
    yield
    _clear_registry_for_tests()


def test_xml_extractor_registered_for_xml():
    assert get_extractor_for_path("AndroidManifest.xml") is not None


# --- Decls (resource definitions) ------------------------------------------

def test_string_decl_extracted_with_correct_kind():
    src = (b"<?xml version=\"1.0\"?><resources>"
           b"<string name=\"hello\">Hi</string>"
           b"</resources>")
    res = XmlExtractor().extract(path="strings.xml", source=src)
    decls_by_name = {d.name: d for d in res.decls}
    assert "hello" in decls_by_name
    assert decls_by_name["hello"].kind == "string"


def test_color_drawable_dimen_decls_get_their_tag_as_kind():
    src = (b"<?xml version=\"1.0\"?><resources>"
           b"<color name=\"primary\">#FF0000</color>"
           b"<dimen name=\"padding_lg\">16dp</dimen>"
           b"<bool name=\"feature_x\">true</bool>"
           b"</resources>")
    res = XmlExtractor().extract(path="values.xml", source=src)
    by_name = {d.name: d.kind for d in res.decls}
    assert by_name == {"primary": "color", "padding_lg": "dimen", "feature_x": "bool"}


def test_layout_structural_elements_NOT_treated_as_decls():
    """LinearLayout, TextView etc. don't define globally-named resources;
    they should NOT show up as decls (only @+id/X declarations should)."""
    src = (b"<?xml version=\"1.0\"?>"
           b"<LinearLayout xmlns:android=\"http://schemas.android.com/apk/res/android\">"
           b"<TextView android:layout_width=\"wrap_content\"/>"
           b"</LinearLayout>")
    res = XmlExtractor().extract(path="layout/main.xml", source=src)
    decl_names = {d.name for d in res.decls}
    # No `name` attribute on LinearLayout/TextView, so nothing here
    assert decl_names == set()


def test_plus_id_creates_id_decl_AND_ref():
    """Android's @+id/foo declares an id on the fly. Our extractor
    records both an id Decl (for resolvers) and an id Ref (for blast
    radius). Both being present is fine — resolve() succeeds either way."""
    src = (b"<?xml version=\"1.0\"?>"
           b"<TextView xmlns:android=\"http://schemas.android.com/apk/res/android\""
           b"          android:id=\"@+id/etAddress\"/>")
    res = XmlExtractor().extract(path="layout/x.xml", source=src)
    id_decls = [d for d in res.decls if d.kind == "id" and d.name == "etAddress"]
    id_refs = [r for r in res.refs if r.name == "etAddress" and r.expected_kind == "id"]
    assert len(id_decls) == 1
    assert len(id_refs) == 1


# --- Refs (resource references) --------------------------------------------

def test_string_ref_extracted_with_correct_expected_kind():
    src = (b"<?xml version=\"1.0\"?>"
           b"<TextView xmlns:android=\"http://schemas.android.com/apk/res/android\""
           b"          android:text=\"@string/welcome\"/>")
    res = XmlExtractor().extract(path="layout/x.xml", source=src)
    refs = [r for r in res.refs if r.name == "welcome"]
    assert len(refs) == 1
    assert refs[0].expected_kind == "string"


def test_drawable_color_layout_refs_carry_their_type():
    src = (b"<?xml version=\"1.0\"?>"
           b"<View xmlns:android=\"http://schemas.android.com/apk/res/android\""
           b"      android:background=\"@drawable/bg\""
           b"      android:textColor=\"@color/primary\"/>")
    res = XmlExtractor().extract(path="layout/x.xml", source=src)
    by_name = {r.name: r.expected_kind for r in res.refs}
    assert by_name == {"bg": "drawable", "primary": "color"}


def test_android_namespaced_string_ref():
    """Refs like `@android:string/ok` should still extract the inner type+name."""
    src = (b"<?xml version=\"1.0\"?>"
           b"<TextView xmlns:android=\"http://schemas.android.com/apk/res/android\""
           b"          android:text=\"@android:string/ok\"/>")
    res = XmlExtractor().extract(path="layout/x.xml", source=src)
    refs = [r for r in res.refs if r.name == "ok"]
    assert len(refs) == 1
    assert refs[0].expected_kind == "string"


def test_v9_failure_pattern_reproduced():
    """The exact v9 P69-17 dogfood failure mode: AndroidManifest references
    @string/google_maps_api_key but no `<string name="google_maps_api_key">`
    decl in strings.xml. Extractor must capture the ref so the gate can flag
    it as no_decl_found across the two-file resource graph.
    """
    manifest = (
        b"<?xml version=\"1.0\"?>"
        b"<manifest>"
        b"  <application>"
        b"    <meta-data"
        b"      android:name=\"com.google.android.geo.API_KEY\""
        b"      android:value=\"@string/google_maps_api_key\"/>"
        b"  </application>"
        b"</manifest>"
    )
    res_manifest = XmlExtractor().extract(path="AndroidManifest.xml", source=manifest)
    refs = [r for r in res_manifest.refs if r.name == "google_maps_api_key"]
    assert len(refs) == 1
    assert refs[0].expected_kind == "string"
    # AndroidManifest itself doesn't define the string — it only refs.
    assert not any(d.name == "google_maps_api_key" for d in res_manifest.decls)


def test_invalid_xml_does_not_crash():
    src = b"<unclosed tag here"
    res = XmlExtractor().extract(path="bad.xml", source=src)
    assert isinstance(res.decls, tuple)
    assert isinstance(res.refs, tuple)
