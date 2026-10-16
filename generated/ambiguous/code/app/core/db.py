"""Database engine/session factories, engine tuning and the request-scoped session dependency.

File-based SQLite is hardened for concurrent readers and writers: every new connection
switches to WAL journal mode with ``synchronous=NORMAL``, and the busy timeout is set via
the sqlite3 ``timeout`` connect argument (no SQL is built with formatting). In-memory
SQLite uses a ``StaticPool`` (one connection shared by all threads, so every request
sees the same database), ignores the pool sizing settings and gets no pragmas.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from fastapi import Request
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

logger = logging.getLogger("app.db")


def is_memory_sqlite(url: URL) -> bool:
    """Return True when ``url`` points at an in-memory SQLite database."""
    if url.get_backend_name() != "sqlite":
        return False
    database = url.database
    return (
        database in (None, "", ":memory:")
        or (database is not None and database.startswith("file::memory:"))
        or url.query.get("mode") == "memory"
    )


def create_db_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 10,
    pool_timeout: float = 30.0,
    sqlite_busy_timeout_ms: int = 5000,
) -> Engine:
    """Create a SQLAlchemy engine for ``database_url``.

    SQLite connections may be used from a different thread than the one that created
    them (FastAPI runs sync endpoints in a thread pool), so ``check_same_thread`` is
    disabled. In-memory SQLite uses a ``StaticPool`` and ignores the pool settings. File-based SQLite uses a
    ``QueuePool`` with the given sizing, the busy timeout and WAL pragmas. Other backends
    get ``pool_pre_ping`` to survive dropped connections plus the pool sizing.
    """
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if is_memory_sqlite(url):
            # Each pysqlite connection to ':memory:' is a separate, empty database, and
            # SQLAlchemy's default pool for it hands every thread its own connection.
            # FastAPI runs sync endpoints in worker threads, so the tables created at
            # startup would be invisible to requests. StaticPool shares the single
            # connection (and therefore the single database) across all threads. The
            # pool sizing settings do not apply to it.
            return create_engine(
                database_url,
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        engine = create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            connect_args={
                "check_same_thread": False,
                # pysqlite sets busy_timeout to int(timeout * 1000); the +0.5 ms keeps
                # float truncation from landing one millisecond below the configured value.
                "timeout": (sqlite_busy_timeout_ms + 0.5) / 1000.0,
            },
        )

        @event.listens_for(engine, "connect")
        def _sqlite_on_connect(dbapi_connection: Any, _record: Any) -> None:
            """Enable WAL and synchronous=NORMAL on every new DBAPI connection."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

        return engine
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
    )


def describe_engine(engine: Engine) -> dict[str, object]:
    """Return the effective engine tuning (never the URL or credentials)."""
    pool = engine.pool
    info: dict[str, object] = {
        "dialect": engine.dialect.name,
        "pool_class": type(pool).__name__,
        "pool_size": None,
        "max_overflow": None,
        "pool_timeout": None,
    }
    if isinstance(pool, QueuePool):
        info["pool_size"] = pool.size()
        info["max_overflow"] = pool._max_overflow
        info["pool_timeout"] = pool.timeout()
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            info["journal_mode"] = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
            info["synchronous"] = conn.exec_driver_sql("PRAGMA synchronous").scalar()
            info["busy_timeout_ms"] = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    return info


def log_engine_tuning(engine: Engine) -> dict[str, object]:
    """Log one structured line with the effective engine tuning and return it."""
    info = describe_engine(engine)
    logger.info(
        "database tuning " + " ".join(f"{key}={value}" for key, value in info.items()),
        extra=info,
    )
    return info


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory bound to ``engine``."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)


def get_db(request: Request) -> Iterator[Session]:
    """Yield a session from the app's session factory and always close it.

    This function is the single override point for tests
    (``app.dependency_overrides[get_db]``) and must be used by every router.
    """
    session: Session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()
