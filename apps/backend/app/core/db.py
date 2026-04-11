from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models.base import Base

settings = get_settings()
is_sqlite = settings.database_url.startswith("sqlite")

engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False} if is_sqlite else {},
)

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


def ensure_local_schema() -> None:
    if not is_sqlite:
        return

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as connection:
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
            event_columns = {column["name"] for column in inspector.get_columns("event")}
            if "session_id" not in event_columns:
                connection.execute(text("ALTER TABLE event ADD COLUMN session_id VARCHAR(36)"))

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
