from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.services import verification_profile as verification_profile_service  # noqa: E402
from app.services.verification_profile import _kotlinc_syntax_precheck  # noqa: E402


class FakeSandbox:
    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self.responses = list(responses or [])
        self.commands: list[str] = []

    def run(self, command: str, **kwargs: object) -> dict[str, object]:
        self.commands.append(command)
        if not self.responses:
            raise AssertionError(f"unexpected sandbox.run call: {command}")
        return self.responses.pop(0)


def _which_for(*, kotlinc: str | None = "/usr/bin/kotlinc", git: str | None = "/usr/bin/git"):
    def fake_which(executable: str) -> str | None:
        if executable == "kotlinc":
            return kotlinc
        if executable == "git":
            return git
        return None

    return fake_which


def test_skips_when_kotlinc_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox()
    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        _which_for(kotlinc=None),
    )

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is True
    assert message == "skipped: no kotlinc"
    assert sandbox.commands == []


def test_skips_when_no_kt_files_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox(
        [{"stdout": "README.md\nfoo.py\n", "stderr": "", "exit_code": 0}]
    )
    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        _which_for(kotlinc="/usr/bin/kotlinc"),
    )

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is True
    assert message == "skipped: no .kt changes"
    assert len(sandbox.commands) == 1


def test_passes_when_kotlinc_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox(
        [
            {"stdout": "Foo.kt\n", "stderr": "", "exit_code": 0},
            {"stdout": "", "stderr": "", "exit_code": 0},
        ]
    )
    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        _which_for(kotlinc="/usr/bin/kotlinc"),
    )

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is True
    assert "passed" in message
    assert len(sandbox.commands) == 2
    assert "Foo.kt" in sandbox.commands[1]


def test_fails_on_real_syntax_error(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox(
        [
            {"stdout": "Foo.kt\n", "stderr": "", "exit_code": 0},
            {"stdout": "", "stderr": "error: expecting '}'", "exit_code": 1},
        ]
    )
    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        _which_for(kotlinc="/usr/bin/kotlinc"),
    )

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is False
    assert "expecting" in message


def test_defers_to_gradle_on_unresolved_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox(
        [
            {"stdout": "Foo.kt\n", "stderr": "", "exit_code": 0},
            {"stdout": "", "stderr": "error: unresolved reference 'Foo'", "exit_code": 1},
        ]
    )
    monkeypatch.setattr(
        verification_profile_service.shutil,
        "which",
        _which_for(kotlinc="/usr/bin/kotlinc"),
    )

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is True
    assert "inconclusive" in message


def test_disabled_by_config(monkeypatch: pytest.MonkeyPatch) -> None:
    sandbox = FakeSandbox()
    settings = get_settings()
    monkeypatch.setattr(settings, "kotlinc_precheck_enabled", False)

    def fail_which(executable: str) -> str | None:
        raise AssertionError(f"unexpected shutil.which call: {executable}")

    monkeypatch.setattr(verification_profile_service.shutil, "which", fail_which)

    ok, message = _kotlinc_syntax_precheck(
        sandbox=sandbox,
        sandbox_workdir=Path("."),
    )

    assert ok is True
    assert message == "precheck disabled by config"
    assert sandbox.commands == []
