from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import Settings, get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402
from app.models.knowledge_card import KnowledgeCard  # noqa: E402
from app.models.knowledge_document import KnowledgeDocument  # noqa: E402
from app.services.cards import CARD_PROMPT_VERSION, CardGenerator, upsert_card  # noqa: E402


@dataclass(frozen=True)
class BuildCardsSummary:
    generated: int
    skipped: int
    failed: int
    total: int


def _eligible_documents(
    *,
    source_name: str | None,
    skip_existing: bool,
) -> tuple[list[KnowledgeDocument], int]:
    with SessionLocal() as db:
        stmt = select(KnowledgeDocument)
        if source_name:
            stmt = stmt.where(KnowledgeDocument.source_name == source_name)
        documents = list(db.scalars(stmt.order_by(KnowledgeDocument.source_name, KnowledgeDocument.relative_path)))
        cards = {
            card.document_id: card
            for card in db.scalars(select(KnowledgeCard)).all()
        }

    selected: list[KnowledgeDocument] = []
    skipped = 0
    for document in documents:
        existing = cards.get(document.id)
        fresh = (
            existing is not None
            and existing.content_hash == document.content_hash
            and existing.card_version == CARD_PROMPT_VERSION
        )
        if skip_existing and fresh:
            skipped += 1
            continue
        selected.append(document)
    return selected, skipped


def _generate_one(document_id: str, settings: Settings) -> tuple[str, float]:
    start = time.perf_counter()
    with SessionLocal() as db:
        document = db.get(KnowledgeDocument, document_id)
        if document is None:
            raise LookupError(f"Knowledge document not found: {document_id}")
        card_text, model_name = CardGenerator(settings, db=db).generate(document=document)
        upsert_card(db, document=document, card_text=card_text, model_name=model_name)
        db.commit()
    return model_name, time.perf_counter() - start


def build_cards(
    *,
    source_name: str | None = None,
    skip_existing: bool = False,
    settings: Settings | None = None,
    concurrency: int | None = None,
) -> BuildCardsSummary:
    settings = settings or get_settings()
    documents, skipped = _eligible_documents(source_name=source_name, skip_existing=skip_existing)
    total = len(documents)
    if total == 0:
        print(f"Generated 0 cards; skipped {skipped}; failed 0.")
        return BuildCardsSummary(generated=0, skipped=skipped, failed=0, total=0)

    workers = max(1, concurrency or int(getattr(settings, "knowledge_cards_concurrency", 5)))
    generated = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_generate_one, document.id, settings): document
            for document in documents
        }
        for index, future in enumerate(as_completed(futures), start=1):
            document = futures[future]
            try:
                model_name, latency = future.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"[{index}/{total}] doc_id={document.id} path={document.relative_path} failed={exc}")
                continue
            generated += 1
            print(
                f"[{index}/{total}] doc_id={document.id} model={model_name} "
                f"latency={latency:.2f}s path={document.relative_path}"
            )

    print(f"Generated {generated} cards; skipped {skipped}; failed {failed}; estimated cost unavailable.")
    return BuildCardsSummary(generated=generated, skipped=skipped, failed=failed, total=total)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate LLM markdown cards for indexed knowledge documents.")
    parser.add_argument("--source-name", default=None)
    parser.add_argument("--backend-url", default=None, help="Accepted for run-log compatibility; local DB is used.")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--skip-existing", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings_values = {}
    if args.provider:
        settings_values["knowledge_cards_provider"] = args.provider
    if args.model:
        settings_values["knowledge_cards_model"] = args.model
    settings = Settings(**settings_values) if settings_values else get_settings()
    summary = build_cards(
        source_name=args.source_name,
        skip_existing=args.skip_existing,
        settings=settings,
        concurrency=args.concurrency,
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
