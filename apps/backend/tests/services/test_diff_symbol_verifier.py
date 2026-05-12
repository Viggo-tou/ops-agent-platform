"""Unit tests for the post-codegen symbol verifier."""
from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services.diff_symbol_verifier import (  # noqa: E402
    VerificationReport,
    _added_lines_per_file,
    _extract_members_from_class_body,
    verify_diff_symbols,
)


def test_added_lines_split_by_file() -> None:
    diff = dedent(
        """\
        diff --git a/Foo.kt b/Foo.kt
        --- a/Foo.kt
        +++ b/Foo.kt
        @@ -1,1 +1,2 @@
         existing
        +new line in Foo
        diff --git a/Bar.kt b/Bar.kt
        --- a/Bar.kt
        +++ b/Bar.kt
        @@ -1,1 +1,2 @@
         existing
        +new line in Bar
        """
    )
    by_file = _added_lines_per_file(diff)
    assert set(by_file.keys()) == {"Foo.kt", "Bar.kt"}
    assert by_file["Foo.kt"] == ["new line in Foo"]
    assert by_file["Bar.kt"] == ["new line in Bar"]


def test_extract_members_kotlin_class() -> None:
    body = dedent(
        """\
        class JobPostingViewModel : ViewModel() {
            var locationAddress by mutableStateOf("")
            var latitude by mutableStateOf(0.0)
            var longitude by mutableStateOf(0.0)
            fun clearData() { locationAddress = "" }
            private val internal = 5
        }
        """
    )
    members = _extract_members_from_class_body(body)
    assert "locationAddress" in members
    assert "latitude" in members
    assert "longitude" in members
    assert "clearData" in members
    assert "internal" in members
    # jobAddress doesn't exist — must NOT be in members.
    assert "jobAddress" not in members


def test_pascal_case_class_static_call_caught(tmp_path: Path) -> None:
    # Reproduce a v46-style hallucination using a PascalCase receiver
    # (class-as-receiver). `viewModel.jobAddress` (lowercase variable)
    # is intentionally skipped by the verifier — see
    # test_skips_lowercase_receiver_variable. compile_gate is the
    # safety net for variable-receiver cases.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "JobPostingViewModel.kt").write_text(
        dedent(
            """\
            class JobPostingViewModel : ViewModel() {
                var locationAddress by mutableStateOf("")
            }
            """
        ),
        encoding="utf-8",
    )
    diff = dedent(
        """\
        diff --git a/Other.kt b/Other.kt
        --- a/Other.kt
        +++ b/Other.kt
        @@ -1 +1,2 @@
         old
        +JobPostingViewModel.makeUpFactoryThatDoesntExist()
        """
    )

    report = verify_diff_symbols(diff=diff, repo_root=repo)

    assert report.has_hallucinations
    assert len(report.findings) == 1
    f = report.findings[0]
    assert f.receiver == "JobPostingViewModel"
    assert f.member == "makeUpFactoryThatDoesntExist"
    assert "locationAddress" in f.available_members_sample


def test_skips_lowercase_receiver_variable(tmp_path: Path) -> None:
    # Lowercase receivers (variables) can't have their type resolved
    # from a single diff line, so the verifier must not flag them as
    # hallucinations even if the .member doesn't exist anywhere.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Real.kt").write_text("class Foo { val knownField = 1 }\n", encoding="utf-8")
    diff = dedent(
        """\
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +viewModel.bogusField = 1
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    assert not report.has_hallucinations
    assert report.skipped_refs_unverifiable >= 1


def test_catches_pascal_case_class_member(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Real.kt").write_text(
        "class SessionManager { fun knownMethod() {} }\n",
        encoding="utf-8",
    )
    diff = dedent(
        """\
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +SessionManager.getHomeAddress(ctx)
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    assert report.has_hallucinations
    f = report.findings[0]
    assert f.receiver == "SessionManager"
    assert f.member == "getHomeAddress"
    assert "knownMethod" in f.available_members_sample


def test_passes_when_member_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Real.kt").write_text(
        "class SessionManager { fun knownMethod() {} }\n",
        encoding="utf-8",
    )
    diff = dedent(
        """\
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +SessionManager.knownMethod()
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    assert not report.has_hallucinations


def test_skips_external_library_receiver(tmp_path: Path) -> None:
    # FirebaseDatabase isn't declared anywhere in the test repo — the
    # verifier should defer (skip), not flag, since it's likely a
    # third-party library that compile_gate can validate.
    repo = tmp_path / "repo"
    repo.mkdir()
    diff = dedent(
        """\
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +FirebaseDatabase.getInstance()
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    assert not report.has_hallucinations


def test_blocklisted_receivers_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    diff = dedent(
        """\
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +Modifier.fillMaxWidth()
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    assert not report.has_hallucinations
    assert report.skipped_refs_blocklist >= 1


def test_skips_files_in_diff_for_receiver_resolution(tmp_path: Path) -> None:
    # If a class is being created BY the diff itself (currently has
    # stale on-disk content), we don't want the verifier to reject
    # references to it as hallucinated.
    repo = tmp_path / "repo"
    repo.mkdir()
    # The on-disk file has only an empty class body (pre-diff state).
    (repo / "NewClass.kt").write_text("class NewClass\n", encoding="utf-8")
    diff = dedent(
        """\
        diff --git a/NewClass.kt b/NewClass.kt
        --- a/NewClass.kt
        +++ b/NewClass.kt
        @@ -1 +1,3 @@
        -class NewClass
        +class NewClass {
        +    fun freshMethod() {}
        +}
        diff --git a/X.kt b/X.kt
        --- a/X.kt
        +++ b/X.kt
        @@ -1 +1,2 @@
         old
        +NewClass.freshMethod()
        """
    )
    report = verify_diff_symbols(diff=diff, repo_root=repo)
    # NewClass is in the diff — verifier should skip it (defer to
    # compile_gate which evaluates the post-patch sandbox state).
    assert not report.has_hallucinations


def test_repair_feedback_format() -> None:
    from app.services.diff_symbol_verifier import HallucinatedReference

    report = VerificationReport(
        findings=[
            HallucinatedReference(
                receiver="JobPostingViewModel",
                member="jobAddress",
                file="JobPostingFragment.kt",
                line="viewModel.jobAddress = homeAddress",
                receiver_resolved_to="JobPostingViewModel.kt",
                available_members_sample=["locationAddress", "latitude", "longitude"],
            )
        ],
    )
    feedback = report.repair_feedback()
    assert "jobAddress" in feedback
    assert "locationAddress" in feedback
    assert "Do not invent symbols" in feedback
