"""Startup schema migration for links.expires_at (FR-8, NFR-3, NFR-6, NFR-7) and docs (FR-14)."""

from __future__ import annotations

import ast
import logging
import sqlite3
from pathlib import Path

import app.core.db as db
import pytest
from app.core.config import Settings
from app.core.db import create_db_engine, ensure_schema
from app.main import create_app
from fastapi.testclient import TestClient
from helpers import TEST_API_KEY, TEST_BASE_URL
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

ROOT = Path(__file__).resolve().parent.parent

LEGACY_DDL = """
CREATE TABLE links (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(32) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    deactivated_at DATETIME,
    CONSTRAINT uq_links_code UNIQUE (code)
)
"""


def _legacy_db(tmp_path) -> Path:
    path = tmp_path / "legacy.db"
    con = sqlite3.connect(path)
    con.execute(LEGACY_DDL)
    con.execute(
        "INSERT INTO links (code, url, is_active, created_at) VALUES (?, ?, 1, ?)",
        ("Old1234", "https://legacy.example.com/x", "2024-01-01 00:00:00.000000"),
    )
    con.commit()
    con.close()
    return path


def _boot(path: Path) -> TestClient:
    app = create_app(Settings(database_url=f"sqlite:///{path}", base_url=TEST_BASE_URL,
                              api_key=TEST_API_KEY))
    return TestClient(app, raise_server_exceptions=False, headers={"X-API-Key": TEST_API_KEY})


def _columns(path: Path) -> list[str]:
    con = sqlite3.connect(path)
    try:
        return [row[1] for row in con.execute("PRAGMA table_info(links)")]
    finally:
        con.close()


def _migration_info(caplog):
    return [r for r in caplog.records if r.name == "app.migrations" and r.levelno >= logging.INFO]


def test_legacy_database_is_migrated_and_usable(tmp_path, caplog):
    """Covers: FR-8, NFR-3, NFR-7, FR-12"""
    path = _legacy_db(tmp_path)
    assert "expires_at" not in _columns(path)
    caplog.set_level(logging.DEBUG, logger="app.migrations")
    with _boot(path) as client:
        assert "expires_at" in _columns(path)
        infos = _migration_info(caplog)
        assert len(infos) == 1
        assert "expires_at" in infos[0].getMessage()

        redirect = client.get("/Old1234", follow_redirects=False)
        assert redirect.status_code == 307
        assert redirect.headers["location"] == "https://legacy.example.com/x"
        meta = client.get("/api/links/Old1234").json()
        assert meta["expires_at"] is None
        assert meta["is_expired"] is False
        assert meta["total_clicks"] == 1

        assert client.post("/api/links", json={"url": "https://n.example/"}).status_code == 201
        resp = client.post("/api/links", json={"url": "https://n.example/",
                                               "alias": "new-alias",
                                               "expires_at": "2099-01-01T00:00:00Z"})
        assert resp.status_code == 201
        assert resp.json()["expires_at"] == "2099-01-01T00:00:00Z"


def test_second_boot_is_noop_and_preserves_data(tmp_path, caplog):
    """Covers: FR-8, NFR-3, NFR-7"""
    path = _legacy_db(tmp_path)
    with _boot(path) as client:
        client.post("/api/links", json={"url": "https://a.example/", "alias": "keep-me"})
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="app.migrations")
    with _boot(path) as client:
        assert _migration_info(caplog) == []
        assert client.get("/Old1234", follow_redirects=False).status_code == 307
        assert client.get("/keep-me", follow_redirects=False).status_code == 307
    con = sqlite3.connect(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM links").fetchone()[0] == 2
        assert con.execute("SELECT url FROM links WHERE code='Old1234'").fetchone()[0] == (
            "https://legacy.example.com/x"
        )
    finally:
        con.close()
    assert _columns(path).count("expires_at") == 1


def test_fresh_database_has_column_and_migration_is_noop(tmp_path, caplog):
    """Covers: FR-8, NFR-3"""
    path = tmp_path / "fresh.db"
    caplog.set_level(logging.DEBUG, logger="app.migrations")
    with _boot(path):
        assert "expires_at" in _columns(path)
    assert _migration_info(caplog) == []
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        assert ensure_schema(engine) is False
    finally:
        engine.dispose()


def test_ensure_schema_without_links_table(tmp_path):
    """Covers: FR-8"""
    engine = create_db_engine(f"sqlite:///{tmp_path}/empty.db")
    try:
        assert ensure_schema(engine) is False
        assert not inspect(engine).has_table("links")
    finally:
        engine.dispose()


def test_ensure_schema_returns_true_on_legacy_db(tmp_path):
    """Covers: FR-8, NFR-3"""
    path = _legacy_db(tmp_path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        assert ensure_schema(engine) is True
        assert ensure_schema(engine) is False
        col = next(c for c in inspect(engine).get_columns("links") if c["name"] == "expires_at")
        assert col["nullable"] is True
    finally:
        engine.dispose()


def test_concurrent_duplicate_column_is_tolerated(tmp_path, monkeypatch, caplog):
    """Covers: FR-8, NFR-3"""
    path = _legacy_db(tmp_path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        assert ensure_schema(engine) is True  # column now exists
        real = db._links_columns
        calls = {"n": 0}

        def stale_first(eng):
            calls["n"] += 1
            cols = real(eng)
            return cols - {"expires_at"} if calls["n"] == 1 else cols

        monkeypatch.setattr(db, "_links_columns", stale_first)
        caplog.set_level(logging.DEBUG, logger="app.migrations")
        assert ensure_schema(engine) is False  # ALTER failed with duplicate column
        assert calls["n"] == 2
    finally:
        engine.dispose()


def test_concurrent_race_tolerated_at_app_startup(tmp_path, monkeypatch):
    """Covers: FR-8"""
    path = tmp_path / "race.db"
    with _boot(path):
        pass
    real = db._links_columns
    calls = {"n": 0}

    def stale_first(eng):
        calls["n"] += 1
        cols = real(eng)
        return cols - {"expires_at"} if calls["n"] == 1 else cols

    monkeypatch.setattr(db, "_links_columns", stale_first)
    with _boot(path) as client:
        assert client.get("/healthz").status_code == 200


def test_other_migration_failure_is_raised_and_logged(tmp_path, monkeypatch, caplog):
    """Covers: FR-8, NFR-7"""
    path = _legacy_db(tmp_path)
    engine = create_db_engine(f"sqlite:///{path}")
    try:
        ensure_schema(engine)
        monkeypatch.setattr(db, "_links_columns", lambda eng: {"id", "code"})
        caplog.set_level(logging.DEBUG, logger="app.migrations")
        with pytest.raises(DBAPIError):
            ensure_schema(engine)
        assert any(r.levelno == logging.ERROR and r.name == "app.migrations"
                   for r in caplog.records)
    finally:
        engine.dispose()


def test_migration_ddl_is_literal_and_portable():
    """Covers: FR-8, NFR-6"""
    src = (ROOT / "app" / "core" / "db.py").read_text(encoding="utf-8")
    assert "ALTER TABLE links ADD COLUMN expires_at DATETIME NULL" in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "text":
            assert len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
            assert isinstance(node.args[0].value, str)


def test_migration_log_has_no_alias_or_expiry_values(tmp_path, caplog):
    """Covers: NFR-8, NFR-7"""
    caplog.set_level(logging.DEBUG, logger="app")
    with _boot(tmp_path / "log.db") as client:
        client.post("/api/links", json={"url": "https://p.example/", "alias": "secret-alias-xyz",
                                        "expires_at": "2099-03-04T05:06:07Z"})
    for record in caplog.records:
        if record.name.startswith("app") and record.name != "app.request":
            msg = record.getMessage()
            assert "secret-alias-xyz" not in msg
            assert "2099-03-04" not in msg


# ---------------------------------------------------------------- FR-14 docs


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_api_docs_describe_cr142():
    """Covers: FR-14"""
    api = _read("docs/API.md")
    for needle in ("alias", "expires_at", "is_expired", "metrics", "409", "410", "422",
                   "X-API-Key", "request_hash", "[A-Za-z0-9_-]{1,32}"):
        assert needle in api, needle
    assert "Authentication: none" not in api


def test_operations_docs_describe_migration():
    """Covers: FR-14, FR-8"""
    ops = _read("docs/OPERATIONS.md")
    assert "ALTER TABLE links ADD COLUMN expires_at DATETIME NULL" in ops
    assert "expires_at" in ops
    assert "MySQL" in ops
    assert "collation" in ops.lower()


def test_readme_and_changelog_updated():
    """Covers: FR-14, FR-11"""
    readme = _read("README.md")
    assert "alias" in readme.lower()
    assert "X-API-Key" in readme or "API key" in readme or "API_KEY" in readme
    changelog = _read("CHANGELOG.md")
    assert "CR-142" in changelog
    assert "BUG-77" in changelog
    assert "metrics" in changelog
