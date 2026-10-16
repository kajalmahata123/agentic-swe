"""Tests for redirects (FR-5) and click recording/privacy (FR-8, NFR-1)."""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta

from app.models import ClickEvent
from app.repositories.click_repository import ClickRepository
from app.services.link_service import hash_ip
from helpers import assert_json_error, count_rows, create_link, new_session
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError

CLIENT_IP = "testclient"  # request.client.host under TestClient
FORWARDED_IP = "203.0.113.7"


def _events(client):
    with new_session(client) as s:
        return s.query(ClickEvent).order_by(ClickEvent.id).all()


def test_redirect_returns_307_with_exact_location(client):
    """Covers: FR-5"""
    url = "https://example.com/a?b=1&c=%2F#frag"
    code = create_link(client, url)["code"]
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url


def test_redirect_location_not_requoted(client):
    """Covers: FR-5"""
    url = "http://example.com/~user/a%20b?x=[1]&y=|&z='q'"
    code = create_link(client, url)["code"]
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url


def test_redirect_sets_no_store_cache_control(client):
    """Covers: FR-5"""
    code = create_link(client)["code"]
    resp = client.get(f"/{code}", follow_redirects=False)
    cc = resp.headers.get("cache-control", "")
    assert "no-store" in cc or ("private" in cc and "max-age=0" in cc)


def test_redirect_unknown_code_returns_404_json(client):
    """Covers: FR-5, FR-11"""
    resp = client.get("/Unknown", follow_redirects=False)
    assert_json_error(resp, 404)
    assert isinstance(resp.json()["detail"], str)
    assert "location" not in resp.headers


def test_redirect_malformed_code_returns_404(client):
    """Covers: FR-5, FR-6"""
    for path in ("/abc-def", "/a.b", "/" + "a" * 40, "/%C3%A9"):
        resp = client.get(path, follow_redirects=False)
        assert_json_error(resp, 404)


def test_redirect_deactivated_returns_410_and_no_redirect(client):
    """Covers: FR-5, FR-7, FR-11"""
    code = create_link(client)["code"]
    assert client.delete(f"/api/links/{code}").status_code == 204
    resp = client.get(f"/{code}", follow_redirects=False)
    assert_json_error(resp, 410)
    assert isinstance(resp.json()["detail"], str)
    assert "location" not in resp.headers


def test_head_behaves_like_get_without_recording_click(client):
    """Covers: FR-5, FR-8"""
    url = "https://example.com/head"
    code = create_link(client, url)["code"]
    resp = client.head(f"/{code}", follow_redirects=False)
    assert resp.status_code in (307, 405)
    if resp.status_code == 307:
        assert resp.headers["location"] == url
        assert client.head("/Missing", follow_redirects=False).status_code == 404
        client.delete(f"/api/links/{code}")
        assert client.head(f"/{code}", follow_redirects=False).status_code == 410
    assert count_rows(client, ClickEvent) == 0


def test_n_redirects_create_n_click_events(client):
    """Covers: FR-8"""
    code = create_link(client)["code"]
    other = create_link(client)["code"]
    for _ in range(7):
        assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    client.get(f"/{other}", follow_redirects=False)
    events = _events(client)
    assert len(events) == 8
    detail = client.get(f"/api/links/{code}").json()
    assert detail["total_clicks"] == 7
    assert client.get(f"/api/links/{other}").json()["total_clicks"] == 1


def test_click_event_stores_timestamp_referrer_and_user_agent(client):
    """Covers: FR-8"""
    code = create_link(client)["code"]
    before = datetime.now(UTC).replace(tzinfo=None)
    client.get(
        f"/{code}",
        headers={"Referer": "https://ref.example/page", "User-Agent": "UA-Test/1.0"},
        follow_redirects=False,
    )
    after = datetime.now(UTC).replace(tzinfo=None)
    (event,) = _events(client)
    assert event.referrer == "https://ref.example/page"
    assert event.user_agent == "UA-Test/1.0"
    created = event.created_at
    if created.tzinfo is not None:
        created = created.astimezone(UTC).replace(tzinfo=None)
    assert before - timedelta(seconds=2) <= created <= after + timedelta(seconds=2)


def test_click_event_null_when_headers_absent(client):
    """Covers: FR-8"""
    code = create_link(client)["code"]
    # Remove TestClient's default user-agent header.
    client.headers.pop("user-agent", None)
    client.get(f"/{code}", follow_redirects=False)
    (event,) = _events(client)
    assert event.referrer is None
    assert event.user_agent is None


def test_oversized_headers_are_truncated(client):
    """Covers: FR-8"""
    code = create_link(client)["code"]
    long_ref = "https://ref.example/" + "r" * 5000
    long_ua = "U" * 2000
    resp = client.get(
        f"/{code}", headers={"Referer": long_ref, "User-Agent": long_ua}, follow_redirects=False
    )
    assert resp.status_code == 307
    (event,) = _events(client)
    assert event.referrer == long_ref[:2048]
    assert event.user_agent == long_ua[:512]


def test_404_and_410_do_not_create_click_events(client):
    """Covers: FR-8"""
    client.get("/Nothere", follow_redirects=False)
    code = create_link(client)["code"]
    client.delete(f"/api/links/{code}")
    client.get(f"/{code}", follow_redirects=False)
    assert count_rows(client, ClickEvent) == 0


def test_click_events_have_no_raw_ip_column(client):
    """Covers: FR-8, NFR-1"""
    columns = {c["name"] for c in inspect(client.app.state.engine).get_columns("click_events")}
    assert "ip_hash" in columns
    allowed = {"id", "link_id", "created_at", "referrer", "user_agent", "ip_hash"}
    assert columns <= allowed, columns - allowed
    assert not {"ip", "ip_address", "client_ip", "remote_addr", "ip_addr"} & columns


def test_ip_hash_null_without_salt_and_ip_never_stored(client, caplog):
    """Covers: FR-8, NFR-1, NFR-8"""
    code = create_link(client)["code"]
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            client.get(
                f"/{code}",
                # TestClient's default User-Agent is also "testclient"; override it so the
                # scan only targets the client address.
                headers={
                    "X-Forwarded-For": FORWARDED_IP,
                    "X-Real-IP": FORWARDED_IP,
                    "User-Agent": "Privacy-UA/1.0",
                },
                follow_redirects=False,
            )
    events = _events(client)
    assert len(events) == 3
    for event in events:
        assert event.ip_hash is None
        for col in ClickEvent.__table__.columns:
            value = getattr(event, col.key)
            assert CLIENT_IP not in str(value)
            assert FORWARDED_IP not in str(value)
    assert CLIENT_IP not in caplog.text
    assert FORWARDED_IP not in caplog.text
    for record in caplog.records:
        assert FORWARDED_IP not in str(record.__dict__)
        assert CLIENT_IP not in str(record.__dict__)


def test_ip_hash_is_hmac_sha256_with_salt(make_client, caplog):
    """Covers: FR-8, NFR-1, FR-12"""
    salt = "s3cr3t-salt"
    client = make_client(ip_hash_salt=salt)
    code = create_link(client)["code"]
    with caplog.at_level(logging.DEBUG):
        client.get(
            f"/{code}",
            headers={"X-Forwarded-For": FORWARDED_IP, "User-Agent": "Privacy-UA/1.0"},
            follow_redirects=False,
        )
    (event,) = _events(client)
    expected = hmac.new(salt.encode(), CLIENT_IP.encode(), hashlib.sha256).hexdigest()
    assert event.ip_hash == expected
    for col in ClickEvent.__table__.columns:
        value = str(getattr(event, col.key))
        assert CLIENT_IP not in value
        assert FORWARDED_IP not in value
    assert CLIENT_IP not in caplog.text
    assert FORWARDED_IP not in caplog.text


def test_blank_salt_means_no_hash(make_client):
    """Covers: FR-8, FR-12, NFR-1"""
    client = make_client(ip_hash_salt="   ")
    code = create_link(client)["code"]
    client.get(f"/{code}", follow_redirects=False)
    (event,) = _events(client)
    assert event.ip_hash is None


def test_hash_ip_unit():
    """Covers: FR-8, NFR-1"""
    assert hash_ip(None, "salt") is None
    assert hash_ip("1.2.3.4", None) is None
    assert hash_ip("1.2.3.4", "") is None
    digest = hash_ip("1.2.3.4", "salt")
    assert digest == hmac.new(b"salt", b"1.2.3.4", hashlib.sha256).hexdigest()
    assert "1.2.3.4" not in digest


def test_click_recording_failure_still_redirects_and_logs(client, monkeypatch, caplog):
    """Covers: FR-8"""
    url = "https://example.com/fail"
    code = create_link(client, url)["code"]

    def boom(self, event):
        raise SQLAlchemyError("insert failed")

    monkeypatch.setattr(ClickRepository, "add", boom)
    with caplog.at_level(logging.DEBUG):
        resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    failures = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert failures, "click recording failure must be logged"
    assert CLIENT_IP not in caplog.text
    monkeypatch.undo()
    assert count_rows(client, ClickEvent) == 0
    # Service keeps working afterwards.
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert count_rows(client, ClickEvent) == 1
