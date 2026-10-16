"""Startup schema upgrade for links.expires_at (FR-26), backward-compatible schema
(NFR-18) and MySQL case-sensitive codes (FR-19)."""

from __future__ import annotations

import ast
import hashlib
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from app.core.config import Settings
from app.core.schema import ensure_links_expires_at
from app.main import create_app
from app.models import ClickEvent, Link
from fastapi.testclient import TestClient
from helpers import AUTH_HEADERS, TEST_API_KEY, create_link, new_session
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable

APP_DIR = Path(__file__).resolve().parent.parent / "app"
LEGACY_URL = "https://example.com/legacy?x=1"
LEGACY_KEY = "legacy-idem-key"

# DDL produced by the previous release (no links.expires_at), rollback-journal mode.
OLD_SCHEMA = """
CREATE TABLE links (
    id INTEGER NOT NULL,
    code VARCHAR(32) NOT NULL,
    url VARCHAR(2048) NOT NULL,
    is_active BOOLEAN NOT NULL,
    created_at DATETIME NOT NULL,
    deactivated_at DATETIME,
    PRIMARY KEY (id),
    CONSTRAINT uq_links_code UNIQUE (code)
);
CREATE TABLE click_events (
    id INTEGER NOT NULL,
    link_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    referrer VARCHAR(2048),
    user_agent VARCHAR(512),
    ip_hash VARCHAR(64),
    PRIMARY KEY (id),
    FOREIGN KEY(link_id) REFERENCES links (id)
);
CREATE INDEX ix_click_events_link_id_created_at ON click_events (link_id, created_at);
CREATE TABLE idempotency_keys (
    id INTEGER NOT NULL,
    key VARCHAR(255) NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    link_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_idempotency_keys_key UNIQUE (key),
    FOREIGN KEY(link_id) REFERENCES links (id)
);
INSERT INTO links (id, code, url, is_active, created_at, deactivated_at)
VALUES (1, 'Legacy01', 'https://example.com/legacy?x=1', 1, '2025-06-01 10:00:00.000000', NULL);
INSERT INTO links (id, code, url, is_active, created_at, deactivated_at)
VALUES (2, 'Retired2', 'https://example.com/old', 0, '2025-06-01 10:00:00.000000',
        '2025-07-01 10:00:00.000000');
INSERT INTO click_events (link_id, created_at, referrer)
VALUES (1, '2025-06-02 09:15:00.000000', 'https://ref.example');
INSERT INTO click_events (link_id, created_at, referrer)
VALUES (1, '2025-06-02 09:45:00.000000', NULL);
"""


def _make_old_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO idempotency_keys (key, request_hash, link_id, created_at) "
            "VALUES (?, ?, 1, '2025-06-01 10:00:00.000000')",
            (LEGACY_KEY, hashlib.sha256(LEGACY_URL.encode("utf-8")).hexdigest()),
        )
        conn.commit()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


@contextmanager
def capture_alters(engine):
    alters: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().upper().startswith("ALTER"):
            alters.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield alters
    finally:
        event.remove(engine, "before_cursor_execute", _before)


def _app(db_path: Path):
    return create_app(Settings(database_url=f"sqlite:///{db_path}", api_key=TEST_API_KEY))


def _schema_records(caplog):
    return [r for r in caplog.records if hasattr(r, "schema_upgrade")]


def test_upgrade_old_schema_database_end_to_end(tmp_path, caplog):
    """Covers: FR-26, NFR-18, FR-21"""
    db_path = tmp_path / "old.db"
    _make_old_db(db_path)
    app = _app(db_path)
    with caplog.at_level(logging.INFO, logger="app"):
        with capture_alters(app.state.engine) as alters:
            with TestClient(app, headers=AUTH_HEADERS) as client:
                cols = {c["name"] for c in inspect(app.state.engine).get_columns("links")}
                assert "expires_at" in cols
                assert len(alters) == 1
                assert alters[0] == "ALTER TABLE links ADD COLUMN expires_at DATETIME"

                with app.state.engine.connect() as conn:
                    assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"

                resp = client.get("/Legacy01", follow_redirects=False)
                assert resp.status_code == 307
                assert resp.headers["location"] == LEGACY_URL
                assert client.head("/Legacy01", follow_redirects=False).status_code == 307
                assert client.get("/Retired2", follow_redirects=False).json() == {
                    "detail": "Link has been deactivated"
                }

                detail = client.get("/api/links/Legacy01")
                assert detail.status_code == 200
                body = detail.json()
                assert body["expires_at"] is None
                assert body["is_expired"] is False
                assert body["total_clicks"] == 3

                stats = client.get("/api/links/Legacy01/stats", params={"days": 365})
                assert stats.status_code == 200
                assert stats.json()["total_clicks"] == 3
                hourly = client.get(
                    "/api/links/Legacy01/stats/hourly",
                    params={"start": "2025-06-02T09:00:00Z", "end": "2025-06-02T10:00:00Z"},
                )
                assert hourly.json()["clicks_per_hour"] == [
                    {"hour": "2025-06-02T09:00:00Z", "count": 2}
                ]

                replay = client.post(
                    "/api/links", json={"url": LEGACY_URL}, headers={"Idempotency-Key": LEGACY_KEY}
                )
                assert replay.status_code == 201, replay.text
                assert replay.headers.get("idempotent-replayed") == "true"
                assert replay.json()["code"] == "Legacy01"
                assert replay.json()["expires_at"] is None
    records = _schema_records(caplog)
    assert len(records) == 1
    assert records[0].schema_upgrade == "added"
    assert records[0].getMessage() == "schema upgrade: links.expires_at added"
    with sqlite3.connect(db_path) as raw:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("SELECT COUNT(*) FROM click_events").fetchone()[0] == 3
        assert raw.execute("SELECT expires_at FROM links WHERE id = 1").fetchone()[0] is None


def test_second_startup_is_noop(tmp_path, caplog):
    """Covers: FR-26"""
    db_path = tmp_path / "old2.db"
    _make_old_db(db_path)
    first = _app(db_path)
    with TestClient(first):
        pass
    caplog.clear()
    second = _app(db_path)
    with caplog.at_level(logging.INFO, logger="app"):
        with capture_alters(second.state.engine) as alters:
            with TestClient(second, headers=AUTH_HEADERS) as client:
                assert client.get("/Legacy01", follow_redirects=False).status_code == 307
    assert alters == []
    (record,) = _schema_records(caplog)
    assert record.schema_upgrade == "already_present"
    assert record.getMessage() == "schema upgrade: links.expires_at already present"


def test_fresh_database_gets_column_from_create_all_without_alter(tmp_path, caplog):
    """Covers: FR-26"""
    app = _app(tmp_path / "fresh.db")
    with caplog.at_level(logging.INFO, logger="app"):
        with capture_alters(app.state.engine) as alters:
            with TestClient(app, headers=AUTH_HEADERS) as client:
                create_link(client)
    assert alters == []
    cols = {c["name"]: c for c in inspect(app.state.engine).get_columns("links")}
    assert cols["expires_at"]["nullable"] is True
    (record,) = _schema_records(caplog)
    assert record.schema_upgrade == "already_present"


def test_upgrade_failure_aborts_startup_with_logged_error(tmp_path, monkeypatch, caplog):
    """Covers: FR-26"""
    db_path = tmp_path / "old3.db"
    _make_old_db(db_path)
    real = Connection.exec_driver_sql

    def failing(self, statement, *args, **kwargs):
        if statement.strip().upper().startswith("ALTER"):
            raise OperationalError(statement, {}, Exception("attempt to write a readonly database"))
        return real(self, statement, *args, **kwargs)

    monkeypatch.setattr(Connection, "exec_driver_sql", failing)
    app = _app(db_path)
    served: list[int] = []
    with caplog.at_level(logging.INFO, logger="app"):
        with pytest.raises(OperationalError):
            with TestClient(app) as client:  # pragma: no cover - must not be reached
                served.append(client.get("/Legacy01", follow_redirects=False).status_code)
    assert served == []
    errors = [
        r for r in caplog.records
        if r.levelno == logging.ERROR
        and r.getMessage() == "schema upgrade failed: could not add links.expires_at"
    ]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert not any(getattr(r, "schema_upgrade", None) == "added" for r in caplog.records)
    assert not any(r.getMessage().startswith("database tuning") for r in caplog.records)


def test_ensure_links_expires_at_unit_returns_flag(tmp_path):
    """Covers: FR-26"""
    db_path = tmp_path / "unit.db"
    _make_old_db(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        assert ensure_links_expires_at(engine) is True
        assert ensure_links_expires_at(engine) is False
        cols = {c["name"] for c in inspect(engine).get_columns("links")}
        assert "expires_at" in cols
    finally:
        engine.dispose()


def test_postgresql_branch_uses_timestamp_without_time_zone(tmp_path):
    """Covers: FR-26"""
    db_path = tmp_path / "pg-branch.db"
    _make_old_db(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    # Pretend the dialect is PostgreSQL to exercise the dialect-guarded literal ALTER;
    # SQLite accepts the multi-word type name, so the statement runs.
    engine.dialect.name = "postgresql"
    try:
        with capture_alters(engine) as alters:
            assert ensure_links_expires_at(engine) is True
        assert alters == ["ALTER TABLE links ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE"]
    finally:
        engine.dispose()


def test_upgrade_code_lives_in_core_and_is_called_from_lifespan():
    """Covers: FR-26, NFR-17"""
    schema_py = APP_DIR / "core" / "schema.py"
    assert schema_py.exists()
    tree = ast.parse(schema_py.read_text(encoding="utf-8"))
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    } | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not {m for m in imported if m.startswith(("app.api", "app.services", "app.repositories"))}
    main_src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    main_tree = ast.parse(main_src)
    calls = [
        n.func.id for n in ast.walk(main_tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "ensure_links_expires_at" in calls
    # Literal SQL only.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "exec_driver_sql":
            assert all(isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args)


# --------------------------------------------------------------------------- NFR-18 DDL


def _ddl_lines(dialect) -> list[str]:
    ddl = str(CreateTable(Link.__table__).compile(dialect=dialect))
    return [ln.strip().rstrip(",").strip() for ln in ddl.strip().splitlines() if ln.strip()]


def test_sqlite_ddl_matches_baseline_plus_expires_at():
    """Covers: NFR-18, FR-19"""
    baseline = [
        "CREATE TABLE links (",
        "id INTEGER NOT NULL",
        "code VARCHAR(32) NOT NULL",
        "url VARCHAR(2048) NOT NULL",
        "is_active BOOLEAN NOT NULL",
        "created_at DATETIME NOT NULL",
        "deactivated_at DATETIME",
        "PRIMARY KEY (id)",
        "CONSTRAINT uq_links_code UNIQUE (code)",
        ")",
    ]
    expected = baseline[:7] + ["expires_at DATETIME"] + baseline[7:]
    assert _ddl_lines(sqlite.dialect()) == expected


def test_postgresql_ddl_matches_baseline_plus_expires_at():
    """Covers: NFR-18, FR-19"""
    baseline = [
        "CREATE TABLE links (",
        "id SERIAL NOT NULL",
        "code VARCHAR(32) NOT NULL",
        "url VARCHAR(2048) NOT NULL",
        "is_active BOOLEAN NOT NULL",
        "created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL",
        "deactivated_at TIMESTAMP WITHOUT TIME ZONE",
        "PRIMARY KEY (id)",
        "CONSTRAINT uq_links_code UNIQUE (code)",
        ")",
    ]
    expected = baseline[:7] + ["expires_at TIMESTAMP WITHOUT TIME ZONE"] + baseline[7:]
    assert _ddl_lines(postgresql.dialect()) == expected


def test_other_tables_ddl_unchanged():
    """Covers: NFR-18"""
    click_cols = {c.name for c in ClickEvent.__table__.columns}
    assert click_cols == {"id", "link_id", "created_at", "referrer", "user_agent", "ip_hash"}
    link_cols = [c.name for c in Link.__table__.columns]
    assert link_cols == [
        "id", "code", "url", "is_active", "created_at", "deactivated_at", "expires_at"
    ]
    assert Link.__table__.c.expires_at.nullable is True
    assert Link.__table__.c.expires_at.default is None
    assert Link.__table__.c.expires_at.server_default is None


# --------------------------------------------------------------------------- FR-19


def test_mysql_ddl_declares_binary_collation_for_code():
    """Covers: FR-19"""
    lines = _ddl_lines(mysql.dialect())
    code_line = next(ln for ln in lines if ln.startswith("code "))
    assert "COLLATE utf8mb4_bin" in code_line
    assert "NOT NULL" in code_line
    assert not any("COLLATE" in ln for ln in lines if not ln.startswith("code "))
    for dialect in (sqlite.dialect(), postgresql.dialect()):
        assert "COLLATE" not in " ".join(_ddl_lines(dialect))


def test_operations_doc_has_mysql_alter_statement():
    """Covers: FR-19, FR-20"""
    ops = (APP_DIR.parent / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    assert (
        "ALTER TABLE links MODIFY code VARCHAR(32) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin "
        "NOT NULL;" in ops
    )


def test_sqlite_codes_case_sensitive(client):
    """Covers: FR-19, FR-21"""
    with new_session(client) as s:
        s.add(Link(code="AbC1234", url="https://example.com/upper"))
        s.add(Link(code="abc1234", url="https://example.com/lower"))
        s.commit()
    upper = client.get("/AbC1234", follow_redirects=False)
    lower = client.get("/abc1234", follow_redirects=False)
    assert upper.headers["location"] == "https://example.com/upper"
    assert lower.headers["location"] == "https://example.com/lower"
    assert client.get("/ABC1234", follow_redirects=False).status_code == 404
    assert client.get("/api/links/AbC1234").json()["url"] == "https://example.com/upper"
