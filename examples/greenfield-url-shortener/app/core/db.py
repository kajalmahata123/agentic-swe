"""Database engine/session factories and the request-scoped session dependency."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


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
