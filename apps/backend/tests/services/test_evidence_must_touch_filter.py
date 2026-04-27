from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def _settings(*, include_configs: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        evidence_must_touch_excluded_extensions=(
            ".lock,.min.js,.min.css,.map,.tar,.gz,.zip,.7z,.rar,.pdf,"
            ".png,.jpg,.jpeg,.gif,.svg,.webp,.ico,.bmp,"
            ".woff,.woff2,.ttf,.otf,.eot,"
            ".mp3,.mp4,.mov,.wav,.avi,.mkv,"
            ".pyc,.pyo,.class,.dll,.so,.dylib,.exe"
        ),
        evidence_must_touch_excluded_path_segments=(
            "build/,build-before/,build-after/,dist/,node_modules/,"
            "__pycache__/,.next/,.cache/,.tmp/,data/sandboxes/,data/agent_workspace/"
        ),
        evidence_must_touch_excluded_filenames=(
            "package.json,package-lock.json,yarn.lock,pnpm-lock.yaml,"
            "tsconfig.json,jsconfig.json,.eslintrc*,.prettierrc*,.editorconfig,"
            "cors.json,firebase.json,poetry.lock,requirements.txt,requirements-*.txt,"
            "go.sum,cargo.lock"
        ),
        evidence_must_touch_include_configs=include_configs,
    )


def test_package_json_is_filtered_from_must_touch() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["package.json"],
        settings=_settings(),
    ) == []


def test_normal_source_file_passes_must_touch_filter() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["src/components/Sidebar.js"],
        settings=_settings(),
    ) == ["src/components/Sidebar.js"]


def test_build_before_bundle_is_filtered_by_path_segment() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["build-before/static/js/main.abc.js"],
        settings=_settings(),
    ) == []


def test_css_source_file_passes_must_touch_filter() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["src/styles/Login.css"],
        settings=_settings(),
    ) == ["src/styles/Login.css"]


def test_cors_json_is_filtered_from_must_touch() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["cors.json"],
        settings=_settings(),
    ) == []


def test_cors_json_passes_when_config_filter_is_disabled() -> None:
    from app.services.evidence_bundle import _filter_must_touch_files

    assert _filter_must_touch_files(
        ["cors.json"],
        settings=_settings(include_configs=True),
    ) == ["cors.json"]


def test_filtered_files_remain_evidence_candidates(tmp_path: Path) -> None:
    from app.services.evidence_bundle import build_evidence_bundle

    (tmp_path / "src").mkdir()
    (tmp_path / "build-before" / "static" / "js").mkdir(parents=True)
    (tmp_path / "src" / "components").mkdir()
    (tmp_path / "package.json").write_text('{"privacy": true}\n', encoding="utf-8")
    (tmp_path / "cors.json").write_text('{"privacy": true}\n', encoding="utf-8")
    (tmp_path / "build-before" / "static" / "js" / "main.abc.js").write_text(
        "const privacy = true;\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "components" / "Sidebar.js").write_text(
        "export const privacy = true;\n",
        encoding="utf-8",
    )

    result = build_evidence_bundle(
        request_text='update "privacy" handling',
        normalized_request=None,
        source_tree=tmp_path,
        has_destructive_verb=True,
        settings=_settings(),
    )

    assert "src/components/Sidebar.js" in result.must_touch_files
    assert "package.json" not in result.must_touch_files
    assert "cors.json" not in result.must_touch_files
    assert "build-before/static/js/main.abc.js" not in result.must_touch_files
    assert "package.json" in result.candidate_files
    assert "cors.json" in result.candidate_files
    assert "build-before/static/js/main.abc.js" in result.candidate_files
