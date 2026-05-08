"""Unit tests for repository_registry module."""
from __future__ import annotations

import sys
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import repository_registry as rr  # noqa: E402


@pytest.fixture
def temp_registry(monkeypatch, tmp_path):
    target = tmp_path / "repositories"
    monkeypatch.setattr(rr, "_data_root", lambda: target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _make_zip(files: dict[str, str]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_upload_extracts_and_registers(temp_registry):
    zip_bytes = _make_zip({"hello.txt": "world"})
    record = rr.upload_zip_source(name="my project", description="demo", zip_bytes=zip_bytes)
    assert record.name == "my-project"
    assert record.origin == "upload"
    assert (Path(record.path) / "hello.txt").read_text() == "world"


def test_upload_strips_common_top_dir(temp_registry):
    zip_bytes = _make_zip({"toplevel/inner/file.txt": "x"})
    record = rr.upload_zip_source(name="strip", description="", zip_bytes=zip_bytes)
    assert (Path(record.path) / "inner" / "file.txt").read_text() == "x"


def test_upload_rejects_zip_bomb(temp_registry, monkeypatch):
    monkeypatch.setattr(rr, "_UPLOAD_MAX_ENTRIES", 2)
    zip_bytes = _make_zip({f"file{i}.txt": "x" for i in range(5)})
    with pytest.raises(rr.RegistryError):
        rr.upload_zip_source(name="bomb", description="", zip_bytes=zip_bytes)


def test_remove_managed_source_deletes_dir(temp_registry):
    zip_bytes = _make_zip({"a.txt": "a"})
    record = rr.upload_zip_source(name="ephemeral", description="", zip_bytes=zip_bytes)
    assert Path(record.path).exists()
    assert rr.remove_managed_source(record.name) is True
    assert not Path(record.path).exists()
    assert rr.remove_managed_source(record.name) is False


def test_resolve_path_by_name_managed(temp_registry):
    zip_bytes = _make_zip({"x.txt": "x"})
    record = rr.upload_zip_source(name="findme", description="", zip_bytes=zip_bytes)
    assert rr.resolve_path_by_name("findme") == record.path
    assert rr.resolve_path_by_name("nonexistent") is None


def test_validate_clone_url_rejects_ssh():
    with pytest.raises(rr.RegistryError):
        rr._validate_clone_url("git@github.com:user/repo.git")
    with pytest.raises(rr.RegistryError):
        rr._validate_clone_url("")
    rr._validate_clone_url("https://github.com/user/repo.git")
