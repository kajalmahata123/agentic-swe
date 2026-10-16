"""Database engine/session factories, the startup schema migration and the
request-scoped session dependency."""

from __future__ import annotations

import logging
from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger("app.migrations")


def create_db_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy engine for ``database_url``.

    SQLite connections may be used from a different thread than the one that created
    them (FastAPI runs sync endpoints in a thread pool), so ``check_same_thread`` is
    disabled. Other backends get ``pool_pre_ping`` to survive dropped connections.
    """
    if database_url.startswith("sqlite"):
        return create_engine(database_url, connect_args={"check_same_thread": False})
    return create_engine(database_url, pool_pre_ping=True)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build the session factory bound to ``engine``."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)


def _links_columns(engine: Engine) -> set[str]:
    """Return the column names of the ``links`` table.

    A fresh inspector is created on every call so that schema changes made in between
    (by this process or a concurrent instance) are always observed.
    """
    return {column["name"] for column in inspect(engine).get_columns("links")}


def ensure_schema(engine: Engine) -> bool:
    """Bring an existing database up to the current schema; return True if changed.

    ``Base.metadata.create_all`` creates missing tables but never alters existing ones.
    Databases created before CR-142 therefore lack ``links.expires_at``. This step is
    idempotent and safe to run on every startup:

    * no ``links`` table, or the column already exists: no-op (returns False);
    * column missing: adds it as a nullable column, so existing rows get NULL and never
      expire (returns True, logs one INFO line);
    * the ALTER fails but the column now exists (a concurrent instance won the race):
      tolerated (returns False);
    * any other failure is logged and re-raised so startup fails loudly.

    Equivalent manual SQL for DBAs::

        -- SQLite / MySQL
        ALTER TABLE links ADD COLUMN expires_at DATETIME NULL;
        -- PostgreSQL (has no DATETIME type)
        ALTER TABLE links ADD COLUMN expires_at TIMESTAMP NULL;
    """
    if not inspect(engine).has_table("links"):
        logger.debug("schema migration skipped: table links does not exist")
        return False
    if "expires_at" in _links_columns(engine):
        logger.debug("schema up to date")
        return False

    try:
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE links ADD COLUMN expires_at TIMESTAMP NULL"))
            else:
                conn.execute(text("ALTER TABLE links ADD COLUMN expires_at DATETIME NULL"))
    except DBAPIError:
        if "expires_at" in _links_columns(engine):
            logger.info("links.expires_at already added by another instance")
            return False
        logger.error("schema migration failed: could not add links.expires_at")
        raise

    logger.info("schema migration applied: added column links.expires_at")
    return True


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
