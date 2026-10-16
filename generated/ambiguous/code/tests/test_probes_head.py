"""Tests for HEAD support on the liveness/readiness probes (FR-14).

Also checks that the catch-all ``HEAD /{code}`` behaves as before and that the
OpenAPI path set changes only by the FR-24/FR-25 additions.
"""

from __future__ import annotations

import ast
import logging
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.core.db import get_db
from app.models import ClickEvent
from app.repositories.link_repository import LinkRepository
from helpers import assert_json_error, count_rows, create_link
from sqlalchemy import event
from sqlalchemy.exc import OperationalError

LEAK_MARKER = "leak-marker-head-probe-42"


@contextmanager
def capture_sql(engine):
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", _before)


class _BrokenSession:
    def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception(LEAK_MARKER))

    def close(self):
        pass


def _spy_link_repository(monkeypatch):
    calls: list[str] = []

    def _fail(name):
        def _inner(self, *args, **kwargs):
            calls.append(name)
            raise AssertionError(f"LinkRepository.{name} must not be called by probes")

        return _inner

    monkeypatch.setattr(LinkRepository, "get_by_code", _fail("get_by_code"))
    monkeypatch.setattr(LinkRepository, "get_redirect_target", _fail("get_redirect_target"))
    return calls


def test_head_healthz_200_empty_body_without_database(client):
    """Covers: FR-14, NFR-15"""

    def exploding_factory():
        raise RuntimeError("db must not be touched")

    def broken_db():
        raise RuntimeError("db must not be touched")
        yield  # pragma: no cover

    client.app.state.session_factory = exploding_factory
    client.app.dependency_overrides[get_db] = broken_db
    resp = client.head("/healthz")
    assert resp.status_code == 200
    assert resp.content == b""


def test_head_healthz_issues_no_sql_and_no_link_lookup(client, monkeypatch):
    """Covers: FR-14"""
    calls = _spy_link_repository(monkeypatch)
    with capture_sql(client.app.state.engine) as statements:
        resp = client.head("/healthz")
    assert resp.status_code == 200
    assert statements == []
    assert calls == []


def test_head_readyz_200_empty_body_runs_only_select_1(client, monkeypatch):
    """Covers: FR-14"""
    calls = _spy_link_repository(monkeypatch)
    with capture_sql(client.app.state.engine) as statements:
        resp = client.head("/readyz")
    assert resp.status_code == 200
    assert resp.content == b""
    assert [s.strip().upper() for s in statements] == ["SELECT 1"]
    assert not any("links" in s.lower() for s in statements)
    assert calls == []


def test_head_readyz_503_from_readiness_check_without_leak(client, monkeypatch, caplog):
    """Covers: FR-14, NFR-15"""
    calls = _spy_link_repository(monkeypatch)

    def broken_db():
        yield _BrokenSession()

    client.app.dependency_overrides[get_db] = broken_db
    with caplog.at_level(logging.DEBUG):
        resp = client.head("/readyz")
    assert resp.status_code == 503
    assert LEAK_MARKER not in resp.text
    assert "OperationalError" not in resp.text
    assert "Traceback" not in resp.text
    assert "SELECT" not in resp.text
    # The 503 comes from the readiness check, not from a link lookup.
    assert calls == []
    assert any(
        r.name == "app.health" and "readiness check failed" in r.getMessage()
        for r in caplog.records
    )
    # The GET twin returns the readiness detail (proves which code path produced it).
    get_resp = client.get("/readyz")
    assert_json_error(get_resp, 503)
    assert get_resp.json() == {"detail": "Database is not ready"}


def test_get_probes_keep_status_and_bodies(client):
    """Covers: FR-14, FR-10, FR-21"""
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    assert client.get("/readyz").json() == {"status": "ready"}


def test_post_probes_still_405(client):
    """Covers: FR-14, FR-21"""
    for path in ("/healthz", "/readyz"):
        resp = client.post(path)
        assert_json_error(resp, 405)


def test_probes_head_are_public(make_client):
    """Covers: FR-14, FR-21"""
    anon = make_client(send_api_key=False)
    assert anon.head("/healthz").status_code == 200
    assert anon.head("/readyz").status_code == 200


def test_head_code_active_307_exact_location_no_click(client):
    """Covers: FR-14, FR-21"""
    url = "https://example.com/p?q=a%20b&x=[1]#frag"
    code = create_link(client, url)["code"]
    resp = client.head(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    assert resp.headers["cache-control"] == "no-store"
    assert count_rows(client, ClickEvent) == 0


def test_head_code_unknown_and_malformed_404(client):
    """Covers: FR-14, FR-21"""
    for path in ("/Unknown1", "/bad-code", "/" + "a" * 40):
        resp = client.head(path, follow_redirects=False)
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/json")
        assert "location" not in resp.headers
        assert resp.headers.get("cache-control") == "no-store"
    assert count_rows(client, ClickEvent) == 0


def test_head_code_deactivated_and_expired_410_no_location(client, clock):
    """Covers: FR-14, FR-22, FR-21"""
    gone = create_link(client)["code"]
    client.delete(f"/api/links/{gone}")
    resp = client.post(
        "/api/links", json={"url": "https://example.com/e", "expires_at": "2025-12-31T23:59:00Z"}
    )
    expired = resp.json()["code"]
    clock.set(datetime(2026, 1, 1))
    for code in (gone, expired):
        r = client.head(f"/{code}", follow_redirects=False)
        assert r.status_code == 410
        assert "location" not in r.headers
        assert r.headers.get("cache-control") == "no-store"
        assert r.headers["content-type"].startswith("application/json")
    assert count_rows(client, ClickEvent) == 0


def test_openapi_paths_change_only_by_patch_and_hourly(client):
    """Covers: FR-14, FR-24, FR-25"""
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert set(paths) == {
        "/api/links",
        "/api/links/{code}",
        "/api/links/{code}/stats",
        "/api/links/{code}/stats/hourly",
        "/{code}",
        "/healthz",
        "/readyz",
    }
    assert set(paths["/healthz"]) == {"get"}
    assert set(paths["/readyz"]) == {"get"}
    assert set(paths["/{code}"]) == {"get"}
    assert set(paths["/api/links"]) == {"post"}
    assert set(paths["/api/links/{code}"]) == {"get", "patch", "delete"}
    assert set(paths["/api/links/{code}/stats"]) == {"get"}
    assert set(paths["/api/links/{code}/stats/hourly"]) == {"get"}


def test_redirect_router_is_registered_last():
    """Covers: FR-14"""
    main_py = Path(__file__).resolve().parent.parent / "app" / "main.py"
    tree = ast.parse(main_py.read_text(encoding="utf-8"))
    included = [
        ast.unparse(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "include_router"
    ]
    assert included == ["health.router", "links.router", "redirect.router"]
