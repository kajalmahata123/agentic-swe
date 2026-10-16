"""Tests for link metadata lookup (FR-6) and soft-delete (FR-7)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from app.models import ClickEvent, Link
from helpers import TEST_BASE_URL, assert_json_error, count_rows, create_link, new_session


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def test_lookup_active_link_full_shape(client):
    """Covers: FR-6"""
    created = create_link(client, "https://example.com/look")
    resp = client.get(f"/api/links/{created['code']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == created["code"]
    assert body["short_url"] == f"{TEST_BASE_URL}/{created['code']}"
    assert body["url"] == "https://example.com/look"
    assert body["is_active"] is True
    assert _parse(body["created_at"]).utcoffset() == timedelta(0)
    assert "deactivated_at" in body and body["deactivated_at"] is None
    assert isinstance(body["total_clicks"], int) and body["total_clicks"] == 0


def test_lookup_total_clicks_counts_redirects(client):
    """Covers: FR-6, FR-8"""
    code = create_link(client)["code"]
    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False)
    assert client.get(f"/api/links/{code}").json()["total_clicks"] == 3


def test_lookup_deactivated_link_still_returned(client):
    """Covers: FR-6, FR-7"""
    code = create_link(client)["code"]
    client.delete(f"/api/links/{code}")
    resp = client.get(f"/api/links/{code}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_active"] is False
    assert body["deactivated_at"] is not None
    assert _parse(body["deactivated_at"]).utcoffset() == timedelta(0)


def test_lookup_unknown_code_returns_404(client):
    """Covers: FR-6, FR-11"""
    resp = client.get("/api/links/Unknown1")
    assert_json_error(resp, 404)


@pytest.mark.parametrize(
    "code", ["abc-def", "abc_def", "a.b", "%20", "%C3%A9t%C3%A9", "a%27b", "a" * 40, "x;drop"]
)
def test_lookup_malformed_code_returns_404_not_500(client, code):
    """Covers: FR-6, FR-11"""
    resp = client.get(f"/api/links/{code}")
    assert resp.status_code in (404, 422), resp.text
    assert "detail" in resp.json()


def test_delete_active_link_soft_deletes(client):
    """Covers: FR-7"""
    code = create_link(client)["code"]
    before = datetime.now(UTC).replace(tzinfo=None)
    resp = client.delete(f"/api/links/{code}")
    after = datetime.now(UTC).replace(tzinfo=None)
    assert resp.status_code == 204
    assert resp.content == b""
    with new_session(client) as s:
        link = s.query(Link).filter_by(code=code).one()
        assert link.is_active is False
        assert link.deactivated_at is not None
        assert before - timedelta(seconds=2) <= link.deactivated_at <= after + timedelta(seconds=2)
    assert count_rows(client, Link) == 1


def test_delete_stops_redirects(client):
    """Covers: FR-7, FR-5"""
    code = create_link(client)["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    client.delete(f"/api/links/{code}")
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


def test_delete_is_idempotent_and_keeps_deactivated_at(client):
    """Covers: FR-7"""
    code = create_link(client)["code"]
    assert client.delete(f"/api/links/{code}").status_code == 204
    first = client.get(f"/api/links/{code}").json()["deactivated_at"]
    time.sleep(0.02)
    again = client.delete(f"/api/links/{code}")
    assert again.status_code == 204
    assert again.content == b""
    assert client.get(f"/api/links/{code}").json()["deactivated_at"] == first


def test_delete_unknown_returns_404(client):
    """Covers: FR-7, FR-11"""
    assert_json_error(client.delete("/api/links/Nope123"), 404)
    assert_json_error(client.delete("/api/links/bad-code"), 404)


def test_clicks_survive_deactivation_and_appear_in_stats(client):
    """Covers: FR-7, FR-9"""
    code = create_link(client)["code"]
    for _ in range(4):
        client.get(f"/{code}", headers={"Referer": "https://r.example"}, follow_redirects=False)
    client.delete(f"/api/links/{code}")
    assert count_rows(client, ClickEvent) == 4
    assert client.get(f"/api/links/{code}").json()["total_clicks"] == 4
    stats = client.get(f"/api/links/{code}/stats")
    assert stats.status_code == 200
    body = stats.json()
    assert body["total_clicks"] == 4
    assert body["top_referrers"] == [{"referrer": "https://r.example", "count": 4}]
    assert sum(d["count"] for d in body["clicks_per_day"]) == 4
