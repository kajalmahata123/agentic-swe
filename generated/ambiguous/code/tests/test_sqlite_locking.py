"""Redirect resilience under SQLite write contention (FR-15, NFR-12) and privacy of
the click-failure log line (NFR-14)."""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

from app.models import ClickEvent
from app.repositories.click_repository import ClickRepository
from app.repositories.link_repository import LinkRepository
from helpers import assert_json_error, count_rows, create_link
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

CLIENT_IP = "testclient"
FORWARDED_IP = "198.51.100.23"
PRIVACY_HEADERS = {"X-Forwarded-For": FORWARDED_IP, "User-Agent": "Lock-UA/1.0"}
LEAK_MARKER = "lookup-leak-marker-77"


def _db_path(client) -> str:
    return client.app.state.engine.url.database


@contextmanager
def held_write_lock(client):
    """A second connection holding the write lock with an uncommitted insert."""
    conn = sqlite3.connect(_db_path(client), isolation_level=None, check_same_thread=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO links (code, url, is_active, created_at) "
            "VALUES ('LockHolder1', 'https://lock.example', 1, '2025-01-01 00:00:00.000000')"
        )
        yield conn
    finally:
        conn.execute("ROLLBACK")
        conn.close()


def _click_failures(caplog):
    return [
        r for r in caplog.records
        if r.name == "app.services.links" and "failed to record click" in r.getMessage()
    ]


def _assert_no_ip(caplog):
    assert CLIENT_IP not in caplog.text
    assert FORWARDED_IP not in caplog.text
    for r in caplog.records:
        dumped = str(r.__dict__)
        assert CLIENT_IP not in dumped
        assert FORWARDED_IP not in dumped


def test_get_redirect_under_held_write_lock_returns_307_quickly(make_client, caplog):
    """Covers: FR-15, NFR-12, NFR-14"""
    client = make_client(sqlite_busy_timeout_ms=200)
    url = "https://example.com/locked?a=1&b=%2F"
    code = create_link(client, url)["code"]
    with held_write_lock(client):
        with caplog.at_level(logging.DEBUG):
            start = time.perf_counter()
            resp = client.get(f"/{code}", headers=PRIVACY_HEADERS, follow_redirects=False)
            elapsed_ms = (time.perf_counter() - start) * 1000
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    assert resp.headers["cache-control"] == "no-store"
    assert elapsed_ms < 700, elapsed_ms
    # Worst case added latency is bounded by busy timeout + 500 ms (NFR-12).
    assert elapsed_ms < 200 + 500
    assert count_rows(client, ClickEvent) == 0
    failures = _click_failures(caplog)
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert failures[0].exc_info is None
    assert "OperationalError" in failures[0].getMessage()
    _assert_no_ip(caplog)


def test_head_deactivated_and_expired_under_held_write_lock(make_client, clock):
    """Covers: FR-15, FR-22, NFR-12"""
    client = make_client(sqlite_busy_timeout_ms=200)
    active = create_link(client, "https://example.com/active")["code"]
    gone = create_link(client, "https://example.com/gone")["code"]
    client.delete(f"/api/links/{gone}")
    expiring = client.post(
        "/api/links",
        json={"url": "https://example.com/exp", "expires_at": "2025-12-31T23:59:00Z"},
    ).json()["code"]
    clock.set(datetime(2026, 1, 1, 0, 0, 0))
    with held_write_lock(client):
        start = time.perf_counter()
        head = client.head(f"/{active}", follow_redirects=False)
        r_gone = client.get(f"/{gone}", follow_redirects=False)
        r_exp = client.get(f"/{expiring}", follow_redirects=False)
        r_exp_head = client.head(f"/{expiring}", follow_redirects=False)
        elapsed_ms = (time.perf_counter() - start) * 1000
    assert head.status_code == 307
    assert head.headers["location"] == "https://example.com/active"
    assert r_gone.status_code == 410
    assert r_gone.json() == {"detail": "Link has been deactivated"}
    assert r_exp.status_code == 410
    assert r_exp.json() == {"detail": "Link has expired"}
    assert r_exp_head.status_code == 410
    # WAL: none of these lookups waited for the lock.
    assert elapsed_ms < 600, elapsed_ms
    assert count_rows(client, ClickEvent) == 0


def test_reads_not_blocked_by_held_write_lock(make_client):
    """Covers: FR-15"""
    client = make_client(sqlite_busy_timeout_ms=2000)
    code = create_link(client)["code"]
    with held_write_lock(client):
        start = time.perf_counter()
        assert client.get(f"/api/links/{code}").status_code == 200
        assert client.get(f"/api/links/{code}/stats").status_code == 200
        assert client.head(f"/{code}", follow_redirects=False).status_code == 307
        assert client.get("/readyz").status_code == 200
        assert (time.perf_counter() - start) < 1.5


def test_click_recorded_after_lock_released(make_client):
    """Covers: FR-15, NFR-12"""
    client = make_client(sqlite_busy_timeout_ms=200)
    code = create_link(client)["code"]
    with held_write_lock(client):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert count_rows(client, ClickEvent) == 1


def test_each_successful_get_persists_exactly_one_click_visible_immediately(client):
    """Covers: FR-15, FR-21"""
    code = create_link(client)["code"]
    for expected in range(1, 6):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 307
        assert count_rows(client, ClickEvent) == expected
        assert client.get(f"/api/links/{code}").json()["total_clicks"] == expected
        hourly = client.get(f"/api/links/{code}/stats/hourly").json()
        assert hourly["total_clicks"] == expected
        assert hourly["clicks_per_hour"][-1]["count"] == expected
        daily = client.get(f"/api/links/{code}/stats").json()
        assert daily["total_clicks"] == expected


def test_click_insert_operational_error_still_307_rolls_back_and_logs(
    client, monkeypatch, caplog
):
    """Covers: NFR-12, FR-15, NFR-14"""
    url = "https://example.com/fault"
    code = create_link(client, url)["code"]
    rollbacks = {"n": 0}
    real_rollback = Session.rollback

    def counting_rollback(self):
        rollbacks["n"] += 1
        return real_rollback(self)

    def failing_add(self, event):
        raise OperationalError("INSERT INTO click_events", {}, Exception("database is locked"))

    monkeypatch.setattr(Session, "rollback", counting_rollback)
    monkeypatch.setattr(ClickRepository, "add", failing_add)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/{code}", headers=PRIVACY_HEADERS, follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    # one rollback ending the read transaction + one rolling back the failed write
    assert rollbacks["n"] >= 2
    failures = _click_failures(caplog)
    assert len(failures) == 1
    assert failures[0].levelno == logging.WARNING
    assert "database is locked" not in caplog.text.split("failed to record click")[-1]
    _assert_no_ip(caplog)
    monkeypatch.undo()
    assert count_rows(client, ClickEvent) == 0


def test_click_commit_operational_error_still_307_and_no_click(client, monkeypatch, caplog):
    """Covers: NFR-12, FR-15, NFR-14"""
    url = "https://example.com/commit-fault"
    code = create_link(client, url)["code"]
    real_commit = Session.commit
    real_rollback = Session.rollback
    calls = {"commit": 0, "rollback": 0}

    def failing_commit(self):
        calls["commit"] += 1
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    def counting_rollback(self):
        calls["rollback"] += 1
        return real_rollback(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    monkeypatch.setattr(Session, "rollback", counting_rollback)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/{code}", headers=PRIVACY_HEADERS, follow_redirects=False)
    monkeypatch.setattr(Session, "commit", real_commit)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    assert calls["commit"] == 1
    assert calls["rollback"] >= 2
    assert len(_click_failures(caplog)) == 1
    assert "disk I/O error" not in caplog.text
    _assert_no_ip(caplog)
    monkeypatch.undo()
    assert count_rows(client, ClickEvent) == 0
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert count_rows(client, ClickEvent) == 1


def test_click_failure_with_failing_rollback_still_307(client, monkeypatch, caplog):
    """Covers: NFR-12, FR-15"""
    code = create_link(client)["code"]
    real_rollback = Session.rollback
    state = {"n": 0}

    def flaky_rollback(self):
        state["n"] += 1
        if state["n"] == 2:
            raise OperationalError("ROLLBACK", {}, Exception("rollback failed"))
        return real_rollback(self)

    def failing_add(self, event):
        raise OperationalError("INSERT", {}, Exception("locked"))

    monkeypatch.setattr(Session, "rollback", flaky_rollback)
    monkeypatch.setattr(ClickRepository, "add", failing_add)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert len(_click_failures(caplog)) == 1


def test_lookup_failure_returns_503_without_internal_details(client, monkeypatch, caplog):
    """Covers: FR-15, FR-21, NFR-15"""
    code = create_link(client)["code"]

    def db_down(self, code):
        raise OperationalError("SELECT links.id FROM links", {}, Exception(LEAK_MARKER))

    monkeypatch.setattr(LinkRepository, "get_redirect_target", db_down)
    for method in ("get", "head"):
        resp = getattr(client, method)(f"/{code}", follow_redirects=False)
        assert resp.status_code == 503
        assert "location" not in resp.headers
        assert LEAK_MARKER not in resp.text
        assert "SELECT" not in resp.text
        assert "OperationalError" not in resp.text
    get_resp = client.get(f"/{code}", follow_redirects=False)
    assert_json_error(get_resp, 503)
    assert get_resp.json() == {"detail": "Service temporarily unavailable"}
    assert count_rows(client, ClickEvent) == 0


def test_lookup_on_unavailable_links_table_returns_503(client):
    """Covers: FR-15"""
    code = create_link(client)["code"]
    # Simulate the database becoming unavailable: drop the table out from under the app.
    with sqlite3.connect(_db_path(client)) as raw:
        raw.execute("ALTER TABLE links RENAME TO links_gone")
    resp = client.get(f"/{code}", follow_redirects=False)
    assert_json_error(resp, 503)
    assert resp.json() == {"detail": "Service temporarily unavailable"}
    assert "links" not in resp.text
