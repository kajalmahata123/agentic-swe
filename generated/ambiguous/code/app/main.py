"""Application factory for the URL shortener.

Run with ``uvicorn app.main:app --workers 1 --no-access-log``:

- On SQLite run exactly one uvicorn worker (one process); multiple workers or
  replicas require a server database such as PostgreSQL.
- uvicorn's access log prints client IP addresses, which must never be logged. The
  app's own request log (``app.request``) records only method, path, status and
  duration.

At startup the lifespan creates missing tables, then runs the idempotent schema
upgrade (adds ``links.expires_at`` to databases created before link expiry existed;
a failure aborts startup), then logs one line with the effective database tuning.

Set ``API_KEY`` (at least 16 characters) to enable the ``/api/links`` management API;
clients send it in the ``X-API-Key`` header. Redirects and probes stay public.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import health, links, redirect
from app.core.config import Settings
from app.core.db import create_db_engine, create_session_factory, log_engine_tuning
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestLoggingMiddleware
from app.core.schema import ensure_links_expires_at
from app.models import Base


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fully wired FastAPI application.

    Each call creates its own settings, engine and session factory (stored on
    ``app.state``), so separate instances, e.g. per test, share no state. At startup
    tables are created, the schema upgrade runs and the engine tuning is logged; the
    engine is disposed on shutdown.
    """
    settings = settings if settings is not None else Settings()
    configure_logging(settings.log_level)
    if settings.api_key is None:
        logging.getLogger("app.security").warning(
            "API_KEY is not configured: all /api/links requests will be rejected with 401"
        )

    engine = create_db_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        sqlite_busy_timeout_ms=settings.sqlite_busy_timeout_ms,
    )
    session_factory = create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        Base.metadata.create_all(bind=engine)
        # Exceptions propagate so startup fails instead of serving 503 on every redirect.
        ensure_links_expires_at(engine)
        log_engine_tuning(engine)
        yield
        engine.dispose()

    app = FastAPI(title="URL Shortener", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory

    register_exception_handlers(app)
    app.add_middleware(RequestLoggingMiddleware)

    # Order matters: the catch-all GET/HEAD /{code} router must be registered last.
    app.include_router(health.router)
    app.include_router(links.router)
    app.include_router(redirect.router)

    return app


app = create_app()
