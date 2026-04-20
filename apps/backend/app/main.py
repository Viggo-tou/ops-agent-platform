from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401
from app.api.approvals import router as approvals_router
from app.api.governance import router as governance_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.memory import router as memory_router
from app.api.metrics import router as metrics_router
from app.api.model_config import router as model_config_router
from app.api.tasks import router as tasks_router
from app.api.tools import router as tools_router
from app.core.config import get_settings
from app.core.db import Base, SessionLocal, engine, ensure_local_schema
from app.core.logging import configure_logging
from app.core.middleware import RequestLoggingMiddleware
from app.core.pipeline_executor import init_pipeline_executor, shutdown_pipeline_executor
from app.core.telemetry import configure_telemetry
from app.services.governance import bootstrap_governance_data
from app.services.model_config import bootstrap_model_catalog

configure_logging()
configure_telemetry()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_local_schema()
    bootstrap_governance_data()
    with SessionLocal() as db:
        bootstrap_model_catalog(db)
    init_pipeline_executor(settings.pipeline_max_workers)
    try:
        yield
    finally:
        shutdown_pipeline_executor(wait=True)


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.add_middleware(RequestLoggingMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(tasks_router, prefix=settings.api_prefix)
app.include_router(approvals_router, prefix=settings.api_prefix)
app.include_router(governance_router, prefix=settings.api_prefix)
app.include_router(knowledge_router, prefix=settings.api_prefix)
app.include_router(memory_router, prefix=settings.api_prefix)
app.include_router(metrics_router)
app.include_router(model_config_router, prefix=settings.api_prefix)
app.include_router(tools_router, prefix=settings.api_prefix)
