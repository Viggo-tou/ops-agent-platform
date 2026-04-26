from __future__ import annotations

import hashlib
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.knowledge_document import KnowledgeDocument
from app.schemas.knowledge import (
    KnowledgeAnswerTrace,
    KnowledgeCitation,
    KnowledgeDeleteResponse,
    KnowledgeDocumentSummary,
    KnowledgeSearchResult,
    KnowledgeSourceDescriptor,
    KnowledgeSyncResponse,
    KnowledgeUploadResponse,
    KnowledgeUploadSkipped,
)
from app.services.knowledge_chunking import build_snippet

UPLOAD_ACCEPTED_EXTENSIONS = {".md", ".txt", ".json", ".yml", ".yaml", ".properties"}
SOURCE_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

TEXT_EXTENSIONS = {
    ".kt",
    ".java",
    ".xml",
    ".gradle",
    ".md",
    ".txt",
    ".json",
    ".properties",
    ".yml",
    ".yaml",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".py",
    ".css",
    ".scss",
    ".html",
    ".vue",
}
IGNORED_PARTS = {
    ".git", ".gradle", ".idea", "__pycache__", "node_modules",
    "build", "dist", ".next", "out", "target", "bin", "obj",
    ".cache", ".turbo", ".parcel-cache", ".vite", ".svelte-kit",
    "coverage", ".nyc_output", ".venv", "venv", ".tox",
    # Common "build" sibling directories used for A/B comparisons
    "build-before", "build-after", "dist-before", "dist-after",
}
# Directory part prefixes that indicate build output regardless of exact name.
IGNORED_PART_PREFIXES = ("build-", "dist-")
# File name patterns (basename) to exclude even if extension is listed in
# TEXT_EXTENSIONS. These are generated / bundled / lock files that pollute
# retrieval results (e.g. A-05 baseline pulled build/.../main.xxx.js.LICENSE.txt
# into the top-5 before the real src/components/ExportReportButton.js).
IGNORED_FILENAME_SUFFIXES = (
    ".min.js", ".min.css",
    ".bundle.js", ".bundle.css",
    ".chunk.js", ".chunk.css",
    ".map",
    ".LICENSE.txt",
    "-lock.json",
)
IGNORED_FILENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
    "composer.lock", "Gemfile.lock", "poetry.lock", "Cargo.lock",
    "firebase-debug.log",
})


def _is_ignored_path(file_path: Path) -> bool:
    """Central deny rule: return True if the path should be excluded from
    knowledge retrieval. Covers directory-part matches, prefix matches on
    build-* siblings, basename-level deny list, and suffix patterns for
    generated / minified / lock / sourcemap artefacts.
    """
    parts = file_path.parts
    for part in parts:
        if part in IGNORED_PARTS:
            return True
        for prefix in IGNORED_PART_PREFIXES:
            if part.startswith(prefix):
                return True
    name = file_path.name
    if name in IGNORED_FILENAMES:
        return True
    for suffix in IGNORED_FILENAME_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _excluded_file_patterns(settings: object | None = None) -> set[str]:
    settings = settings or get_settings()
    raw_value = str(getattr(settings, "knowledge_excluded_extensions", "") or "")
    patterns: set[str] = set()
    for raw_item in raw_value.replace(";", ",").split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = f".{item}"
        patterns.add(item)
    return patterns


def _is_excluded_resource_file(file_path: Path, settings: object | None = None) -> bool:
    name = file_path.name.lower()
    suffix = file_path.suffix.lower()
    for pattern in _excluded_file_patterns(settings):
        if suffix == pattern or name.endswith(pattern):
            return True
    return False


def _decode_indexable_content(raw_bytes: bytes) -> str | None:
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if _non_printable_ratio(content) > 0.01:
        return None
    return content


def _non_printable_ratio(content: str) -> float:
    if not content:
        return 0.0
    allowed_controls = {"\n", "\r", "\t", "\f"}
    non_printable = sum(1 for char in content if not char.isprintable() and char not in allowed_controls)
    return non_printable / len(content)
TOKEN_PATTERN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]+")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")
SEMANTIC_EXPANSIONS = {
    "login": {"auth", "signin", "customerlogin"},
    "auth": {"login", "signin", "customerlogin"},
    "chat": {"chatbox", "message", "messages"},
    "message": {"chat", "chatbox"},
    "debug": {"error", "exception", "trace", "logcat", "crash"},
    "error": {"debug", "exception", "crash", "failure"},
    "exception": {"error", "traceback", "stacktrace", "crash"},
    "test": {"androidtest", "unittest", "test", "instrumentedtest"},
    "ui": {"layout", "fragment", "activity", "xml"},
    "layout": {"ui", "xml", "drawable", "navigation"},
    "build": {"gradle", "dependency", "manifest", "config"},
    "gradle": {"build", "dependency", "config"},
    "config": {"configuration", "settings", "properties", "json", "manifest"},
    "configuration": {"config", "settings", "properties", "json", "manifest"},
    "firebase": {"google", "services", "google-services", "google_services", "googleservices"},
    "file": {"files", "path", "source", "json", "xml", "properties"},
    "files": {"file", "path", "source", "json", "xml", "properties"},
}


def _language_from_extension(extension: str) -> str | None:
    mapping = {
        ".kt": "kotlin",
        ".java": "java",
        ".xml": "xml",
        ".gradle": "gradle",
        ".md": "markdown",
        ".json": "json",
        ".properties": "properties",
        ".yml": "yaml",
        ".yaml": "yaml",
        ".txt": "text",
        ".py": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx",
    }
    return mapping.get(extension.lower())


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_PATTERN.findall(text)]


def _expand_tokens(tokens: list[str]) -> set[str]:
    expanded = set(tokens)
    for token in tokens:
        expanded.update(SEMANTIC_EXPANSIONS.get(token, set()))
    return expanded


@dataclass(frozen=True)
class SourceSpec:
    name: str
    path: Path


@dataclass(frozen=True)
class QueryRoute:
    kind: str
    reason: str
    preferred_extensions: tuple[str, ...]
    preferred_path_terms: tuple[str, ...]
    source_candidates: tuple[str, ...]


@dataclass(frozen=True)
class ScoredDocument:
    document: KnowledgeDocument
    score: float
    matched_tokens: set[str]


class KnowledgeService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def sync_repositories(self, *, source_name: str | None = None) -> KnowledgeSyncResponse:
        source_specs = self._resolve_source_specs()
        if source_name:
            source_specs = [spec for spec in source_specs if spec.name == source_name]
            if not source_specs:
                raise ValueError(f"Unknown knowledge source: {source_name}")

        total_indexed = 0
        total_updated = 0
        total_removed = 0
        primary_source_name = source_specs[0].name
        primary_source_path = str(source_specs[0].path)

        for spec in source_specs:
            indexed, updated, removed = self._sync_single_repository(spec)
            total_indexed += indexed
            total_updated += updated
            total_removed += removed

        self.db.commit()
        return KnowledgeSyncResponse(
            source_name=primary_source_name if len(source_specs) == 1 else "multiple",
            source_path=primary_source_path if len(source_specs) == 1 else "multiple",
            indexed_documents=total_indexed,
            updated_documents=total_updated,
            removed_documents=total_removed,
        )

    def ensure_repositories_ready(self) -> None:
        for spec in self._resolve_source_specs():
            stmt = (
                select(KnowledgeDocument.id)
                .where(KnowledgeDocument.source_name == spec.name)
                .limit(1)
            )
            existing = self.db.execute(stmt).scalar_one_or_none()
            if existing is None:
                self.sync_repositories(source_name=spec.name)

    def search_repositories(
        self,
        *,
        query: str,
        top_k: int | None = None,
        source_name: str | None = None,
        language: str | None = None,
    ) -> KnowledgeSearchResult:
        self.ensure_repositories_ready()
        source_specs = self._resolve_source_specs()
        route = self._route_query(query=query, source_specs=source_specs)

        if source_name:
            selected_sources = [spec for spec in source_specs if spec.name == source_name]
        elif route.source_candidates:
            selected_sources = [spec for spec in source_specs if spec.name in route.source_candidates]
            if not selected_sources:
                selected_sources = source_specs
        else:
            selected_sources = source_specs

        top_k = max(1, top_k or self.settings.knowledge_top_k)
        source_names = [spec.name for spec in selected_sources]
        documents_stmt = select(KnowledgeDocument).where(KnowledgeDocument.source_name.in_(source_names))
        documents = list(self.db.scalars(documents_stmt))

        query_tokens = _tokenize(query)
        expanded_tokens = _expand_tokens(query_tokens)

        # Trace fields: capture retrieval/synthesis configuration as the
        # search runs so the AnswerTrace at the end can record exactly
        # what knobs produced this answer. Lets benchmark runs group by
        # config dimension and attribute score deltas to specific knobs.
        query_rewrite_enabled_setting = bool(
            getattr(self.settings, "knowledge_query_rewrite_enabled", False)
        )
        query_rewrite_added_tokens_count: int | None = None
        actual_rerank_pool_size: int | None = None

        # Query rewrite: ask LLM for additional likely-source tokens (e.g.
        # CamelCase identifiers, synonyms, adjacent concepts) to lift recall
        # for natural-language phrases. Fails safe to empty set.
        if query_rewrite_enabled_setting:
            from app.services.query_rewrite import expand_query_tokens
            llm_tokens = expand_query_tokens(
                query=query,
                settings=self.settings,
                existing_tokens=set(query_tokens) | expanded_tokens,
            )
            query_rewrite_added_tokens_count = len(llm_tokens)
            if llm_tokens:
                expanded_tokens = expanded_tokens | llm_tokens

        scored_documents: list[ScoredDocument] = []
        for document in documents:
            scored = self._score_document(
                document=document,
                query=query,
                query_tokens=query_tokens,
                expanded_tokens=expanded_tokens,
                route=route,
            )
            if scored.score <= 0:
                continue
            scored_documents.append(scored)

        scored_documents.sort(key=lambda item: item.score, reverse=True)

        # Semantic rerank: take a larger pool of keyword-top candidates and
        # ask an LLM to reorder by true relevance, then slice to top_k.
        # Fails safe — if reranker is disabled, no MiniMax key, or LLM call
        # fails, the original keyword order is preserved.
        rerank_enabled = bool(getattr(self.settings, "knowledge_rerank_enabled", False))
        pool_size = max(
            top_k,
            int(getattr(self.settings, "knowledge_rerank_pool_size", top_k)),
        )
        if rerank_enabled and len(scored_documents) > top_k:
            from app.services.knowledge_rerank import RerankCandidate, rerank_candidates

            pool = scored_documents[:pool_size]
            actual_rerank_pool_size = len(pool)
            snippet_cap = int(getattr(self.settings, "knowledge_rerank_snippet_chars", 600))
            rerank_input = [
                RerankCandidate(
                    candidate_id=idx,
                    relative_path=scored.document.relative_path,
                    source_name=scored.document.source_name,
                    snippet=(scored.document.content or "")[:snippet_cap],
                )
                for idx, scored in enumerate(pool)
            ]
            ranked_ids = rerank_candidates(
                query=query,
                candidates=rerank_input,
                settings=self.settings,
            )
            # Reorder pool by ranked_ids; entries not in ranked_ids stay in
            # original order at the tail (rerank_candidates already handles
            # that, but be defensive).
            id_to_scored = {idx: scored for idx, scored in enumerate(pool)}
            ranked_pool = [id_to_scored[i] for i in ranked_ids if i in id_to_scored]
            if len(ranked_pool) < len(pool):
                seen = set(ranked_ids)
                ranked_pool.extend(
                    id_to_scored[i] for i in range(len(pool)) if i not in seen
                )
            scored_documents = ranked_pool + scored_documents[pool_size:]

        selected = scored_documents[:top_k]
        citations = [
            self._build_citation(scored=scored, query_tokens=query_tokens, settings=self.settings)
            for scored in selected
        ]

        matched_tokens = sorted({token for scored in selected for token in scored.matched_tokens})
        token_coverage = (
            round(len(matched_tokens) / max(len(set(query_tokens)), 1), 2) if query_tokens else 0.0
        )
        top_score = round(selected[0].score, 2) if selected else 0.0
        citation_count = len(citations)
        hallucination_risk, rationale = self._assess_risk(
            citation_count=citation_count,
            token_coverage=token_coverage,
            top_score=top_score,
        )

        selected_paths = [citation.relative_path for citation in citations]
        selected_source_names = list(dict.fromkeys(citation.source_name for citation in citations))
        primary_source_name = selected_source_names[0] if len(selected_source_names) == 1 else "multiple"
        primary_source_path = next(
            (str(spec.path) for spec in selected_sources if spec.name == selected_source_names[0]),
            str(selected_sources[0].path) if selected_sources else "",
        ) if selected_source_names else (str(selected_sources[0].path) if selected_sources else "")

        answer, answer_provider = self._synthesize_or_template(
            query=query,
            citations=citations,
            hallucination_risk=hallucination_risk,
            route_kind=route.kind,
            language=language,
        )
        packaged_context = "\n\n".join(
            [
                f"[{citation.source_name}:{citation.relative_path}:{citation.line_start}-{citation.line_end}]\n{citation.snippet}"
                for citation in citations
            ]
        )
        if not packaged_context:
            packaged_context = "No repository citations matched this query."

        # Synthesis trace fields. None when the template fallback was used,
        # populated when MiniMax actually synthesised the answer text.
        synth_was_used = answer_provider != "template"
        synthesis_max_snippet = (
            int(getattr(self.settings, "knowledge_synthesis_max_snippet_chars", 0))
            if synth_was_used else None
        ) or None
        synthesis_model_used = (
            str(getattr(self.settings, "knowledge_synthesis_model", "") or "") or None
        ) if synth_was_used else None
        synthesis_prompt_v = None
        if synth_was_used:
            try:
                from app.services.knowledge_synthesis import SYNTHESIS_PROMPT_VERSION
                synthesis_prompt_v = SYNTHESIS_PROMPT_VERSION
            except Exception:  # noqa: BLE001
                synthesis_prompt_v = "unknown"

        return KnowledgeSearchResult(
            query=query,
            answer=answer,
            citations=citations,
            answer_trace=KnowledgeAnswerTrace(
                source_name=primary_source_name,
                source_path=primary_source_path,
                selected_sources=selected_source_names,
                strategy="repository_semantic_retrieval",
                route_kind=route.kind,
                route_reason=route.reason,
                top_k=top_k,
                indexed_document_count=len(documents),
                selected_paths=selected_paths,
                matched_tokens=matched_tokens,
                token_coverage=token_coverage,
                top_score=top_score,
                citation_count=citation_count,
                hallucination_risk=hallucination_risk,
                rationale=rationale,
                answer_provider=answer_provider,
                rerank_enabled=rerank_enabled,
                rerank_pool_size=actual_rerank_pool_size,
                query_rewrite_enabled=query_rewrite_enabled_setting,
                query_rewrite_added_tokens=query_rewrite_added_tokens_count,
                synthesis_max_snippet_chars=synthesis_max_snippet,
                synthesis_prompt_version=synthesis_prompt_v,
                synthesis_model=synthesis_model_used,
            ),
            packaged_context=packaged_context,
        )

    def list_documents(self, *, limit: int = 100, source_name: str | None = None) -> list[KnowledgeDocument]:
        stmt = select(KnowledgeDocument)
        if source_name:
            stmt = stmt.where(KnowledgeDocument.source_name == source_name)
        stmt = stmt.order_by(KnowledgeDocument.source_name.asc(), KnowledgeDocument.relative_path.asc()).limit(limit)
        return list(self.db.scalars(stmt))

    def list_sources(self) -> list[KnowledgeSourceDescriptor]:
        configured_sources = {spec.name: str(spec.path) for spec in self._resolve_source_specs()}
        counts_stmt = (
            select(KnowledgeDocument.source_name, func.count(KnowledgeDocument.id))
            .group_by(KnowledgeDocument.source_name)
        )
        counts = {row[0]: int(row[1]) for row in self.db.execute(counts_stmt)}

        return [
            KnowledgeSourceDescriptor(
                source_name=name,
                source_path=path,
                indexed_document_count=counts.get(name, 0),
            )
            for name, path in configured_sources.items()
        ]

    def _sync_single_repository(self, spec: SourceSpec) -> tuple[int, int, int]:
        existing_stmt = select(KnowledgeDocument).where(KnowledgeDocument.source_name == spec.name)
        existing_documents = {document.relative_path: document for document in self.db.scalars(existing_stmt)}

        seen_paths: set[str] = set()
        updated_documents = 0
        indexed_documents = 0

        for file_path in self._iter_source_files(spec.path):
            raw_bytes = file_path.read_bytes()
            content = _decode_indexable_content(raw_bytes)
            if content is None:
                continue

            relative_path = file_path.relative_to(spec.path).as_posix()
            seen_paths.add(relative_path)

            content_hash = hashlib.sha256(raw_bytes).hexdigest()
            line_count = len(content.splitlines()) if content else 0
            extension = file_path.suffix.lower()
            metadata = {
                "language": _language_from_extension(extension),
                "source_path": str(spec.path),
                "file_name": file_path.name,
            }

            existing = existing_documents.get(relative_path)
            if existing is None:
                self.db.add(
                    KnowledgeDocument(
                        source_name=spec.name,
                        relative_path=relative_path,
                        title=file_path.name,
                        extension=extension,
                        language=metadata["language"],
                        size_bytes=len(raw_bytes),
                        line_count=line_count,
                        content_hash=content_hash,
                        metadata_json=metadata,
                        content=content,
                    )
                )
                indexed_documents += 1
                continue

            if existing.content_hash != content_hash:
                existing.title = file_path.name
                existing.extension = extension
                existing.language = metadata["language"]
                existing.size_bytes = len(raw_bytes)
                existing.line_count = line_count
                existing.content_hash = content_hash
                existing.metadata_json = metadata
                existing.content = content
                updated_documents += 1

        stale_paths = set(existing_documents) - seen_paths
        removed_documents = 0
        if stale_paths:
            removed_documents = len(stale_paths)
            self.db.execute(
                delete(KnowledgeDocument).where(
                    KnowledgeDocument.source_name == spec.name,
                    KnowledgeDocument.relative_path.in_(stale_paths),
                )
            )

        return indexed_documents, updated_documents, removed_documents

    def _resolve_source_specs(self) -> list[SourceSpec]:
        specs: list[SourceSpec] = []
        seen_names: set[str] = set()
        if self.settings.knowledge_source_specs:
            for raw_item in self.settings.knowledge_source_specs.split(";"):
                item = raw_item.strip()
                if not item or "=" not in item:
                    continue
                name, raw_path = item.split("=", 1)
                path = Path(raw_path.strip())
                if path.exists():
                    normalized = name.strip().lower()
                    specs.append(SourceSpec(name=normalized, path=path))
                    seen_names.add(normalized)

        if not specs:
            if self.settings.knowledge_source_path:
                path = Path(self.settings.knowledge_source_path)
                if path.exists():
                    normalized = self.settings.knowledge_source_name.strip().lower()
                    specs.append(SourceSpec(name=normalized, path=path))
                    seen_names.add(normalized)

        upload_root = Path(self.settings.knowledge_upload_root)
        if upload_root.exists():
            for child in sorted(upload_root.iterdir()):
                if not child.is_dir():
                    continue
                name = child.name.lower()
                if name in seen_names:
                    continue
                if not SOURCE_NAME_PATTERN.match(name):
                    continue
                specs.append(SourceSpec(name=name, path=child))
                seen_names.add(name)

        if not specs:
            raise ValueError("No knowledge source path is configured")

        return specs

    def _is_upload_source(self, source_name: str) -> bool:
        upload_root = Path(self.settings.knowledge_upload_root)
        candidate = upload_root / source_name
        return candidate.exists() and candidate.is_dir()

    def upload_documents(
        self,
        *,
        files: list[tuple[str, bytes]],
        source_name: str | None = None,
    ) -> KnowledgeUploadResponse:
        normalized_source = (source_name or self.settings.knowledge_upload_default_source).strip().lower()
        if not SOURCE_NAME_PATTERN.match(normalized_source):
            raise ValueError(
                "Invalid source name. Use 1-64 lowercase letters, digits, underscores, or hyphens."
            )

        configured_names = {
            spec.name
            for spec in self._collect_configured_specs()
        }
        if normalized_source in configured_names:
            raise ValueError(
                f"Source '{normalized_source}' is a configured repository source and cannot receive uploads."
            )

        upload_root = Path(self.settings.knowledge_upload_root)
        source_dir = upload_root / normalized_source
        source_dir.mkdir(parents=True, exist_ok=True)

        max_bytes = int(self.settings.knowledge_upload_max_bytes)
        indexed: list[KnowledgeDocument] = []
        skipped: list[KnowledgeUploadSkipped] = []

        for raw_name, data in files:
            safe_name = Path(raw_name).name
            if not safe_name:
                skipped.append(KnowledgeUploadSkipped(file_name=raw_name, reason="empty file name"))
                continue
            extension = Path(safe_name).suffix.lower()
            if _is_excluded_resource_file(Path(safe_name), self.settings):
                skipped.append(
                    KnowledgeUploadSkipped(
                        file_name=safe_name,
                        reason=f"excluded non-text extension {extension or '(none)'}",
                    )
                )
                continue
            if extension not in UPLOAD_ACCEPTED_EXTENSIONS:
                skipped.append(
                    KnowledgeUploadSkipped(
                        file_name=safe_name,
                        reason=f"unsupported extension {extension or '(none)'}",
                    )
                )
                continue
            if len(data) == 0:
                skipped.append(KnowledgeUploadSkipped(file_name=safe_name, reason="empty content"))
                continue
            if len(data) > max_bytes:
                skipped.append(
                    KnowledgeUploadSkipped(
                        file_name=safe_name,
                        reason=f"exceeds upload limit ({max_bytes} bytes)",
                    )
                )
                continue

            content = _decode_indexable_content(data)
            if content is None:
                skipped.append(KnowledgeUploadSkipped(file_name=safe_name, reason="binary or non-text content"))
                continue

            destination = source_dir / safe_name
            destination.write_bytes(data)

            content_hash = hashlib.sha256(data).hexdigest()
            line_count = len(content.splitlines()) if content else 0
            metadata = {
                "language": _language_from_extension(extension),
                "source_path": str(source_dir),
                "file_name": safe_name,
                "uploaded": True,
            }

            existing_stmt = select(KnowledgeDocument).where(
                KnowledgeDocument.source_name == normalized_source,
                KnowledgeDocument.relative_path == safe_name,
            )
            existing = self.db.scalars(existing_stmt).first()
            if existing is None:
                document = KnowledgeDocument(
                    source_name=normalized_source,
                    relative_path=safe_name,
                    title=safe_name,
                    extension=extension,
                    language=metadata["language"],
                    size_bytes=len(data),
                    line_count=line_count,
                    content_hash=content_hash,
                    metadata_json=metadata,
                    content=content,
                )
                self.db.add(document)
            else:
                existing.title = safe_name
                existing.extension = extension
                existing.language = metadata["language"]
                existing.size_bytes = len(data)
                existing.line_count = line_count
                existing.content_hash = content_hash
                existing.metadata_json = metadata
                existing.content = content
                document = existing
            indexed.append(document)

        self.db.flush()
        self.db.commit()

        summaries = [KnowledgeDocumentSummary.model_validate(document) for document in indexed]
        return KnowledgeUploadResponse(
            source_name=normalized_source,
            source_path=str(source_dir),
            indexed_documents=summaries,
            skipped=skipped,
        )

    def delete_document(self, *, document_id: str) -> KnowledgeDeleteResponse:
        document = self.db.get(KnowledgeDocument, document_id)
        if document is None:
            raise LookupError(f"Knowledge document not found: {document_id}")

        source_name = document.source_name
        removed_from_disk = False
        if self._is_upload_source(source_name):
            disk_path = Path(self.settings.knowledge_upload_root) / source_name / document.relative_path
            if disk_path.exists() and disk_path.is_file():
                disk_path.unlink()
                removed_from_disk = True

        self.db.delete(document)
        self.db.commit()
        return KnowledgeDeleteResponse(
            source_name=source_name,
            removed_documents=1,
            removed_from_disk=removed_from_disk,
        )

    def delete_source(self, *, source_name: str) -> KnowledgeDeleteResponse:
        normalized = source_name.strip().lower()
        if not SOURCE_NAME_PATTERN.match(normalized):
            raise ValueError("Invalid source name")

        count_stmt = (
            select(func.count(KnowledgeDocument.id))
            .where(KnowledgeDocument.source_name == normalized)
        )
        removed_count = int(self.db.execute(count_stmt).scalar_one() or 0)

        self.db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.source_name == normalized)
        )

        removed_from_disk = False
        if self._is_upload_source(normalized):
            disk_dir = Path(self.settings.knowledge_upload_root) / normalized
            shutil.rmtree(disk_dir, ignore_errors=True)
            removed_from_disk = True

        self.db.commit()
        return KnowledgeDeleteResponse(
            source_name=normalized,
            removed_documents=removed_count,
            removed_from_disk=removed_from_disk,
        )

    def _collect_configured_specs(self) -> list[SourceSpec]:
        specs: list[SourceSpec] = []
        if self.settings.knowledge_source_specs:
            for raw_item in self.settings.knowledge_source_specs.split(";"):
                item = raw_item.strip()
                if not item or "=" not in item:
                    continue
                name, raw_path = item.split("=", 1)
                path = Path(raw_path.strip())
                specs.append(SourceSpec(name=name.strip().lower(), path=path))
        if not specs and self.settings.knowledge_source_path:
            specs.append(
                SourceSpec(
                    name=self.settings.knowledge_source_name.strip().lower(),
                    path=Path(self.settings.knowledge_source_path),
                )
            )
        return specs

    def _iter_source_files(self, source_path: Path):
        for file_path in source_path.rglob("*"):
            if not file_path.is_file():
                continue
            if _is_ignored_path(file_path):
                continue
            if _is_excluded_resource_file(file_path, self.settings):
                continue
            if file_path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            if file_path.stat().st_size > self.settings.knowledge_max_file_bytes:
                continue
            yield file_path

    @staticmethod
    def _route_query(*, query: str, source_specs: list[SourceSpec]) -> QueryRoute:
        lowered = query.lower()
        source_candidates = tuple(spec.name for spec in source_specs if spec.name in lowered)

        if any(keyword in lowered for keyword in ("test", "assert", "junit", "androidtest", "instrumented")):
            return QueryRoute(
                kind="test_failure",
                reason="The query mentions tests or assertions, so test paths and Kotlin/Java files should be prioritized.",
                preferred_extensions=(".kt", ".java"),
                preferred_path_terms=("src/test", "androidTest", "Test", "test"),
                source_candidates=source_candidates,
            )

        if any(keyword in lowered for keyword in ("layout", "xml", "theme", "drawable", "navigation", "fragment")):
            return QueryRoute(
                kind="android_resource_debug",
                reason="The query mentions Android UI or resources, so XML and res paths should be prioritized.",
                preferred_extensions=(".xml",),
                preferred_path_terms=("res/layout", "res/navigation", "res/values", "drawable", "fragment"),
                source_candidates=source_candidates,
            )

        if any(keyword in lowered for keyword in ("gradle", "build", "manifest", "dependency", "config")):
            return QueryRoute(
                kind="build_config",
                reason="The query mentions build or configuration concerns, so Gradle and manifest files should be prioritized.",
                preferred_extensions=(".gradle", ".properties", ".xml", ".json"),
                preferred_path_terms=("gradle", "build.gradle", "settings.gradle", "AndroidManifest", "google-services", "firebase"),
                source_candidates=source_candidates,
            )

        return QueryRoute(
            kind="code_debug",
            reason="The query looks like a code or debug request, so Kotlin and Java application files should be prioritized.",
            preferred_extensions=(".kt", ".java", ".xml"),
            preferred_path_terms=("src/main", "viewmodel", "activity", "fragment", "login", "chat"),
            source_candidates=source_candidates,
        )

    @staticmethod
    def _score_document(
        *,
        document: KnowledgeDocument,
        query: str,
        query_tokens: list[str],
        expanded_tokens: set[str],
        route: QueryRoute,
    ) -> ScoredDocument:
        if not query_tokens:
            return ScoredDocument(document=document, score=0.0, matched_tokens=set())

        path_text = f"{document.relative_path} {document.title}".lower()
        content_sample = document.content[:40_000]
        content_tokens = Counter(_tokenize(content_sample))
        matched_tokens = {token for token in set(query_tokens) if token in path_text or content_tokens.get(token, 0) > 0}
        semantic_hits = sum(min(content_tokens.get(token, 0), 4) for token in expanded_tokens)
        path_hits = sum(1 for token in expanded_tokens if token in path_text)
        phrase_bonus = 8 if query.lower() in content_sample.lower() else 0
        extension_bonus = 4 if document.extension in route.preferred_extensions else 0
        path_term_bonus = sum(3 for term in route.preferred_path_terms if term.lower() in path_text)

        score = float((path_hits * 5) + semantic_hits + phrase_bonus + extension_bonus + path_term_bonus)
        return ScoredDocument(document=document, score=score, matched_tokens=matched_tokens)

    @staticmethod
    def _build_citation(
        *,
        scored: ScoredDocument,
        query_tokens: list[str],
        settings: object | None = None,
    ) -> KnowledgeCitation:
        document = scored.document
        lines = document.content.splitlines() or [document.content]
        best_line_index = 0
        best_line_score = -1

        for index, line in enumerate(lines):
            normalized = line.lower()
            line_score = sum(1 for token in query_tokens if token in normalized)
            if line_score > best_line_score:
                best_line_score = line_score
                best_line_index = index

        settings = settings or get_settings()
        chunk = build_snippet(
            content=document.content,
            extension=document.extension,
            target_line=best_line_index + 1,
            min_lines=int(getattr(settings, "knowledge_chunk_min_lines", 5)),
            max_lines=int(getattr(settings, "knowledge_chunk_max_lines", 150)),
            fallback_radius=int(getattr(settings, "knowledge_chunk_fallback_radius", 10)),
        )

        return KnowledgeCitation(
            document_id=document.id,
            source_name=document.source_name,
            title=document.title,
            relative_path=document.relative_path,
            line_start=chunk.line_start,
            line_end=chunk.line_end,
            snippet=chunk.snippet,
            score=round(scored.score, 2),
            metadata={
                "extension": document.extension,
                "language": document.language,
                "size_bytes": document.size_bytes,
                "line_count": document.line_count,
                "enclosing_symbol": chunk.enclosing_symbol,
                "chunk_kind": chunk.chunk_kind,
                "truncated": chunk.truncated,
            },
        )

    @staticmethod
    def _assess_risk(*, citation_count: int, token_coverage: float, top_score: float) -> tuple[str, str]:
        if citation_count == 0 or token_coverage < 0.25 or top_score < 6:
            return "high", "Repository grounding is weak because citation coverage or relevance is too low."
        if citation_count == 1 or token_coverage < 0.5 or top_score < 12:
            return "medium", "Repository grounding is partial; the answer should be reviewed with caution."
        return "low", "Multiple repository citations were found with strong query-token coverage and relevance."

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        return bool(CJK_PATTERN.search(text))

    @staticmethod
    def _route_label(*, route_kind: str, use_chinese: bool) -> str:
        labels = {
            "test_failure": ("test failure debugging", "测试失败排查"),
            "android_resource_debug": ("Android resource or UI debugging", "Android 资源或界面排查"),
            "build_config": ("build or configuration debugging", "构建或配置排查"),
            "code_debug": ("general code debugging", "通用代码排查"),
        }
        english, chinese = labels.get(route_kind, ("code debugging", "代码排查"))
        return chinese if use_chinese else english

    @staticmethod
    def _format_reference(citation: KnowledgeCitation, *, include_lines: bool) -> str:
        path = f"{citation.source_name}:{citation.relative_path}"
        if include_lines:
            return f"{path} (lines {citation.line_start}-{citation.line_end})"
        return path

    @classmethod
    def _supporting_references(cls, citations: list[KnowledgeCitation]) -> str:
        return ", ".join(cls._format_reference(citation, include_lines=False) for citation in citations[1:3])

    @classmethod
    def _confidence_summary(cls, *, hallucination_risk: str, use_chinese: bool) -> str:
        if use_chinese:
            if hallucination_risk == "low":
                return "当前把握度较高，因为这次检索找到了多处相关代码依据。"
            if hallucination_risk == "medium":
                return "当前判断可以作为排查起点，但证据还不算完整，最好结合日志或复现步骤一起确认。"
            return "当前依据偏弱，这个结论只能作为线索，不能直接当成根因判断。"

        if hallucination_risk == "low":
            return "Confidence is relatively high because multiple repository citations support this lead."
        if hallucination_risk == "medium":
            return "This is a useful lead, but the evidence is still partial and should be checked against logs or the failing flow."
        return "This is only a weak lead and should not be treated as the root cause without more evidence."

    @classmethod
    def _next_steps(
        cls,
        *,
        route_kind: str,
        primary: KnowledgeCitation,
        use_chinese: bool,
    ) -> list[str]:
        primary_reference = cls._format_reference(primary, include_lines=True)

        if use_chinese:
            if route_kind == "test_failure":
                return [
                    f"先确认 {primary_reference} 附近的断言、测试数据和预期结果是否一致。",
                    "再检查对应的业务逻辑入口，看最近改动是否改变了返回值、状态或渲染条件。",
                    "如果有失败日志或堆栈，把类名和方法名补进查询，可以更快缩小范围。",
                ]
            if route_kind == "android_resource_debug":
                return [
                    f"先检查 {primary_reference} 附近引用的布局、资源 ID、theme 或 navigation 配置是否一致。",
                    "再对照相关 Fragment、Activity 或 ViewBinding 调用，确认代码和资源文件引用的是同一套名称。",
                    "如果现场能复现，再结合 logcat 看是否有 inflate、resource not found 或 navigation 错误。",
                ]
            if route_kind == "build_config":
                return [
                    f"先核对 {primary_reference} 附近的 Gradle、manifest 或配置项是否和当前模块一致。",
                    "再确认依赖版本、插件版本和模块声明没有遗漏或冲突。",
                    "如果是构建失败，把报错里的任务名和依赖名补进查询，可以进一步缩小范围。",
                ]
            return [
                f"先从 {primary_reference} 附近复现问题路径，确认这里是不是实际入口。",
                "再向上追调用链，看是 ViewModel、Activity、Fragment 还是 Repository 把异常状态传进来的。",
                "如果你手上有报错日志、接口返回或复现步骤，把这些信息补进查询，答案会更准确。",
            ]

        if route_kind == "test_failure":
            return [
                f"Start by checking whether the assertions, test data, and expected values around {primary_reference} still match the current behavior.",
                "Then inspect the linked production code path to see whether recent changes altered state, return values, or rendering conditions.",
                "If you have the failing log or stack trace, add the class and method names to the next query to narrow the search faster.",
            ]
        if route_kind == "android_resource_debug":
            return [
                f"Start by checking whether the layouts, resource IDs, theme values, or navigation references near {primary_reference} still line up.",
                "Then compare the related Fragment, Activity, or ViewBinding code to confirm both sides reference the same resources.",
                "If you can reproduce the issue, cross-check logcat for inflate failures, missing resources, or navigation errors.",
            ]
        if route_kind == "build_config":
            return [
                f"Start by checking whether the Gradle, manifest, or configuration entries near {primary_reference} match the current module setup.",
                "Then verify dependency versions, plugin versions, and module declarations for omissions or conflicts.",
                "If the build is failing, add the task name and dependency name from the error output to the next query to narrow the scope.",
            ]
        return [
            f"Start by reproducing the issue around {primary_reference} and confirm whether this is the real entry point.",
            "Then trace the caller chain upward to see whether a ViewModel, Activity, Fragment, or Repository is passing the wrong state into it.",
            "If you have logs, API responses, or concrete reproduction steps, add them to the next query to get a more precise answer.",
        ]

    @classmethod
    def _build_answer(
        cls,
        *,
        query: str,
        citations: list[KnowledgeCitation],
        hallucination_risk: str,
        route_kind: str,
        language: str | None = None,
    ) -> str:
        use_chinese = (language == "zh") if language else cls._contains_cjk(query)
        if not citations:
            if use_chinese:
                return (
                    "我暂时没有找到足够强的代码依据，所以现在还不能可靠判断问题点。\n\n"
                    "建议你补充报错日志、失败用例名称、页面流程或相关类名后再查一次，这样答案会更准确。"
                )
            return (
                "I could not find strong enough repository evidence to make a reliable call yet.\n\n"
                "Add the error log, failing test name, user flow, or related class name and run the search again for a more precise answer."
            )

        primary = citations[0]
        primary_reference = cls._format_reference(primary, include_lines=True)
        supporting = cls._supporting_references(citations)
        next_steps = cls._next_steps(
            route_kind=route_kind,
            primary=primary,
            use_chinese=use_chinese,
        )
        route_label = cls._route_label(route_kind=route_kind, use_chinese=use_chinese)
        confidence_summary = cls._confidence_summary(
            hallucination_risk=hallucination_risk,
            use_chinese=use_chinese,
        )

        parts: list[str] = []
        if use_chinese:
            parts.append(
                f"我建议你先看 {primary_reference}。这通常是处理“{route_label}”问题时最先需要确认的位置。"
            )
            parts.append(
                "这样判断的原因是：这个文件和你的问题关键词最匹配，而且在当前检索结果里相关性最高。"
            )
            if supporting:
                parts.append(
                    f"如果第一处没有直接暴露问题，再继续看 {supporting}。它们是这次检索里最相关的辅助文件。"
                )
            parts.append("建议按这个顺序继续排查：\n- " + "\n- ".join(next_steps))
            parts.append(confidence_summary)
            return "\n\n".join(parts)

        parts.append(
            f"I would start with {primary_reference}. That is the most likely place to inspect first for this kind of {route_label} issue."
        )
        parts.append(
            "I am pointing you there because this file matched your query most strongly and ranked highest in the current retrieval results."
        )
        if supporting:
            parts.append(
                f"If the first file does not explain the issue directly, continue with {supporting}. They are the strongest supporting files from this retrieval pass."
            )
        parts.append("I would debug it in this order:\n- " + "\n- ".join(next_steps))
        parts.append(confidence_summary)
        return "\n\n".join(parts)

    def _synthesize_or_template(
        self,
        *,
        query: str,
        citations: list[KnowledgeCitation],
        hallucination_risk: str,
        route_kind: str,
        language: str | None,
    ) -> tuple[str, str]:
        """Return answer text and provider, falling back to the deterministic template."""
        from app.services.knowledge_synthesis import (
            KnowledgeSynthesisError,
            KnowledgeSynthesizer,
        )

        if citations and self.settings.knowledge_synthesis_enabled:
            synthesizer = KnowledgeSynthesizer(self.settings)
            try:
                answer = synthesizer.synthesize(
                    query=query,
                    citations=citations,
                    hallucination_risk=hallucination_risk,
                    route_kind=route_kind,
                    language=language,
                )
                return answer, "minimax"
            except KnowledgeSynthesisError:
                pass

        template_answer = self._build_answer(
            query=query,
            citations=citations,
            hallucination_risk=hallucination_risk,
            route_kind=route_kind,
            language=language,
        )
        return template_answer, "template"
