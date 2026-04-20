"""Tests for runtime_validation source file filtering."""
import pytest

from app.services.runtime_validation import (
    _is_source_file,
    build_source_manifest,
    validate_diff_semantics,
)


class TestIsSourceFile:
    def test_js_source(self):
        assert _is_source_file("src/pages/Login.js") is True

    def test_tsx_source(self):
        assert _is_source_file("components/App.tsx") is True

    def test_python_source(self):
        assert _is_source_file("app/main.py") is True

    def test_package_lock_excluded(self):
        assert _is_source_file("package-lock.json") is False

    def test_yarn_lock_excluded(self):
        assert _is_source_file("yarn.lock") is False

    def test_build_dir_excluded(self):
        assert _is_source_file("build-before/static/js/main.7cb791f7.js") is False

    def test_build_after_excluded(self):
        assert _is_source_file("build-after/static/js/main.js") is False

    def test_dist_excluded(self):
        assert _is_source_file("dist/bundle.js") is False

    def test_node_modules_excluded(self):
        assert _is_source_file("node_modules/react/index.js") is False

    def test_chunk_file_excluded(self):
        assert _is_source_file("src/static/js/239.9255f836.chunk.js") is False

    def test_min_file_excluded(self):
        assert _is_source_file("dist/app.min.js") is False

    def test_css_source(self):
        assert _is_source_file("src/styles/Login.css") is True

    def test_json_in_src_not_source(self):
        # Regular .json is not in _SOURCE_EXTENSIONS
        assert _is_source_file("src/data/config.json") is False

    def test_coverage_excluded(self):
        assert _is_source_file("coverage/lcov-report/index.html") is False

    def test_pycache_excluded(self):
        assert _is_source_file("__pycache__/module.cpython-311.pyc") is False

    def test_html_source(self):
        assert _is_source_file("public/index.html") is True

    def test_scss_source(self):
        assert _is_source_file("src/styles/app.scss") is True

    def test_kotlin_source(self):
        assert _is_source_file("src/main/kotlin/App.kt") is True

    def test_java_source(self):
        assert _is_source_file("src/main/java/App.java") is True

    def test_pnpm_lock_excluded(self):
        assert _is_source_file("pnpm-lock.yaml") is False


class TestBuildSourceManifest:
    def test_separates_source_and_generated(self):
        files = [
            "src/Login.js",
            "src/api/auth.js",
            "package-lock.json",
            "build-before/main.chunk.js",
            "build-after/main.js",
        ]
        result = build_source_manifest(files)
        assert result["source_files"] == ["src/Login.js", "src/api/auth.js"]
        assert "package-lock.json" in result["generated_files"]
        assert "build-before/main.chunk.js" in result["generated_files"]
        assert "build-after/main.js" in result["generated_files"]

    def test_empty_input(self):
        result = build_source_manifest([])
        assert result["source_files"] == []
        assert result["generated_files"] == []

    def test_all_source(self):
        files = ["src/app.py", "src/main.tsx"]
        result = build_source_manifest(files)
        assert result["source_files"] == files
        assert result["generated_files"] == []

    def test_all_generated(self):
        files = ["package-lock.json", "dist/bundle.js"]
        result = build_source_manifest(files)
        assert result["source_files"] == []
        assert len(result["generated_files"]) == 2


class TestReplacementCompletenessFiltered:
    """Verify that _check_replacement_completeness skips non-source files."""

    def test_no_false_positive_on_build_artifacts(self):
        diff = (
            "--- a/src/Login.js\n"
            "+++ b/src/Login.js\n"
            '-  role = "Master Admin"\n'
            '+  role = "Admin"\n'
        )
        # context_files includes a build artifact that contains "Master Admin"
        context_files = {
            "src/Login.js": 'const role = "Master Admin";',
            "build-before/static/js/main.7cb791f7.js": '..."Master Admin"...',
            "package-lock.json": '..."test"...',
        }
        report = validate_diff_semantics(diff, context_files)
        # Should NOT produce findings for build artifacts
        non_source_findings = [
            f for f in report.findings
            if "build-before" in f.file or "package-lock" in f.file
        ]
        assert len(non_source_findings) == 0

    def test_still_flags_source_files(self):
        """Source files that contain a replaced string should still be flagged."""
        diff = (
            "--- a/src/Login.js\n"
            "+++ b/src/Login.js\n"
            '-  role = "Master Admin"\n'
            '+  role = "Admin"\n'
        )
        context_files = {
            "src/Login.js": 'const role = "Master Admin";',
            "src/Dashboard.js": 'const role = "Master Admin";',
        }
        report = validate_diff_semantics(diff, context_files)
        source_findings = [
            f for f in report.findings
            if f.file == "src/Dashboard.js"
        ]
        assert len(source_findings) > 0


class TestCaseSensitiveComparisonsFiltered:
    """Verify that _check_case_sensitive_comparisons skips non-source files."""

    def test_no_finding_on_build_artifact_diff(self):
        diff = (
            "+++ b/build-after/static/js/main.js\n"
            '+  if (role === "admin") {\n'
        )
        context_files = {
            "build-after/static/js/main.js": 'role = "Admin";',
        }
        report = validate_diff_semantics(diff, context_files)
        build_findings = [
            f for f in report.findings
            if "build-after" in f.file
        ]
        assert len(build_findings) == 0

    def test_still_flags_source_diff(self):
        diff = (
            "+++ b/src/Login.js\n"
            '+  if (role === "admin") {\n'
        )
        context_files = {
            "src/Login.js": 'role = "Admin";',
        }
        report = validate_diff_semantics(diff, context_files)
        # The case-sensitive check should flag this source file
        source_findings = [
            f for f in report.findings
            if f.file == "src/Login.js" and f.rule == "case_sensitive_comparison"
        ]
        assert len(source_findings) > 0
