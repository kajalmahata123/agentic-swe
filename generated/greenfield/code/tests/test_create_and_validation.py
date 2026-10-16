"""Tests for link creation (FR-1) and URL validation (FR-2)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from app.models import Link
from app.schemas.links import validate_target_url
from helpers import TEST_BASE_URL, assert_json_error, count_rows, create_link


def test_create_link_returns_201_with_metadata(client):
    """Covers: FR-1, FR-12"""
    url = "https://example.com/a?b=1"
    before = datetime.now(UTC)
    resp = client.post("/api/links", json={"url": url})
    after = datetime.now(UTC)
    assert resp.status_code == 201
    body = resp.json()
    assert set(body) >= {"code", "short_url", "url", "is_active", "created_at"}
    assert isinstance(body["code"], str)
    assert re.fullmatch(r"[A-Za-z0-9]{7}", body["code"])
    assert body["short_url"] == f"{TEST_BASE_URL}/{body['code']}"
    assert body["url"] == url
    assert body["is_active"] is True
    created = datetime.fromisoformat(body["created_at"].replace("Z", "+00:00"))
    assert created.tzinfo is not None
    assert created.utcoffset() == timedelta(0)
    assert before - timedelta(seconds=2) <= created <= after + timedelta(seconds=2)


def test_create_link_sets_location_header(client):
    """Covers: FR-1"""
    resp = client.post("/api/links", json={"url": "https://example.com/x"})
    assert resp.status_code == 201
    assert resp.headers["location"] == f"/api/links/{resp.json()['code']}"


def test_same_url_twice_without_key_creates_two_links(client):
    """Covers: FR-1, FR-4"""
    a = create_link(client, "https://example.com/same")
    b = create_link(client, "https://example.com/same")
    assert a["code"] != b["code"]
    assert count_rows(client, Link) == 2
    assert "idempotent-replayed" not in {k.lower() for k in client.post(
        "/api/links", json={"url": "https://example.com/same"}).headers}


def test_get_after_create_returns_same_metadata(client):
    """Covers: FR-1, FR-6"""
    created = create_link(client, "https://example.com/meta?q=1&r=2")
    resp = client.get(f"/api/links/{created['code']}")
    assert resp.status_code == 200
    body = resp.json()
    for key, value in created.items():
        assert body[key] == value, key


def test_url_is_stored_unchanged(client):
    """Covers: FR-1, FR-2"""
    url = "HTTPS://Example.COM/Path/../x?a=%20b#frag"
    body = create_link(client, url)
    assert body["url"] == url


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # missing body
        {"json": {}},  # missing url field
        {"json": {"link": "https://example.com"}},
        {"json": {"url": 123}},
        {"json": {"url": None}},
        {"json": {"url": ["https://example.com"]}},
        {"json": {"url": {"href": "https://example.com"}}},
        {"content": b"not json", "headers": {"content-type": "application/json"}},
    ],
)
def test_invalid_body_returns_422(client, kwargs):
    """Covers: FR-1, FR-11"""
    resp = client.post("/api/links", **kwargs)
    assert_json_error(resp, 422)
    assert count_rows(client, Link) == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://example.com",
        "HTTPS://example.com",
        "HtTp://example.com/path",
        "https://example.com:8443/a/b?c=d#e",
        "http://127.0.0.1/x",
        "https://user:pw@example.com/",
        "http://[::1]:8080/",
    ],
)
def test_valid_urls_are_accepted(client, url):
    """Covers: FR-2"""
    resp = client.post("/api/links", json={"url": url})
    assert resp.status_code == 201, resp.text
    assert resp.json()["url"] == url


def test_url_length_boundary(client):
    """Covers: FR-2"""
    prefix = "https://example.com/"
    exact = prefix + "a" * (2048 - len(prefix))
    assert len(exact) == 2048
    ok = client.post("/api/links", json={"url": exact})
    assert ok.status_code == 201
    assert ok.json()["url"] == exact

    too_long = exact + "a"
    assert len(too_long) == 2049
    bad = client.post("/api/links", json={"url": too_long})
    assert_json_error(bad, 422)
    assert count_rows(client, Link) == 1


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,hi",
        "file:///etc/passwd",
        "ftp://example.com",
        "mailto:a@b.c",
        "JAVASCRIPT://example.com/%0aalert(1)",
        "/path",
        "example.com",
        "http://",
        "https:///x",
        "http:example.com",
        "http://exa mple.com",
        "http://example.com/a\nb",
        "http://example.com/a\tb",
        "http://example.com/\r",
        "http://example.com/\x00",
        "http://example.com/\x7f",
        " http://example.com",
        "http://example.com/ ",
        "http://exämple.com",
        "http://example.com\u00a0/",
        "http://example.com:99999/",
        "http://[::1/",
        "",
    ],
)
def test_invalid_urls_return_422_and_persist_nothing(client, url):
    """Covers: FR-2, NFR-2, FR-11"""
    resp = client.post("/api/links", json={"url": url})
    assert_json_error(resp, 422)
    assert count_rows(client, Link) == 0


def test_validate_target_url_unit():
    """Covers: FR-2"""
    assert validate_target_url("HTTPS://example.com") == "HTTPS://example.com"
    for bad in ("ftp://example.com", "", "http://", "https:///x", "a" * 3000):
        with pytest.raises(ValueError):
            validate_target_url(bad)
