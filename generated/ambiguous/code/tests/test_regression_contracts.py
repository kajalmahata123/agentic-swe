"""Regression protection of existing contracts on the paths this change touches
(FR-21), on the hardened WAL file database."""

from __future__ import annotations

import logging

from app.models import ClickEvent, IdempotencyKey, Link
from app.repositories.idempotency_repository import IdempotencyRepository
from app.services.link_service import LinkService
from app.services.stats_service import StatsService
from helpers import assert_json_error, count_rows, create_link
from sqlalchemy.exc import OperationalError

LEAK = "regression-leak-marker-5150"


def test_test_databases_run_in_wal_mode(client):
    """Covers: FR-21, FR-15"""
    with client.app.state.engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_redirect_contract_unchanged_for_null_expiry_links(client):
    """Covers: FR-21, FR-5"""
    url = "https://example.com/a?b=1&c=%2F#frag"
    code = create_link(client, url)["code"]
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    assert resp.headers["cache-control"] == "no-store"
    assert resp.content == b""
    missing = client.get("/Missing1", follow_redirects=False)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Link not found"}
    assert "location" not in missing.headers


def test_first_get_and_head_after_delete_are_410(client):
    """Covers: FR-21, FR-7"""
    code = create_link(client)["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert client.delete(f"/api/links/{code}").status_code == 204
    head = client.head(f"/{code}", follow_redirects=False)
    get = client.get(f"/{code}", follow_redirects=False)
    assert head.status_code == 410 and get.status_code == 410
    assert get.json() == {"detail": "Link has been deactivated"}
    assert "location" not in get.headers
    assert count_rows(client, ClickEvent) == 1


def test_idempotency_replay_conflict_and_race_on_wal(client, monkeypatch):
    """Covers: FR-21, FR-4"""
    first = client.post("/api/links", json={"url": "https://e.com/1"},
                        headers={"Idempotency-Key": "wal-k"})
    replay = client.post("/api/links", json={"url": "https://e.com/1"},
                         headers={"Idempotency-Key": "wal-k"})
    assert first.status_code == replay.status_code == 201
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replayed"] == "true"
    assert_json_error(
        client.post("/api/links", json={"url": "https://e.com/2"},
                    headers={"Idempotency-Key": "wal-k"}),
        409,
    )
    real_get = IdempotencyRepository.get_by_key
    state = {"n": 0}

    def none_once(self, key):
        state["n"] += 1
        return None if state["n"] == 1 else real_get(self, key)

    monkeypatch.setattr(IdempotencyRepository, "get_by_key", none_once)
    raced = client.post("/api/links", json={"url": "https://e.com/1"},
                        headers={"Idempotency-Key": "wal-k"})
    assert raced.status_code == 201
    assert raced.json()["code"] == first.json()["code"]
    assert count_rows(client, Link) == 1
    assert count_rows(client, IdempotencyKey) == 1


def test_collision_retry_and_exhaustion_on_wal(client, monkeypatch):
    """Covers: FR-21, FR-3"""
    taken = create_link(client)["code"]
    seq = iter([taken, taken, "Fresh12"])
    monkeypatch.setattr("app.services.codes.generate_code", lambda n: next(seq))
    resp = client.post("/api/links", json={"url": "https://e.com/retry"})
    assert resp.status_code == 201
    assert resp.json()["code"] == "Fresh12"
    monkeypatch.setattr("app.services.codes.generate_code", lambda n: taken)
    exhausted = client.post("/api/links", json={"url": "https://e.com/x"})
    assert_json_error(exhausted, 503)


def test_database_errors_on_new_routes_return_generic_503(client, monkeypatch, caplog):
    """Covers: FR-21, NFR-15, FR-24, FR-25"""
    code = create_link(client)["code"]

    def db_down(*args, **kwargs):
        raise OperationalError("SELECT secret FROM links", {}, Exception(LEAK))

    monkeypatch.setattr(LinkService, "set_expiry", db_down)
    monkeypatch.setattr(StatsService, "get_hourly_clicks", db_down)
    with caplog.at_level(logging.DEBUG):
        for resp in (
            client.patch(f"/api/links/{code}", json={"expires_at": None}),
            client.get(f"/api/links/{code}/stats/hourly"),
        ):
            assert_json_error(resp, 503)
            assert resp.json() == {"detail": "Service temporarily unavailable"}
            assert LEAK not in resp.text and "SELECT" not in resp.text


def test_unknown_errors_on_new_routes_return_generic_500(client, monkeypatch):
    """Covers: FR-21, NFR-15"""
    code = create_link(client)["code"]

    def boom(*args, **kwargs):
        raise RuntimeError(LEAK)

    monkeypatch.setattr(LinkService, "set_expiry", boom)
    monkeypatch.setattr(StatsService, "get_hourly_clicks", boom)
    monkeypatch.setattr(LinkService, "resolve_and_record_click", boom)
    for resp in (
        client.patch(f"/api/links/{code}", json={"expires_at": None}),
        client.get(f"/api/links/{code}/stats/hourly"),
        client.get(f"/{code}", follow_redirects=False),
    ):
        assert resp.status_code == 500
        assert resp.json() == {"detail": "Internal Server Error"}
        assert LEAK not in resp.text and "RuntimeError" not in resp.text


def test_click_truncation_still_applies(client):
    """Covers: FR-21, FR-8"""
    code = create_link(client)["code"]
    client.get(
        f"/{code}",
        headers={"Referer": "https://r.example/" + "r" * 3000, "User-Agent": "U" * 900},
        follow_redirects=False,
    )
    with client.app.state.session_factory() as s:
        event = s.query(ClickEvent).one()
        assert len(event.referrer) == 2048
        assert len(event.user_agent) == 512


def test_replay_pointing_at_missing_link_returns_409_not_500(client):
    """Covers: FR-21, FR-23"""
    import hashlib

    url = "https://example.com/orphan"
    with client.app.state.session_factory() as s:
        s.add(IdempotencyKey(
            key="orphan-key",
            request_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
            link_id=987654,
        ))
        s.commit()
    resp = client.post("/api/links", json={"url": url}, headers={"Idempotency-Key": "orphan-key"})
    assert_json_error(resp, 409)
    assert count_rows(client, Link) == 0


def test_bucket_normalisers_accept_driver_specific_values():
    """Covers: FR-18, FR-25, NFR-22"""
    from datetime import date, datetime, timedelta, timezone

    from app.repositories.click_repository import _as_date, _as_hour

    assert _as_date("2025-12-31") == date(2025, 12, 31)
    assert _as_date(datetime(2025, 12, 31, 23, 59)) == date(2025, 12, 31)  # MySQL/PG datetime
    assert _as_date(date(2025, 12, 31)) == date(2025, 12, 31)  # PostgreSQL CAST AS DATE
    assert _as_hour("2025-12-31 23:00:00") == datetime(2025, 12, 31, 23)
    assert _as_hour(datetime(2025, 12, 31, 23, 0, 0)) == datetime(2025, 12, 31, 23)
    aware = datetime(2026, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    assert _as_hour(aware) == datetime(2025, 12, 31, 23)


def test_response_schemas_serialise_aware_datetimes_as_utc_z():
    """Covers: FR-23, NFR-22"""
    from datetime import datetime, timedelta, timezone

    from app.schemas import LinkCreatedResponse

    plus_one = timezone(timedelta(hours=1))
    body = LinkCreatedResponse(
        code="abc", short_url="http://s/abc", url="https://e.com", is_active=True,
        created_at=datetime(2026, 1, 1, 0, 59, tzinfo=plus_one),
        expires_at=datetime(2026, 1, 1, 0, 59, tzinfo=plus_one),
    ).model_dump(mode="json")
    assert body["created_at"] == "2025-12-31T23:59:00Z"
    assert body["expires_at"] == "2025-12-31T23:59:00Z"


def test_detail_body_only_extended_with_expiry_fields(client):
    """Covers: FR-21, FR-23"""
    code = create_link(client)["code"]
    body = client.get(f"/api/links/{code}").json()
    assert set(body) == {
        "code", "short_url", "url", "is_active", "created_at", "deactivated_at",
        "total_clicks", "expires_at", "is_expired",
    }
    stats = client.get(f"/api/links/{code}/stats").json()
    assert set(stats) == {"code", "total_clicks", "clicks_per_day", "top_referrers"}
