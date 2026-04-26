from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.services.knowledge import KnowledgeService, ScoredDocument  # noqa: E402
from app.services.knowledge_chunking import extract_enclosing_symbol  # noqa: E402


def _chunk_settings(**overrides: object) -> SimpleNamespace:
    values = {
        "knowledge_chunk_min_lines": 5,
        "knowledge_chunk_max_lines": 300,
        "knowledge_chunk_fallback_radius": 10,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _service_settings(source_root: Path, upload_root: Path, **overrides: object) -> Settings:
    values = {
        "knowledge_source_name": "fixture",
        "knowledge_source_path": str(source_root),
        "knowledge_upload_root": str(upload_root),
        "knowledge_synthesis_enabled": False,
        "knowledge_rerank_enabled": False,
        "knowledge_query_rewrite_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _document(*, relative_path: str, content: str, extension: str) -> KnowledgeDocument:
    raw = content.encode("utf-8")
    return KnowledgeDocument(
        id=f"doc-{relative_path.replace('/', '-').replace('.', '-')}",
        source_name="fixture",
        relative_path=relative_path,
        title=Path(relative_path).name,
        extension=extension,
        language=None,
        size_bytes=len(raw),
        line_count=len(content.splitlines()),
        content_hash=hashlib.sha256(raw).hexdigest(),
        metadata_json={},
        content=content,
    )


def _citation_for(
    *,
    relative_path: str,
    content: str,
    extension: str,
    query_tokens: list[str],
    settings: object | None = None,
):
    scored = ScoredDocument(
        document=_document(relative_path=relative_path, content=content, extension=extension),
        score=42.0,
        matched_tokens=set(query_tokens),
    )
    return KnowledgeService._build_citation(
        scored=scored,
        query_tokens=query_tokens,
        settings=settings or _chunk_settings(),
    )


def _js_login_fixture() -> str:
    lines = [
        'import React from "react";',
        'import { database } from "../firebase";',
        'import "./Login.css";',
        'import { useNavigate } from "react-router-dom";',
        'import { toast } from "react-toastify";',
        "",
        "function handleLogin() {",
        '  const email = form.email;',
        '  const password = form.password;',
    ]
    while len(lines) < 28:
        lines.append(f"  const checkpoint{len(lines)} = password.length;")
    lines.append('  return database.ref("admins").orderByChild("email").equalTo(email).once("value");')
    lines.append("}")
    lines.extend(["", "export default function Login() {", "  return null;", "}"])
    return "\n".join(lines)


def _ts_login_fixture() -> str:
    lines = [
        'import { database } from "../firebase";',
        'import type { LoginPayload } from "./types";',
        'import "./Login.css";',
        "",
        "export async function handleLogin(payload: LoginPayload): Promise<void> {",
        '  const email = payload.email;',
        '  const password = payload.password;',
        '  await database.ref("admins").orderByChild("email").equalTo(email).once("value");',
    ]
    while len(lines) < 24:
        lines.append(f"  const checkpoint{len(lines)} = password.length;")
    lines.append("}")
    lines.extend(["", "export const pageTitle = 'Login';"])
    return "\n".join(lines)


def _python_login_fixture() -> str:
    lines = [
        "from firebase import database",
        "from pathlib import Path",
        "",
        "LOGIN_TABLE = 'admins'",
        "",
        "",
        "def handle_login(form):",
        "    email = form['email']",
        "    password = form['password']",
        "    result = database.child(LOGIN_TABLE).order_by_child('email').equal_to(email).get()",
    ]
    while len(lines) < 29:
        lines.append(f"    audit_step_{len(lines)} = password")
    lines.append("    return result")
    lines.extend(["", "def other_function():", "    return None"])
    return "\n".join(lines)


def test_js_hit_expands_to_handle_login_symbol() -> None:
    content = _js_login_fixture()
    symbol = extract_enclosing_symbol(content=content, extension=".js", target_line=29)

    assert symbol is not None
    assert symbol.start_line == 7
    assert symbol.end_line == 30
    assert symbol.enclosing_symbol == "handleLogin"
    assert symbol.chunk_kind == "function"

    citation = _citation_for(
        relative_path="src/Login.js",
        extension=".js",
        content=content,
        query_tokens=["ref"],
    )

    assert citation.line_start == 7
    assert citation.line_end == 30
    assert "function handleLogin()" in citation.snippet
    assert "database.ref" in citation.snippet
    assert citation.metadata["enclosing_symbol"] == "handleLogin"
    assert citation.metadata["chunk_kind"] == "function"
    assert citation.metadata["truncated"] is False


def test_typescript_hit_expands_to_handle_login_symbol() -> None:
    content = _ts_login_fixture()
    symbol = extract_enclosing_symbol(content=content, extension=".ts", target_line=8)

    assert symbol is not None
    assert symbol.start_line == 5
    assert symbol.end_line == 25
    assert symbol.enclosing_symbol == "handleLogin"
    assert symbol.chunk_kind == "function"

    citation = _citation_for(
        relative_path="src/Login.ts",
        extension=".ts",
        content=content,
        query_tokens=["ref"],
    )

    assert citation.line_start == 5
    assert citation.line_end == 25
    assert "function handleLogin" in citation.snippet
    assert "database.ref" in citation.snippet
    assert citation.metadata["enclosing_symbol"] == "handleLogin"
    assert citation.metadata["chunk_kind"] == "function"


def test_python_import_hit_expands_to_module_covering_handle_login() -> None:
    citation = _citation_for(
        relative_path="auth/login.py",
        extension=".py",
        content=_python_login_fixture(),
        query_tokens=["firebase"],
    )

    assert citation.line_start == 1
    assert citation.line_end >= 30
    assert "def handle_login(form):" in citation.snippet
    assert "database.child" in citation.snippet
    assert citation.metadata["chunk_kind"] == "module"


def test_python_method_hit_records_enclosing_symbol_metadata() -> None:
    content = "\n".join(
        [
            "class Login:",
            "    def handle_login(self):",
            "        firebase_token = self.token",
            "        return firebase_token",
            "",
            "def other():",
            "    return None",
        ]
    )
    citation = _citation_for(
        relative_path="auth/login.py",
        extension=".py",
        content=content,
        query_tokens=["firebase"],
    )

    assert citation.line_start == 1
    assert citation.line_end == 5
    assert citation.metadata["enclosing_symbol"] == "handle_login"
    assert citation.metadata["chunk_kind"] == "method"


def test_markdown_uses_wider_line_window_fallback() -> None:
    content = "\n".join(f"line {index}" for index in range(1, 31))
    citation = _citation_for(
        relative_path="README.md",
        extension=".md",
        content=content.replace("line 16", "line 16 firebase"),
        query_tokens=["firebase"],
    )

    assert citation.line_start == 6
    assert citation.line_end == 26
    assert citation.metadata["chunk_kind"] == "line_window"
    assert "line 16 firebase" in citation.snippet


def test_snippet_cap_truncates_large_python_function() -> None:
    lines = ["def handle_login():"]
    lines.append("    firebase_token = 'x'")
    lines.extend(f"    step_{index} = firebase_token" for index in range(3, 352))
    lines.append("    return firebase_token")
    citation = _citation_for(
        relative_path="auth/large.py",
        extension=".py",
        content="\n".join(lines),
        query_tokens=["firebase"],
        settings=_chunk_settings(knowledge_chunk_max_lines=300),
    )

    assert citation.line_start == 1
    assert citation.line_end == 300
    assert citation.metadata["enclosing_symbol"] == "handle_login"
    assert citation.metadata["chunk_kind"] == "function"
    assert citation.metadata["truncated"] is True
    assert "... [truncated, full symbol: handle_login, lines 1-352]" in citation.snippet


@pytest.fixture()
def workspace_tmp():
    parent = BACKEND_ROOT / ".tmp-test"
    root = parent / uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
        try:
            parent.rmdir()
        except OSError:
            pass


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_repository_sync_excludes_resource_and_binary_files(workspace_tmp: Path, db_session) -> None:
    source_root = workspace_tmp / "source"
    source_root.mkdir()
    (source_root / "Login.js").write_text(_js_login_fixture(), encoding="utf-8")
    (source_root / "Login.css").write_text(".login { color: red; }\n", encoding="utf-8")
    (source_root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (source_root / "binary.txt").write_bytes(b"\x00" * 200)

    service = KnowledgeService(db_session)
    service.settings = _service_settings(source_root, workspace_tmp / "uploads")

    sync = service.sync_repositories()
    result = service.search_repositories(query="firebase", top_k=4)
    indexed_paths = {document.relative_path for document in service.list_documents()}

    assert sync.indexed_documents == 1
    assert indexed_paths == {"Login.js"}
    assert result.citations
    assert {citation.relative_path for citation in result.citations} == {"Login.js"}


def test_upload_documents_skips_excluded_resources(workspace_tmp: Path, db_session) -> None:
    source_root = workspace_tmp / "source"
    source_root.mkdir()
    service = KnowledgeService(db_session)
    service.settings = _service_settings(source_root, workspace_tmp / "uploads")

    response = service.upload_documents(
        source_name="uploads",
        files=[
            ("notes.md", b"firebase notes\n"),
            ("Login.css", b".login { color: red; }\n"),
            ("logo.png", b"\x89PNG\r\n\x1a\n"),
        ],
    )

    assert [document.relative_path for document in response.indexed_documents] == ["notes.md"]
    assert {item.file_name for item in response.skipped} == {"Login.css", "logo.png"}
