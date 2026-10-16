"""Tests for probes (FR-10), JSON errors (FR-11, NFR-3) and request logging (NFR-8)."""

from __future__ import annotations

import json
import logging
import sys

from app.core.db import get_db
from app.core.logging import JsonFormatter, configure_logging
from app.services.link_service import LinkService
from helpers import assert_json_error, create_link
from sqlalchemy.exc import OperationalError

LEAK_MARKER = "leak-marker-boom-xyz"


def test_healthz_ok_without_database(client):
    """Covers: FR-10, NFR-8"""
    def broken_db():
        raise RuntimeError("db must not be touched")
        yield  # pragma: no cover

    client.app.dependency_overrides[get_db] = broken_db

    def exploding_factory():
        raise RuntimeError("db must not be touched")

    client.app.state.session_factory = exploding_factory
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_ok(client):
    """Covers: FR-10"""
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready"}


def test_readyz_db_failure_returns_503_without_exception_text(client):
    """Covers: FR-10, FR-11, NFR-3"""
    class BrokenSession:
        def execute(self, *args, **kwargs):
            raise OperationalError("SELECT 1", {}, Exception(LEAK_MARKER))

        def close(self):
            pass

    def broken_db():
        yield BrokenSession()

    client.app.dependency_overrides[get_db] = broken_db
    resp = client.get("/readyz")
    assert_json_error(resp, 503)
    assert LEAK_MARKER not in resp.text
    assert "OperationalError" not in resp.text
    assert "Traceback" not in resp.text
    assert "SELECT" not in resp.text


def test_readyz_session_dependency_raising_returns_503(client):
    """Covers: FR-10, FR-11, NFR-3"""
    def raising_db():
        raise OperationalError("connect", {}, Exception(LEAK_MARKER))
        yield  # pragma: no cover

    client.app.dependency_overrides[get_db] = raising_db
    resp = client.get("/readyz")
    assert_json_error(resp, 503)
    assert LEAK_MARKER not in resp.text
    assert "OperationalError" not in resp.text


def test_probes_not_shadowed_by_redirect_route(client):
    """Covers: FR-10, FR-5"""
    create_link(client)
    for path, expected in (("/healthz", {"status": "ok"}), ("/readyz", {"status": "ready"})):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 200
        assert resp.json() == expected
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_error_statuses_are_json_with_detail(client, monkeypatch):
    """Covers: FR-11"""
    # 404
    assert_json_error(client.get("/api/links/Missing"), 404)
    # 405 (wrong method on a fixed route)
    resp = client.put("/api/links", json={"url": "https://e.com"})
    assert_json_error(resp, 405)
    assert_json_error(client.post("/healthz"), 405)
    # 409
    client.post("/api/links", json={"url": "https://e.com/1"}, headers={"Idempotency-Key": "k"})
    assert_json_error(
        client.post("/api/links", json={"url": "https://e.com/2"}, headers={"Idempotency-Key": "k"}),
        409,
    )
    # 410
    code = create_link(client)["code"]
    client.delete(f"/api/links/{code}")
    assert_json_error(client.get(f"/{code}", follow_redirects=False), 410)
    # 422
    assert_json_error(client.post("/api/links", json={"url": "ftp://x"}), 422)
    # 503
    monkeypatch.setattr("app.services.codes.generate_code", lambda n: code)
    assert_json_error(client.post("/api/links", json={"url": "https://e.com/3"}), 503)


def test_unknown_api_routes_return_404_json(client):
    """Covers: FR-11"""
    for path in ("/api/unknown", "/api/links/abc/def/ghi", "/api/v2/links", "/a/b/c"):
        assert_json_error(client.get(path), 404)


def test_unhandled_exception_in_route_returns_generic_500(client, caplog):
    """Covers: FR-11, NFR-3"""
    def boom():
        raise RuntimeError(LEAK_MARKER)

    # Multi-segment path so the catch-all /{code} route cannot match it.
    client.app.add_api_route("/_test/boom", boom)
    with caplog.at_level(logging.DEBUG):
        resp = client.get("/_test/boom")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal Server Error"}
    assert resp.headers["content-type"].startswith("application/json")
    assert LEAK_MARKER not in resp.text
    assert "RuntimeError" not in resp.text
    assert "Traceback" not in resp.text
    logged = [r for r in caplog.records if r.exc_info and r.levelno >= logging.ERROR]
    assert logged, "unhandled exception must be logged with traceback"
    assert any(LEAK_MARKER in str(r.exc_info[1]) for r in logged)


def test_unhandled_exception_in_service_returns_generic_500(client, monkeypatch, caplog):
    """Covers: FR-11, NFR-3"""
    code = create_link(client)["code"]

    def explode(self, code):
        raise ValueError(LEAK_MARKER)

    monkeypatch.setattr(LinkService, "get_link_detail", explode)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/api/links/{code}")
    assert resp.status_code == 500
    assert resp.json() == {"detail": "Internal Server Error"}
    assert LEAK_MARKER not in resp.text
    assert "ValueError" not in resp.text
    logged = [r for r in caplog.records if r.exc_info and r.levelno >= logging.ERROR]
    assert logged, "unhandled exception must be logged with traceback"


def test_database_error_in_route_returns_503(client, monkeypatch, caplog):
    """Covers: FR-11, NFR-3, NFR-5"""
    code = create_link(client)["code"]

    def db_down(self, code):
        raise OperationalError("SELECT * FROM links", {}, Exception(LEAK_MARKER))

    monkeypatch.setattr(LinkService, "get_link_detail", db_down)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/api/links/{code}")
    assert_json_error(resp, 503)
    assert LEAK_MARKER not in resp.text
    assert "SELECT" not in resp.text


def test_request_log_line_has_method_path_status_duration(client, caplog):
    """Covers: NFR-8, NFR-1"""
    code = create_link(client)["code"]
    with caplog.at_level(logging.DEBUG):
        client.get(
            f"/{code}?utm=leakquery",
            headers={"X-Forwarded-For": "203.0.113.7"},
            follow_redirects=False,
        )
        client.get("/api/links/Missing1")
    records = [r for r in caplog.records if r.name == "app.request"]
    assert len(records) >= 2
    redirect_rec = next(r for r in records if r.path == f"/{code}")
    assert redirect_rec.method == "GET"
    assert redirect_rec.status == 307
    assert isinstance(redirect_rec.duration_ms, (int, float))
    assert redirect_rec.duration_ms >= 0
    missing = next(r for r in records if r.path == "/api/links/Missing1")
    assert missing.status == 404
    for r in records:
        dumped = str(r.__dict__)
        assert "testclient" not in dumped
        assert "203.0.113.7" not in dumped
        assert "leakquery" not in dumped


def test_json_formatter_emits_single_json_line():
    """Covers: NFR-8"""
    fmt = JsonFormatter()
    record = logging.LogRecord("app.request", logging.INFO, __file__, 1, "request", None, None)
    record.method = "GET"
    record.path = "/x"
    record.status = 200
    record.duration_ms = 1.5
    line = fmt.format(record)
    assert "\n" not in line
    data = json.loads(line)
    assert data["level"] == "INFO"
    assert data["logger"] == "app.request"
    assert data["message"] == "request"
    assert data["method"] == "GET" and data["path"] == "/x"
    assert data["status"] == 200 and data["duration_ms"] == 1.5
    assert "ts" in data

    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        err = logging.LogRecord(
            "app.errors", logging.ERROR, __file__, 1, "unhandled", None, sys.exc_info()
        )
    data = json.loads(fmt.format(err))
    assert "RuntimeError" in data["exc"]


def test_configure_logging_is_idempotent_and_sets_level():
    """Covers: NFR-8, FR-12"""
    logger = logging.getLogger("app")
    old_level = logger.level
    try:
        configure_logging("debug")
        configure_logging("WARNING")
        handlers = [
            h for h in logger.handlers
            if isinstance(h, logging.StreamHandler) and isinstance(h.formatter, JsonFormatter)
        ]
        assert len(handlers) == 1
        assert logger.level == logging.WARNING
        assert logger.propagate is True
    finally:
        logger.setLevel(old_level)
