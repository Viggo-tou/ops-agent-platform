from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.models  # noqa: F401
from app.api.approvals import router as approvals_router
from app.api.governance import router as governance_router
from app.api.health import router as health_router
from app.api.knowledge import router as knowledge_router
from app.api.tasks import router as tasks_router
from app.api.tools import router as tools_router
from app.core.config import get_settings
from app.core.db import Base, engine, ensure_local_schema
from app.services.governance import bootstrap_governance_data


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_local_schema()
    bootstrap_governance_data()
    yield


settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

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
app.include_router(tools_router, prefix=settings.api_prefix)
