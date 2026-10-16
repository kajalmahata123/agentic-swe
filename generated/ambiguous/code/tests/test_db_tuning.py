"""Tests for SQLite hardening (FR-15), pool configuration (FR-16) and the startup
tuning log line (NFR-16, NFR-15)."""

from __future__ import annotations

import ast
import logging
import os
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy
from app.core import db as db_module
from app.core.config import Settings
from app.core.db import create_db_engine, describe_engine, is_memory_sqlite, log_engine_tuning
from app.main import create_app
from fastapi.testclient import TestClient
from helpers import AUTH_HEADERS, TEST_API_KEY
from pydantic import ValidationError
from sqlalchemy.engine import make_url
from sqlalchemy.pool import QueuePool

APP_DIR = Path(__file__).resolve().parent.parent / "app"
NEW_ENV_VARS = (
    "SQLITE_BUSY_TIMEOUT_MS",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
)


def _pragmas(engine) -> tuple[str, int, int]:
    with engine.connect() as conn:
        return (
            conn.exec_driver_sql("PRAGMA journal_mode").scalar(),
            conn.exec_driver_sql("PRAGMA synchronous").scalar(),
            conn.exec_driver_sql("PRAGMA busy_timeout").scalar(),
        )


# --------------------------------------------------------------------------- settings


def test_conftest_strips_new_env_vars():
    """Covers: FR-16, FR-15"""
    for var in NEW_ENV_VARS:
        assert var not in os.environ, var


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("DB_POOL_SIZE", "0"),
        ("DB_MAX_OVERFLOW", "-1"),
        ("DB_POOL_TIMEOUT_SECONDS", "0"),
        ("DB_POOL_TIMEOUT_SECONDS", "300.5"),
        ("SQLITE_BUSY_TIMEOUT_MS", "-1"),
        ("SQLITE_BUSY_TIMEOUT_MS", "60001"),
        ("SQLITE_BUSY_TIMEOUT_MS", "abc"),
    ],
)
def test_invalid_db_env_prevents_app_start(monkeypatch, tmp_path, var, value):
    """Covers: FR-15, FR-16"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/x.db")
    monkeypatch.setenv(var, value)
    with pytest.raises(ValidationError) as excinfo:
        create_app()
    assert var.lower() in str(excinfo.value).lower()


def test_boundary_values_accepted():
    """Covers: FR-15, FR-16"""
    s = Settings(
        sqlite_busy_timeout_ms=0, db_pool_size=1, db_max_overflow=0, db_pool_timeout_seconds=300
    )
    assert s.sqlite_busy_timeout_ms == 0
    assert Settings(sqlite_busy_timeout_ms=60000).sqlite_busy_timeout_ms == 60000
    assert Settings(db_pool_timeout_seconds=0.001).db_pool_timeout_seconds == 0.001


# --------------------------------------------------------------------------- pragmas


@pytest.mark.parametrize("busy_ms", [0, 200, 1000, 5000, 60000])
def test_file_sqlite_connections_use_wal_normal_and_busy_timeout(tmp_path, busy_ms):
    """Covers: FR-15"""
    engine = create_db_engine(f"sqlite:///{tmp_path}/p.db", sqlite_busy_timeout_ms=busy_ms)
    try:
        assert _pragmas(engine) == ("wal", 1, busy_ms)
        # Every new connection, not just the first one.
        c1 = engine.connect()
        c2 = engine.connect()
        try:
            for c in (c1, c2):
                assert c.exec_driver_sql("PRAGMA synchronous").scalar() == 1
                assert c.exec_driver_sql("PRAGMA busy_timeout").scalar() == busy_ms
                assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        finally:
            c1.close()
            c2.close()
    finally:
        engine.dispose()


def test_app_engine_reports_pragmas_from_settings(make_client):
    """Covers: FR-15"""
    client = make_client(sqlite_busy_timeout_ms=1234)
    assert _pragmas(client.app.state.engine) == ("wal", 1, 1234)


def test_default_busy_timeout_is_5000(client):
    """Covers: FR-15"""
    assert _pragmas(client.app.state.engine)[2] == 5000


def test_sqlite_engine_kwargs_keep_check_same_thread_and_timeout(monkeypatch, tmp_path):
    """Covers: FR-15, FR-16"""
    captured: list[tuple[str, dict]] = []
    real = sqlalchemy.create_engine

    def spy(url, **kwargs):
        captured.append((url, kwargs))
        return real(url, **kwargs)

    monkeypatch.setattr(db_module, "create_engine", spy)
    engine = create_db_engine(
        f"sqlite:///{tmp_path}/k.db",
        pool_size=3,
        max_overflow=4,
        pool_timeout=7.5,
        sqlite_busy_timeout_ms=250,
    )
    engine.dispose()
    (_, kwargs), = captured
    assert kwargs["connect_args"]["check_same_thread"] is False
    assert int(kwargs["connect_args"]["timeout"] * 1000) == 250
    assert kwargs["poolclass"] is QueuePool
    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_timeout"] == 7.5
    assert "pool_pre_ping" not in kwargs


def test_non_sqlite_engine_gets_pre_ping_and_pool_but_no_sqlite_pragmas(monkeypatch):
    """Covers: FR-15, FR-16"""
    captured: list[tuple[str, dict]] = []
    sentinel = object()

    def fake_create_engine(url, **kwargs):
        captured.append((url, kwargs))
        return sentinel

    def no_listeners(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("no SQLite connect listener for non-SQLite engines")

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)
    monkeypatch.setattr(db_module.event, "listens_for", no_listeners)
    url = "postgresql+psycopg://user:pw@db.example:5432/shortener"
    result = create_db_engine(
        url, pool_size=9, max_overflow=2, pool_timeout=12.0, sqlite_busy_timeout_ms=200
    )
    assert result is sentinel
    (got_url, kwargs), = captured
    assert got_url == url
    assert kwargs == {
        "pool_pre_ping": True,
        "pool_size": 9,
        "max_overflow": 2,
        "pool_timeout": 12.0,
    }


# --------------------------------------------------------------------------- pool


def test_file_sqlite_pool_matches_settings(make_client):
    """Covers: FR-16"""
    client = make_client(db_pool_size=3, db_max_overflow=7, db_pool_timeout_seconds=4.5)
    pool = client.app.state.engine.pool
    assert isinstance(pool, QueuePool)
    assert pool.size() == 3
    assert pool._max_overflow == 7
    assert pool.timeout() == 4.5


def test_default_pool_matches_sqlalchemy_defaults(client):
    """Covers: FR-16"""
    pool = client.app.state.engine.pool
    assert pool.size() == 5
    assert pool._max_overflow == 10
    assert pool.timeout() == 30.0


@pytest.mark.parametrize(
    "url", ["sqlite://", "sqlite:///:memory:", "sqlite:///file::memory:?cache=shared&uri=true"]
)
def test_memory_sqlite_detection(url):
    """Covers: FR-15, FR-16"""
    assert is_memory_sqlite(make_url(url)) is True


@pytest.mark.parametrize("url", ["sqlite:///./x.db", "sqlite:////tmp/x.db", "postgresql://h/db"])
def test_file_and_server_urls_not_memory(url):
    """Covers: FR-15, FR-16"""
    assert is_memory_sqlite(make_url(url)) is False


def _memory_app(url: str):
    return create_app(
        Settings(
            database_url=url,
            api_key=TEST_API_KEY,
            db_pool_size=1,
            db_max_overflow=0,
            db_pool_timeout_seconds=1,
            sqlite_busy_timeout_ms=100,
        )
    )


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_in_memory_sqlite_ignores_pool_settings_and_starts(url):
    """Covers: FR-15, FR-16"""
    app = _memory_app(url)
    assert not isinstance(app.state.engine.pool, QueuePool)
    with TestClient(app, headers=AUTH_HEADERS) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 200
        assert client.head("/readyz").status_code == 200
        with app.state.engine.connect() as conn:
            assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "memory"


@pytest.mark.parametrize("url", ["sqlite://", "sqlite:///:memory:"])
def test_in_memory_sqlite_serves_link_requests_without_error(url):
    """Covers: FR-15, FR-16

    FR-15: "For in-memory SQLite URLs ('sqlite://' and ':memory:'), the app starts and
    serves requests without error."
    """
    app = _memory_app(url)
    with TestClient(app, headers=AUTH_HEADERS) as client:
        created = client.post("/api/links", json={"url": "https://example.com/mem"})
        assert created.status_code == 201, created.text
        code = created.json()["code"]
        resp = client.get(f"/{code}", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "https://example.com/mem"
        assert client.get(f"/api/links/{code}").json()["total_clicks"] == 1


def test_engine_and_settings_created_per_app(tmp_path):
    """Covers: FR-16"""
    a = create_app(Settings(database_url=f"sqlite:///{tmp_path}/a.db", db_pool_size=2))
    b = create_app(Settings(database_url=f"sqlite:///{tmp_path}/b.db", db_pool_size=6))
    assert a.state.engine is not b.state.engine
    assert a.state.engine.pool.size() == 2
    assert b.state.engine.pool.size() == 6
    assert a.state.settings is not b.state.settings
    a.state.engine.dispose()
    b.state.engine.dispose()


def test_no_module_level_engine_settings_or_pool():
    """Covers: FR-16, NFR-17"""
    forbidden_calls = {"create_db_engine", "create_engine", "Settings", "QueuePool", "sessionmaker"}
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in tree.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                continue
            value = stmt.value
            if isinstance(value, ast.Call):
                name = getattr(value.func, "id", None) or getattr(value.func, "attr", None)
                assert name not in forbidden_calls, (path, stmt.lineno)
    for module in (db_module,):
        for value in vars(module).values():
            assert not isinstance(value, sqlalchemy.Engine)
            assert not isinstance(value, Settings)


# --------------------------------------------------------------------------- startup log


def _tuning_records(caplog):
    return [
        r for r in caplog.records
        if r.name == "app.db" and r.getMessage().startswith("database tuning")
    ]


def test_startup_logs_one_tuning_line_and_one_schema_line(tmp_path, caplog):
    """Covers: NFR-16, FR-15, FR-16, NFR-15"""
    db_url = f"sqlite:///{tmp_path}/tuning-secretpath.db"
    app = create_app(
        Settings(
            database_url=db_url,
            api_key=TEST_API_KEY,
            sqlite_busy_timeout_ms=1000,
            db_pool_size=4,
            db_max_overflow=6,
            db_pool_timeout_seconds=9,
        )
    )
    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(app) as client:
            assert client.get("/healthz").status_code == 200
    (rec,) = _tuning_records(caplog)
    assert rec.levelno == logging.INFO
    assert rec.dialect == "sqlite"
    assert rec.pool_class == "QueuePool"
    assert rec.pool_size == 4
    assert rec.max_overflow == 6
    assert rec.pool_timeout == 9.0
    assert rec.journal_mode == "wal"
    assert rec.synchronous == 1
    assert rec.busy_timeout_ms == 1000
    msg = rec.getMessage()
    for fragment in (
        "dialect=sqlite",
        "pool_size=4",
        "max_overflow=6",
        "pool_timeout=9.0",
        "journal_mode=wal",
        "synchronous=1",
        "busy_timeout_ms=1000",
    ):
        assert fragment in msg
    schema_lines = [r for r in caplog.records if hasattr(r, "schema_upgrade")]
    assert len(schema_lines) == 1
    assert schema_lines[0].schema_upgrade == "already_present"
    startup = [r for r in caplog.records if r.name in ("app.db", "app.security")]
    for r in startup:
        dumped = r.getMessage() + str(r.__dict__)
        assert TEST_API_KEY not in dumped
        assert "tuning-secretpath" not in dumped
        assert db_url not in dumped


def test_startup_log_never_contains_database_password_or_api_key(monkeypatch, tmp_path, caplog):
    """Covers: NFR-15, NFR-16"""
    # Random per-run values: unique markers to search the logs for, not real secrets.
    password = "dbpw" + secrets.token_hex(12)
    secret_key = "apikey" + secrets.token_hex(12)
    real_engine = sqlalchemy.create_engine(
        f"sqlite:///{tmp_path}/pg-standin.db", connect_args={"check_same_thread": False}
    )
    seen: list[str] = []

    def fake_create_engine(url, **kwargs):
        seen.append(url)
        assert kwargs["pool_pre_ping"] is True
        return real_engine

    monkeypatch.setattr(db_module, "create_engine", fake_create_engine)
    app = create_app(
        Settings(
            database_url=f"postgresql+psycopg://admin:{password}@db.internal:5432/shortener",
            api_key=secret_key,
        )
    )
    with caplog.at_level(logging.DEBUG, logger="app"):
        with TestClient(app) as client:
            assert client.get("/readyz").status_code == 200
    assert seen and password in seen[0]
    assert _tuning_records(caplog)
    for r in caplog.records:
        dumped = r.getMessage() + str(r.__dict__)
        assert password not in dumped
        assert secret_key not in dumped
        assert "db.internal" not in dumped


def test_describe_engine_for_server_dialect_has_no_sqlite_fields():
    """Covers: NFR-16, FR-16"""
    pool = QueuePool(lambda: None, pool_size=8, max_overflow=3, timeout=11.0)
    fake = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), pool=pool)
    info = describe_engine(fake)
    assert info == {
        "dialect": "postgresql",
        "pool_class": "QueuePool",
        "pool_size": 8,
        "max_overflow": 3,
        "pool_timeout": 11.0,
    }


def test_log_engine_tuning_for_memory_sqlite_has_null_pool_fields(caplog):
    """Covers: NFR-16, FR-16"""
    engine = create_db_engine("sqlite://", pool_size=99)
    with caplog.at_level(logging.INFO, logger="app.db"):
        info = log_engine_tuning(engine)
    assert info["dialect"] == "sqlite"
    assert info["pool_size"] is None
    assert info["max_overflow"] is None
    assert info["pool_timeout"] is None
    assert info["journal_mode"] == "memory"
    assert _tuning_records(caplog)
    engine.dispose()
