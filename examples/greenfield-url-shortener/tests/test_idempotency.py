"""Tests for idempotent creation via the Idempotency-Key header (FR-4, NFR-5)."""

from __future__ import annotations

import pytest
from app.models import IdempotencyKey, Link
from app.repositories.idempotency_repository import IdempotencyRepository
from helpers import assert_json_error, count_rows, create_link, new_session


def _post(client, url, key=None):
    headers = {"Idempotency-Key": key} if key is not None else {}
    return client.post("/api/links", json={"url": url}, headers=headers)


def test_same_key_same_body_replays_identical_response(client):
    """Covers: FR-4"""
    first = _post(client, "https://example.com/idem", "key-1")
    second = _post(client, "https://example.com/idem", "key-1")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["location"] == first.headers["location"]
    assert count_rows(client, Link) == 1
    assert count_rows(client, IdempotencyKey) == 1


def test_replay_header_only_on_replayed_response(client):
    """Covers: FR-4"""
    first = _post(client, "https://example.com/idem", "key-2")
    second = _post(client, "https://example.com/idem", "key-2")
    third = _post(client, "https://example.com/idem", "key-2")
    assert "idempotent-replayed" not in first.headers
    assert second.headers.get("idempotent-replayed") == "true"
    assert third.headers.get("idempotent-replayed") == "true"
    assert third.json() == first.json()


def test_same_key_different_url_returns_409(client):
    """Covers: FR-4, FR-11"""
    first = _post(client, "https://example.com/one", "key-3")
    assert first.status_code == 201
    resp = _post(client, "https://example.com/two", "key-3")
    assert_json_error(resp, 409)
    assert count_rows(client, Link) == 1
    # The original link is unaffected and still replayable.
    again = _post(client, "https://example.com/one", "key-3")
    assert again.status_code == 201
    assert again.json()["code"] == first.json()["code"]


@pytest.mark.parametrize("key", ["", "   ", "k" * 256, "x" * 1000])
def test_invalid_idempotency_key_rejected(client, key):
    """Covers: FR-4, FR-11"""
    resp = _post(client, "https://example.com/k", key)
    assert resp.status_code in (400, 422), resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "detail" in resp.json()
    assert count_rows(client, Link) == 0


def test_idempotency_key_of_255_chars_accepted(client):
    """Covers: FR-4"""
    key = "k" * 255
    first = _post(client, "https://example.com/long", key)
    second = _post(client, "https://example.com/long", key)
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json() == second.json()
    assert count_rows(client, Link) == 1


def test_different_keys_same_body_create_distinct_links(client):
    """Covers: FR-4"""
    a = _post(client, "https://example.com/same", "key-a")
    b = _post(client, "https://example.com/same", "key-b")
    assert a.status_code == b.status_code == 201
    assert a.json()["code"] != b.json()["code"]
    assert count_rows(client, Link) == 2


def test_requests_without_key_unaffected_by_keyed_requests(client):
    """Covers: FR-4, FR-1"""
    keyed = _post(client, "https://example.com/mix", "key-mix")
    plain1 = _post(client, "https://example.com/mix")
    plain2 = _post(client, "https://example.com/mix")
    codes = {keyed.json()["code"], plain1.json()["code"], plain2.json()["code"]}
    assert len(codes) == 3
    assert "idempotent-replayed" not in plain1.headers
    assert "idempotent-replayed" not in plain2.headers


def test_replay_of_deactivated_link_returns_stored_state(client):
    """Covers: FR-4, FR-7"""
    first = _post(client, "https://example.com/d", "key-d")
    code = first.json()["code"]
    assert client.delete(f"/api/links/{code}").status_code == 204
    replay = _post(client, "https://example.com/d", "key-d")
    assert replay.status_code == 201
    assert replay.json()["code"] == code
    assert count_rows(client, Link) == 1


def test_concurrent_race_loser_replays_winner(client, monkeypatch):
    """Covers: FR-4, NFR-5"""
    winner = _post(client, "https://example.com/race", "race-key")
    assert winner.status_code == 201

    real_get = IdempotencyRepository.get_by_key
    state = {"calls": 0}

    def get_none_once(self, key):
        state["calls"] += 1
        if state["calls"] == 1:
            return None  # simulate: the concurrent winner had not committed yet
        return real_get(self, key)

    monkeypatch.setattr(IdempotencyRepository, "get_by_key", get_none_once)
    loser = _post(client, "https://example.com/race", "race-key")
    assert loser.status_code == 201, loser.text
    assert loser.json() == winner.json()
    assert loser.headers.get("idempotent-replayed") == "true"
    assert count_rows(client, Link) == 1
    assert count_rows(client, IdempotencyKey) == 1


def test_concurrent_race_loser_with_different_payload_gets_409(client, monkeypatch):
    """Covers: FR-4, NFR-5"""
    assert _post(client, "https://example.com/race1", "race-key2").status_code == 201
    real_get = IdempotencyRepository.get_by_key
    state = {"calls": 0}

    def get_none_once(self, key):
        state["calls"] += 1
        return None if state["calls"] == 1 else real_get(self, key)

    monkeypatch.setattr(IdempotencyRepository, "get_by_key", get_none_once)
    resp = _post(client, "https://example.com/race2", "race-key2")
    assert_json_error(resp, 409)
    assert count_rows(client, Link) == 1


def test_race_with_unreadable_winner_returns_409_not_500(client, monkeypatch):
    """Covers: FR-4, NFR-5, FR-11"""
    assert _post(client, "https://example.com/r", "race-key3").status_code == 201
    monkeypatch.setattr(IdempotencyRepository, "get_by_key", lambda self, key: None)
    resp = _post(client, "https://example.com/r", "race-key3")
    assert_json_error(resp, 409)
    assert count_rows(client, Link) == 1


def test_idempotency_row_links_to_created_link(client):
    """Covers: FR-4, FR-12"""
    body = create_link(client, "https://example.com/row", headers={"Idempotency-Key": "row-key"})
    with new_session(client) as s:
        record = s.query(IdempotencyKey).one()
        link = s.get(Link, record.link_id)
        assert link.code == body["code"]
        assert record.key == "row-key"
        assert len(record.request_hash) == 64
