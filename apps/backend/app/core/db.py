from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event as sa_event, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.base import Base
from app.models.knowledge_card import KnowledgeCard
from app.models.knowledge_document import KnowledgeDocument
from app.models.knowledge_retrieval_cache import KnowledgeRetrievalCache

settings = get_settings()
is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30} if is_sqlite else {},
)

def set_sqlite_pragmas(dbapi_conn) -> None:  # noqa: ANN001
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


if is_sqlite:
    @sa_event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
        set_sqlite_pragmas(dbapi_conn)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_knowledge_fts_table(db: Session) -> None:
    if not is_sqlite:
        return
    if not hasattr(db, "execute"):
        return
    existing = db.execute(
        text("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_document_fts'")
    ).scalar_one_or_none()
    if existing == "knowledge_document_fts":
        columns = {
            row[1]
            for row in db.execute(text("PRAGMA table_info(knowledge_document_fts)")).all()
        }
        if "card_text" not in columns:
            db.execute(text("DROP TABLE IF EXISTS knowledge_document_fts"))
    db.execute(
        text(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_document_fts USING fts5(
                document_id UNINDEXED,
                source_name UNINDEXED,
                relative_path,
                title,
                content,
                card_text,
                tokenize = 'porter unicode61 remove_diacritics 2'
            )
            """
        )
    )


def upsert_knowledge_fts(
    db: Session,
    *,
    document_id: str,
    source_name: str,
    relative_path: str,
    title: str,
    content: str,
    card_text: str | None = None,
) -> None:
    if not is_sqlite:
        return
    db.execute(
        text("DELETE FROM knowledge_document_fts WHERE document_id = :id"),
        {"id": document_id},
    )
    db.execute(
        text(
            """
            INSERT INTO knowledge_document_fts (
                document_id, source_name, relative_path, title, content, card_text
            ) VALUES (
                :id, :src, :rp, :title, :content, :card_text
            )
            """
        ),
        {
            "id": document_id,
            "src": source_name,
            "rp": relative_path,
            "title": title,
            "content": content,
            "card_text": card_text or "",
        },
    )


def backfill_knowledge_fts_if_empty(db: Session) -> int:
    if not is_sqlite:
        return 0
    if not hasattr(db, "execute"):
        return 0
    create_knowledge_fts_table(db)
    raw_count = db.execute(text("SELECT COUNT(*) FROM knowledge_document_fts")).scalar()
    if not isinstance(raw_count, int):
        return 0
    count = int(raw_count or 0)
    if count > 0:
        return 0

    inserted = 0
    for document in db.execute(select(KnowledgeDocument)).scalars():
        card = db.execute(
            select(KnowledgeCard).where(KnowledgeCard.document_id == document.id)
        ).scalar_one_or_none()
        upsert_knowledge_fts(
            db,
            document_id=document.id,
            source_name=document.source_name,
            relative_path=document.relative_path,
            title=document.title,
            content=document.content,
            card_text=card.card_text if card is not None else "",
        )
        inserted += 1
    db.commit()
    return inserted


def ensure_local_schema() -> None:
    if not is_sqlite:
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        llm_usage_table = Base.metadata.tables.get("llm_usage")
        if "llm_usage" not in existing_tables and llm_usage_table is not None:
            llm_usage_table.create(bind=connection, checkfirst=True)

        if "task" in existing_tables:
            task_columns = {column["name"] for column in inspector.get_columns("task")}
            if "session_id" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN session_id VARCHAR(36)"))
            if "actor_name" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN actor_name VARCHAR(100) DEFAULT 'employee'"))
            if "actor_role" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN actor_role VARCHAR(64) DEFAULT 'EMPLOYEE'"))
            if "translation_json" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN translation_json JSON"))
            if "review_json" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN review_json JSON"))
            if "risk_category" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN risk_category VARCHAR(64) DEFAULT 'GENERAL'"))
            if "governance_json" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN governance_json JSON"))
            if "trace_id" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN trace_id VARCHAR(64)"))
            if "latest_checkpoint_json" not in task_columns:
                connection.execute(text("ALTER TABLE task ADD COLUMN latest_checkpoint_json JSON"))
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_task_trace_id ON task (trace_id)"))
            connection.execute(text("UPDATE task SET actor_name = 'employee' WHERE actor_name IS NULL"))
            connection.execute(text("UPDATE task SET actor_role = 'EMPLOYEE' WHERE actor_role IS NULL"))
            connection.execute(text("UPDATE task SET risk_category = 'GENERAL' WHERE risk_category IS NULL"))
            connection.execute(text("UPDATE task SET actor_role = UPPER(actor_role) WHERE actor_role IS NOT NULL"))
            connection.execute(text("UPDATE task SET risk_category = UPPER(risk_category) WHERE risk_category IS NOT NULL"))
            connection.execute(text("UPDATE task SET risk_level = UPPER(risk_level) WHERE risk_level IS NOT NULL"))
            connection.execute(text("UPDATE task SET status = 'CREATED' WHERE status IN ('queued', 'QUEUED')"))
            connection.execute(
                text(
                    "UPDATE task SET status = 'PLANNING' "
                    "WHERE status IN ('running', 'RUNNING') AND workflow_stage IN ('planning', 'PLANNING')"
                )
            )
            connection.execute(
                text(
                    "UPDATE task SET status = 'REVIEWING' "
                    "WHERE status IN ('running', 'RUNNING') AND workflow_stage IN ('review', 'REVIEW')"
                )
            )
            connection.execute(
                text(
                    "UPDATE task SET status = 'EXECUTING' "
                    "WHERE status IN ('running', 'RUNNING') AND workflow_stage IN ('knowledge', 'action', 'KNOWLEDGE', 'ACTION')"
                )
            )
            connection.execute(
                text(
                    "UPDATE task SET status = 'AWAITING_APPROVAL' "
                    "WHERE status IN ('waiting_approval', 'WAITING_APPROVAL')"
                )
            )

        if "event" in existing_tables:
            event_columns_full = inspector.get_columns("event")
            event_columns = {column["name"] for column in event_columns_full}
            if "session_id" not in event_columns:
                connection.execute(text("ALTER TABLE event ADD COLUMN session_id VARCHAR(36)"))
            # Stage 17 (T-LLM-METRICS) lifted NOT NULL on event.task_id so
            # system-level events (LLM_CALL outside a task scope) can write
            # without a task_id. SQLite has no DROP NOT NULL — rebuild the
            # table when the existing column still says NOT NULL. Idempotent
            # on already-migrated DBs (skip when nullable=True).
            task_id_col = next((c for c in event_columns_full if c["name"] == "task_id"), None)
            if task_id_col is not None and not task_id_col.get("nullable", True):
                # Drop user indexes on the old table — they'll be recreated
                # when SQLAlchemy creates the new table from metadata.
                # sqlite_autoindex_* are auto-managed by SQLite (PK), don't drop.
                for idx_row in connection.execute(text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='event' AND name NOT LIKE 'sqlite_%'"
                )).fetchall():
                    connection.execute(text(f"DROP INDEX IF EXISTS {idx_row[0]}"))
                connection.execute(text("ALTER TABLE event RENAME TO event__pre_task_id_nullable"))
                # Recreate the event table with the current SQLAlchemy schema
                # (which now has nullable=True on task_id) — also recreates indexes.
                Base.metadata.tables["event"].create(bind=connection, checkfirst=False)
                # Copy by explicit column list to be schema-stable.
                copy_cols = [c["name"] for c in event_columns_full if c["name"] in event_columns]
                col_csv = ", ".join(copy_cols)
                connection.execute(text(
                    f"INSERT INTO event ({col_csv}) SELECT {col_csv} FROM event__pre_task_id_nullable"
                ))
                connection.execute(text("DROP TABLE event__pre_task_id_nullable"))

        if "approval" in existing_tables:
            approval_columns = {column["name"] for column in inspector.get_columns("approval")}
            if "requested_by_actor_name" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN requested_by_actor_name VARCHAR(100) DEFAULT 'employee'"))
            if "decided_by_actor_name" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN decided_by_actor_name VARCHAR(100)"))
            if "risk_level" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN risk_level VARCHAR(16) DEFAULT 'MEDIUM'"))
            if "risk_category" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN risk_category VARCHAR(64) DEFAULT 'GENERAL'"))
            if "policy_snapshot_json" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN policy_snapshot_json JSON"))
            if "expires_at" not in approval_columns:
                connection.execute(text("ALTER TABLE approval ADD COLUMN expires_at DATETIME"))
            connection.execute(text("UPDATE approval SET requested_by_actor_name = 'employee' WHERE requested_by_actor_name IS NULL"))
            connection.execute(text("UPDATE approval SET risk_level = 'MEDIUM' WHERE risk_level IS NULL"))
            connection.execute(text("UPDATE approval SET risk_category = 'GENERAL' WHERE risk_category IS NULL"))
            connection.execute(text("UPDATE approval SET risk_level = UPPER(risk_level) WHERE risk_level IS NOT NULL"))
            connection.execute(text("UPDATE approval SET risk_category = UPPER(risk_category) WHERE risk_category IS NOT NULL"))

        if "tool_execution" in existing_tables:
            tool_execution_columns = {column["name"] for column in inspector.get_columns("tool_execution")}
            if "inverse_action_json" not in tool_execution_columns:
                connection.execute(text("ALTER TABLE tool_execution ADD COLUMN inverse_action_json JSON"))

        knowledge_retrieval_cache_table = Base.metadata.tables.get("knowledge_retrieval_cache")
        if "knowledge_retrieval_cache" not in existing_tables and knowledge_retrieval_cache_table is not None:
            knowledge_retrieval_cache_table.create(bind=connection, checkfirst=True)

    with SessionLocal() as db:
        backfill_knowledge_fts_if_empty(db)
