"""Idempotency-Key semantics with alias and expires_at (FR-9, NFR-4)."""

from __future__ import annotations

import hashlib
from datetime import datetime

from app.models import IdempotencyKey, Link
from app.services.link_service import compute_request_hash
from helpers import assert_json_error, count_rows, new_session

URL = "https://example.com/idem"
NOW = datetime(2029, 6, 1, 12, 0, 0)


def _post(client, key, **fields):
    return client.post("/api/links", json={"url": URL, **fields}, headers={"Idempotency-Key": key})


def test_identical_payload_replays(client):
    """Covers: FR-9"""
    client.app.state.clock = lambda: NOW
    payload = {"alias": "idem-1", "expires_at": "2030-01-01T12:00:00Z"}
    first = _post(client, "k1", **payload)
    assert first.status_code == 201
    second = _post(client, "k1", **payload)
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert second.json() == first.json()
    assert count_rows(client, Link) == 1
    assert count_rows(client, IdempotencyKey) == 1


def test_different_alias_or_expiry_conflicts(client):
    """Covers: FR-9"""
    client.app.state.clock = lambda: NOW
    assert _post(client, "k2", alias="orig", expires_at="2030-01-01T12:00:00Z").status_code == 201
    assert_json_error(_post(client, "k2", alias="other", expires_at="2030-01-01T12:00:00Z"), 409)
    assert_json_error(_post(client, "k2", alias="orig", expires_at="2031-01-01T12:00:00Z"), 409)
    assert_json_error(_post(client, "k2", alias="orig"), 409)
    assert_json_error(_post(client, "k2"), 409)
    assert count_rows(client, Link) == 1


def test_alias_added_where_original_had_none_conflicts(client):
    """Covers: FR-9"""
    assert _post(client, "k3").status_code == 201
    resp = _post(client, "k3", alias="added")
    assert_json_error(resp, 409)
    assert "Traceback" not in resp.text
    assert count_rows(client, Link) == 1


def test_equal_instants_in_different_offsets_replay(client):
    """Covers: FR-9"""
    client.app.state.clock = lambda: NOW
    first = _post(client, "k4", expires_at="2030-01-01T12:00:00Z")
    assert first.status_code == 201
    second = _post(client, "k4", expires_at="2030-01-01T14:00:00+02:00")
    assert second.status_code == 201
    assert second.headers.get("Idempotent-Replayed") == "true"
    assert second.json()["code"] == first.json()["code"]
    third = _post(client, "k4", expires_at="2030-01-01T12:00:00")
    assert third.status_code == 201
    assert count_rows(client, Link) == 1


def test_legacy_url_only_hash_still_replays(client):
    """Covers: FR-9, FR-12"""
    with new_session(client) as s:
        link = Link(code="Legacy1", url=URL, is_active=True)
        s.add(link)
        s.flush()
        s.add(IdempotencyKey(key="legacy-key",
                             request_hash=hashlib.sha256(URL.encode("utf-8")).hexdigest(),
                             link_id=link.id))
        s.commit()
    resp = _post(client, "legacy-key")
    assert resp.status_code == 201, resp.text
    assert resp.headers.get("Idempotent-Replayed") == "true"
    assert resp.json()["code"] == "Legacy1"
    assert count_rows(client, Link) == 1


def test_compute_request_hash_rules():
    """Covers: FR-9"""
    legacy = hashlib.sha256(URL.encode("utf-8")).hexdigest()
    assert compute_request_hash(URL, None, None) == legacy
    with_alias = compute_request_hash(URL, "abc", None)
    assert with_alias != legacy
    assert compute_request_hash(URL, "abd", None) != with_alias
    exp = datetime(2030, 1, 1, 12)
    assert compute_request_hash(URL, None, exp) != legacy
    assert compute_request_hash(URL, "abc", exp) != with_alias


def test_reserved_or_taken_alias_with_key_stores_no_record(client):
    """Covers: FR-9, FR-2, FR-3"""
    assert_json_error(_post(client, "k5", alias="metrics"), 422)
    assert count_rows(client, IdempotencyKey) == 0
    assert client.post("/api/links", json={"url": URL, "alias": "taken"}).status_code == 201
    assert_json_error(_post(client, "k6", alias="taken"), 409)
    assert count_rows(client, IdempotencyKey) == 0
    assert count_rows(client, Link) == 1
    # The keys stay free for a later, valid request.
    assert _post(client, "k6", alias="free-now").status_code == 201
    assert count_rows(client, IdempotencyKey) == 1


def test_non_future_expiry_with_key_stores_no_record(client):
    """Covers: FR-9, FR-5"""
    client.app.state.clock = lambda: NOW
    assert_json_error(_post(client, "k7", expires_at="2020-01-01T00:00:00Z"), 422)
    assert count_rows(client, IdempotencyKey) == 0
    assert count_rows(client, Link) == 0


def test_replay_body_reflects_current_expiry_state(client):
    """Covers: FR-9, FR-7"""
    client.app.state.clock = lambda: NOW
    first = _post(client, "k8", expires_at="2030-01-01T12:00:00Z")
    assert first.json()["is_expired"] is False
    client.app.state.clock = lambda: datetime(2031, 1, 1)
    replay = _post(client, "k8", expires_at="2030-01-01T12:00:00Z")
    assert replay.status_code == 201
    assert replay.headers.get("Idempotent-Replayed") == "true"
    assert replay.json()["expires_at"] == "2030-01-01T12:00:00Z"
    assert replay.json()["is_expired"] is True
