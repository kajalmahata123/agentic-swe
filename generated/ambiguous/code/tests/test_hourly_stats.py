"""Hourly click counts per link (FR-25), bounded memory/latency (NFR-21, NFR-13),
privacy of the hourly path (NFR-14) and timezone handling (NFR-22)."""

from __future__ import annotations

import logging
import time
import tracemalloc
from datetime import datetime, timedelta

import pytest
from app.core.errors import InvalidInputError, NotFoundError
from app.models import ClickEvent, Link
from app.repositories.click_repository import (
    ClickRepository,
    build_hourly_counts_stmt,
    hour_bucket_expression,
)
from app.services.stats_service import StatsService
from helpers import AUTH_HEADERS, assert_json_error, create_link, new_session
from sqlalchemy import insert
from sqlalchemy.dialects import mysql, postgresql, sqlite

HOURLY_KEYS = {"code", "start", "end", "total_clicks", "clicks_per_hour"}


def _link_id(client, code: str) -> int:
    with new_session(client) as s:
        return s.query(Link).filter_by(code=code).one().id


def _insert_clicks(client, link_id: int, timestamps):
    with new_session(client) as s:
        for ts in timestamps:
            s.add(ClickEvent(link_id=link_id, created_at=ts))
        s.commit()


def _hourly(client, code, **params):
    return client.get(f"/api/links/{code}/stats/hourly", params=params)


@pytest.fixture
def link(client):
    code = create_link(client)["code"]
    return code, _link_id(client, code)


def test_hourly_example_half_open_buckets(client, link):
    """Covers: FR-25"""
    code, link_id = link
    _insert_clicks(
        client,
        link_id,
        [datetime(2025, 11, 20, 20, 0, 0), datetime(2025, 11, 20, 20, 59, 59),
         datetime(2025, 11, 20, 21, 0, 0), datetime(2025, 11, 20, 22, 0, 0),
         datetime(2025, 11, 20, 19, 59, 59, 999999)],
    )
    resp = _hourly(client, code, start="2025-11-20T20:00:00Z", end="2025-11-20T22:00:00Z")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HOURLY_KEYS
    assert body == {
        "code": code,
        "start": "2025-11-20T20:00:00Z",
        "end": "2025-11-20T22:00:00Z",
        "total_clicks": 3,
        "clicks_per_hour": [
            {"hour": "2025-11-20T20:00:00Z", "count": 2},
            {"hour": "2025-11-20T21:00:00Z", "count": 1},
        ],
    }


def test_hourly_zero_filled_ascending(client, link):
    """Covers: FR-25"""
    code, link_id = link
    _insert_clicks(client, link_id, [datetime(2025, 11, 20, 3, 30)] * 4)
    body = _hourly(client, code, start="2025-11-20T00:00:00Z", end="2025-11-20T06:00:00Z").json()
    hours = [e["hour"] for e in body["clicks_per_hour"]]
    assert hours == [f"2025-11-20T0{h}:00:00Z" for h in range(6)]
    assert [e["count"] for e in body["clicks_per_hour"]] == [0, 0, 0, 4, 0, 0]
    assert body["total_clicks"] == 4
    for e in body["clicks_per_hour"]:
        assert set(e) == {"hour", "count"}
        assert isinstance(e["count"], int)


def test_hourly_across_utc_day_boundary(client, link):
    """Covers: FR-25"""
    code, link_id = link
    _insert_clicks(
        client,
        link_id,
        [datetime(2025, 12, 31, 23, 0), datetime(2025, 12, 31, 23, 59, 59),
         datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 30)],
    )
    body = _hourly(client, code, start="2025-12-31T22:00:00Z", end="2026-01-01T02:00:00Z").json()
    assert body["clicks_per_hour"] == [
        {"hour": "2025-12-31T22:00:00Z", "count": 0},
        {"hour": "2025-12-31T23:00:00Z", "count": 2},
        {"hour": "2026-01-01T00:00:00Z", "count": 2},
        {"hour": "2026-01-01T01:00:00Z", "count": 0},
    ]
    assert body["total_clicks"] == 4


def test_hourly_bucketing_exact_for_sub_millisecond_timestamps(client, link):
    """Covers: FR-25, NFR-22

    Clicks are stored with microsecond precision (``utcnow()``). A click at
    23:59:59.999999 belongs to the 23:00 bucket of the same UTC day, and every click
    inside [start, end) must be counted in total_clicks.
    """
    code, link_id = link
    _insert_clicks(
        client,
        link_id,
        [datetime(2025, 12, 31, 23, 0), datetime(2025, 12, 31, 23, 59, 59, 999999),
         datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 30),
         datetime(2026, 1, 1, 0, 59, 59, 999700)],
    )
    body = _hourly(client, code, start="2025-12-31T22:00:00Z", end="2026-01-01T02:00:00Z").json()
    assert body["clicks_per_hour"] == [
        {"hour": "2025-12-31T22:00:00Z", "count": 0},
        {"hour": "2025-12-31T23:00:00Z", "count": 2},
        {"hour": "2026-01-01T00:00:00Z", "count": 3},
        {"hour": "2026-01-01T01:00:00Z", "count": 0},
    ]
    assert body["total_clicks"] == 5


def test_hourly_offset_start_and_alignment(client, link):
    """Covers: FR-25, NFR-22"""
    code, link_id = link
    _insert_clicks(client, link_id, [datetime(2025, 11, 20, 19, 5), datetime(2025, 11, 20, 20, 40)])
    # Use params= so httpx encodes '+' as %2B.
    body = _hourly(
        client, code, start="2025-11-20T20:15:00+01:00", end="2025-11-20T21:45:00+01:00"
    ).json()
    assert body["start"] == "2025-11-20T19:00:00Z"
    assert body["end"] == "2025-11-20T21:00:00Z"
    assert body["clicks_per_hour"][0] == {"hour": "2025-11-20T19:00:00Z", "count": 1}
    assert len(body["clicks_per_hour"]) == 2
    assert body["total_clicks"] == 2


@pytest.mark.parametrize(
    ("start", "end", "exp_start", "exp_end", "n"),
    [
        ("2025-11-20T15:30:00-05:00", "2025-11-20T17:00:00-05:00",
         "2025-11-20T20:00:00Z", "2025-11-20T22:00:00Z", 2),
        ("2025-11-20T21:00:00+05:30", "2025-11-20T23:00:00+05:30",
         "2025-11-20T15:00:00Z", "2025-11-20T18:00:00Z", 3),
        ("2025-11-20T19:15:00", "2025-11-20T19:45:00",
         "2025-11-20T19:00:00Z", "2025-11-20T20:00:00Z", 1),
        ("2025-11-20T19:00:00Z", "2025-11-20T20:00:00.000001Z",
         "2025-11-20T19:00:00Z", "2025-11-20T21:00:00Z", 2),
    ],
)
def test_hourly_offsets_and_rounding(client, link, start, end, exp_start, exp_end, n):
    """Covers: FR-25, NFR-22"""
    code, _ = link
    resp = _hourly(client, code, start=start, end=end)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["start"], body["end"]) == (exp_start, exp_end)
    assert len(body["clicks_per_hour"]) == n


def test_hourly_counts_only_requested_link(client):
    """Covers: FR-25"""
    a = create_link(client)["code"]
    b = create_link(client)["code"]
    ts = [datetime(2025, 11, 20, 20, 10)] * 3
    _insert_clicks(client, _link_id(client, a), ts)
    _insert_clicks(client, _link_id(client, b), ts + ts)
    body = _hourly(client, a, start="2025-11-20T20:00:00Z", end="2025-11-20T21:00:00Z").json()
    assert body["total_clicks"] == 3
    assert body["clicks_per_hour"] == [{"hour": "2025-11-20T20:00:00Z", "count": 3}]


def test_hourly_default_range_is_25_buckets_ending_with_current_hour(client, link, clock):
    """Covers: FR-25"""
    code, link_id = link
    clock.set(datetime(2025, 11, 20, 20, 37, 12, 345678))
    _insert_clicks(
        client,
        link_id,
        [datetime(2025, 11, 19, 20, 0), datetime(2025, 11, 20, 20, 36),
         datetime(2025, 11, 19, 19, 59, 59), datetime(2025, 11, 20, 21, 0)],
    )
    body = _hourly(client, code).json()
    assert len(body["clicks_per_hour"]) == 25
    assert body["start"] == "2025-11-19T20:00:00Z"
    assert body["end"] == "2025-11-20T21:00:00Z"
    assert body["clicks_per_hour"][0] == {"hour": "2025-11-19T20:00:00Z", "count": 1}
    assert body["clicks_per_hour"][-1] == {"hour": "2025-11-20T20:00:00Z", "count": 1}
    assert body["total_clicks"] == 2


def test_hourly_service_default_range_with_injected_now(client, link):
    """Covers: FR-25"""
    code, _ = link
    with new_session(client) as s:
        stats = StatsService(s).get_hourly_clicks(code, now=datetime(2026, 1, 1, 0, 0, 0))
    assert stats.start == datetime(2025, 12, 31, 0, 0)
    assert stats.end == datetime(2026, 1, 1, 1, 0)
    assert len(stats.clicks_per_hour) == 25
    assert stats.clicks_per_hour[-1] == (datetime(2026, 1, 1, 0, 0), 0)


@pytest.mark.parametrize(
    "params",
    [
        {"start": "2025-11-20T20:00:00Z"},
        {"end": "2025-11-20T20:00:00Z"},
        {"start": "2025-11-20T21:00:00Z", "end": "2025-11-20T20:00:00Z"},
        {"start": "2025-11-20T20:00:00Z", "end": "2025-11-20T20:00:00Z"},
        {"start": "2025-11-20T20:10:00Z", "end": "2025-11-20T20:00:00Z"},
        {"start": "2025-11-01T00:00:00Z", "end": "2025-12-02T01:00:00Z"},
        {"start": "2025-11-01T00:00:00Z", "end": "2025-12-02T00:00:00.000001Z"},
        {"start": "garbage", "end": "2025-11-20T20:00:00Z"},
        {"start": "2025-11-20T20:00:00Z", "end": "2025-11-20T25:00:00Z"},
        {"start": "2025-11-20T20:00:00 01:00", "end": "2025-11-20T22:00:00Z"},
        {"start": "9999-12-31T22:00:00Z", "end": "9999-12-31T23:30:00Z"},
        {"start": "0001-01-01T00:30:00+01:00", "end": "0001-01-01T05:00:00Z"},
    ],
)
def test_hourly_invalid_ranges_return_422(client, link, params):
    """Covers: FR-25, NFR-22"""
    code, _ = link
    resp = _hourly(client, code, **params)
    assert_json_error(resp, 422)


def test_hourly_domain_422_details_are_strings(client, link):
    """Covers: FR-25"""
    code, _ = link
    cases = [
        ({"start": "2025-11-20T20:00:00Z"}, "start and end must be supplied together"),
        ({"start": "2025-11-20T21:00:00Z", "end": "2025-11-20T20:00:00Z"},
         "start must be before end"),
        ({"start": "2025-11-01T00:00:00Z", "end": "2025-12-02T01:00:00Z"},
         "range must not exceed 744 hours"),
    ]
    for params, detail in cases:
        assert _hourly(client, code, **params).json() == {"detail": detail}


def test_hourly_exactly_744_hours_allowed(client, link):
    """Covers: FR-25"""
    code, _ = link
    resp = _hourly(client, code, start="2025-11-01T00:00:00Z", end="2025-12-02T00:00:00Z")
    assert resp.status_code == 200
    assert len(resp.json()["clicks_per_hour"]) == 744


@pytest.mark.parametrize("code", ["Unknown1", "bad-code", "a" * 40])
def test_hourly_unknown_or_malformed_code_404(client, code):
    """Covers: FR-25"""
    resp = _hourly(client, code, start="2025-11-20T20:00:00Z", end="2025-11-20T21:00:00Z")
    assert_json_error(resp, 404)
    assert_json_error(_hourly(client, code), 404)


@pytest.mark.parametrize("bad_headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong-key-0123456789"}])
def test_hourly_requires_api_key(make_client, bad_headers):
    """Covers: FR-25, FR-21, NFR-15"""
    client = make_client(send_api_key=False)
    code = client.post(
        "/api/links", json={"url": "https://example.com/h"}, headers=AUTH_HEADERS
    ).json()["code"]
    resp = client.get(f"/api/links/{code}/stats/hourly", headers=bad_headers)
    assert_json_error(resp, 401)
    assert resp.json() == {"detail": "Invalid or missing API key"}
    assert resp.headers["www-authenticate"] == "ApiKey"


def test_hourly_for_deactivated_and_expired_links(client, clock):
    """Covers: FR-25, FR-22"""
    gone = create_link(client)["code"]
    expired = client.post(
        "/api/links", json={"url": "https://example.com/e", "expires_at": "2025-12-31T23:59:00Z"}
    ).json()["code"]
    for code in (gone, expired):
        _insert_clicks(client, _link_id(client, code), [datetime(2025, 12, 31, 21, 15)] * 2)
    client.delete(f"/api/links/{gone}")
    clock.set(datetime(2026, 1, 2))
    for code in (gone, expired):
        resp = _hourly(client, code, start="2025-12-31T21:00:00Z", end="2025-12-31T22:00:00Z")
        assert resp.status_code == 200
        assert resp.json()["total_clicks"] == 2


def test_hourly_logs_no_ip_and_returns_only_aggregates(client, link, caplog):
    """Covers: NFR-14, FR-25"""
    code, _ = link
    client.get(
        f"/{code}",
        headers={"X-Forwarded-For": "192.0.2.55", "User-Agent": "Hourly-UA/1.0",
                 "Referer": "https://secret-ref.example/x"},
        follow_redirects=False,
    )
    with caplog.at_level(logging.DEBUG):
        resp = client.get(
            f"/api/links/{code}/stats/hourly",
            headers={"X-Forwarded-For": "192.0.2.55", "User-Agent": "Hourly-UA/1.0"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == HOURLY_KEYS
    for entry in body["clicks_per_hour"]:
        assert set(entry) == {"hour", "count"}
    text = resp.text
    for leaked in ("192.0.2.55", "testclient", "Hourly-UA", "secret-ref", "ip_hash", "referrer"):
        assert leaked not in text
    assert "192.0.2.55" not in caplog.text
    assert "testclient" not in caplog.text


def test_hourly_service_validation_unit(client, link):
    """Covers: FR-25"""
    code, _ = link
    with new_session(client) as s:
        svc = StatsService(s)
        with pytest.raises(InvalidInputError):
            svc.get_hourly_clicks(code, start=datetime(2025, 1, 1))
        with pytest.raises(InvalidInputError):
            svc.get_hourly_clicks(code, end=datetime(2025, 1, 1))
        with pytest.raises(InvalidInputError):
            svc.get_hourly_clicks(code, start=datetime(2025, 1, 2), end=datetime(2025, 1, 1))
        with pytest.raises(InvalidInputError):
            svc.get_hourly_clicks(code, start=datetime.max - timedelta(minutes=30),
                                  end=datetime.max)
        # Range is validated before the lookup: an invalid range wins over 404.
        with pytest.raises(InvalidInputError):
            svc.get_hourly_clicks("Unknown1", start=datetime(2025, 1, 2), end=datetime(2025, 1, 1))
        with pytest.raises(NotFoundError):
            svc.get_hourly_clicks("Unknown1", start=datetime(2025, 1, 1), end=datetime(2025, 1, 2))


def test_hourly_repository_returns_only_nonzero_buckets(client, link):
    """Covers: FR-25, NFR-13"""
    code, link_id = link
    _insert_clicks(client, link_id, [datetime(2025, 11, 20, 20, m) for m in range(50)])
    _insert_clicks(client, link_id, [datetime(2025, 11, 20, 23, 1)])
    with new_session(client) as s:
        rows = ClickRepository(s).hourly_counts(
            link_id, datetime(2025, 11, 20, 0), datetime(2025, 11, 21, 0)
        )
    assert rows == [(datetime(2025, 11, 20, 20), 50), (datetime(2025, 11, 20, 23), 1)]


# --------------------------------------------------------------------------- dialects


@pytest.mark.parametrize(
    ("dialect", "marker"),
    [
        (sqlite.dialect(),
         "strftime('%Y-%m-%d %H:00:00', substr(click_events.created_at, 1, 19))"),
        (postgresql.dialect(), "date_trunc('hour', click_events.created_at)"),
        (mysql.dialect(), "timestampadd(HOUR, hour(click_events.created_at), "
                          "date(click_events.created_at))"),
    ],
)
def test_hour_bucket_compiles_for_all_dialects(dialect, marker):
    """Covers: FR-25"""
    stmt = build_hourly_counts_stmt(1, datetime(2025, 1, 1), datetime(2025, 1, 2), dialect.name)
    compiled = stmt.compile(dialect=dialect)
    sql = " ".join(str(compiled).split())
    assert marker.lower() in sql.lower()
    assert "GROUP BY" in sql
    assert "click_events.created_at >=" in sql
    assert "click_events.created_at <" in sql
    # The bucket expression carries no bound parameters: only link_id, start, end.
    assert len(compiled.params) == 3
    bucket_sql = str(hour_bucket_expression(dialect.name).compile(dialect=dialect))
    assert hour_bucket_expression(dialect.name).compile(dialect=dialect).params == {}
    assert bucket_sql


def test_hourly_query_uses_link_created_at_index(client):
    """Covers: NFR-21, NFR-13"""
    stmt = build_hourly_counts_stmt(1, datetime(2025, 1, 1), datetime(2025, 1, 2), "sqlite")
    compiled_sql = " ".join(str(stmt.compile(dialect=sqlite.dialect())).split())
    literal_sql = (
        "SELECT strftime('%Y-%m-%d %H:00:00', substr(click_events.created_at, 1, 19)) "
        "AS bucket, count(click_events.id) AS count_1 FROM click_events "
        "WHERE click_events.link_id = ? AND click_events.created_at >= ? "
        "AND click_events.created_at < ? "
        "GROUP BY strftime('%Y-%m-%d %H:00:00', substr(click_events.created_at, 1, 19)) "
        "ORDER BY bucket"
    )
    assert compiled_sql == literal_sql
    with client.app.state.engine.connect() as conn:
        plan = conn.exec_driver_sql(
            "EXPLAIN QUERY PLAN "
            "SELECT strftime('%Y-%m-%d %H:00:00', substr(click_events.created_at, 1, 19)) "
            "AS bucket, count(click_events.id) AS count_1 FROM click_events "
            "WHERE click_events.link_id = ? AND click_events.created_at >= ? "
            "AND click_events.created_at < ? "
            "GROUP BY strftime('%Y-%m-%d %H:00:00', substr(click_events.created_at, 1, 19)) "
            "ORDER BY bucket",
            (1, "2025-01-01 00:00:00.000000", "2025-01-02 00:00:00.000000"),
        ).all()
    details = " ".join(str(row[-1]) for row in plan)
    assert "ix_click_events_link_id_created_at" in details
    assert "link_id=?" in details and "created_at>?" in details and "created_at<?" in details


def test_openapi_documents_hourly_endpoint(client):
    """Covers: FR-25"""
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/api/links/{code}/stats/hourly"]["get"]
    ok = op["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/LinkHourlyStatsResponse")
    assert {"401", "404", "422"} <= set(op["responses"])
    assert {p["name"] for p in op["parameters"]} >= {"code", "start", "end"}
    assert {"ApiKeyAuth": []} in op["security"]
    schemas = spec["components"]["schemas"]
    assert set(schemas["LinkHourlyStatsResponse"]["properties"]) == HOURLY_KEYS
    assert set(schemas["HourlyClicks"]["properties"]) == {"hour", "count"}
    assert set(schemas["LinkStatsResponse"]["properties"]) == {
        "code", "total_clicks", "clicks_per_day", "top_referrers",
    }


# --------------------------------------------------------------------------- scale


def _seed_bulk(client, link_id: int, n: int, start: datetime, hours: int):
    rows = [
        {"link_id": link_id, "created_at": start + timedelta(seconds=(i * 3600 * hours) // n)}
        for i in range(n)
    ]
    with new_session(client) as s:
        s.execute(insert(ClickEvent), rows)
        s.commit()


def _measure(client, code, params):
    client.get(f"/api/links/{code}/stats/hourly", params=params)  # warm-up
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        resp = client.get(f"/api/links/{code}/stats/hourly", params=params)
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return resp, elapsed, peak


@pytest.mark.slow
def test_hourly_200k_clicks_fast_and_bounded_memory(client):
    """Covers: NFR-21, NFR-13, FR-25"""
    start = datetime(2025, 11, 1)
    params = {"start": "2025-11-01T00:00:00Z", "end": "2025-12-02T00:00:00Z"}
    small = create_link(client)["code"]
    big = create_link(client)["code"]
    _seed_bulk(client, _link_id(client, small), 2_000, start, 744)
    _seed_bulk(client, _link_id(client, big), 200_000, start, 744)

    small_resp, _, small_peak = _measure(client, small, params)
    big_resp, big_elapsed, big_peak = _measure(client, big, params)
    assert small_resp.status_code == 200
    assert small_resp.json()["total_clicks"] == 2_000
    assert big_resp.status_code == 200
    assert big_resp.json()["total_clicks"] == 200_000
    assert len(big_resp.json()["clicks_per_hour"]) == 744
    assert big_elapsed < 2.0, big_elapsed
    assert big_peak <= 2 * small_peak, (big_peak, small_peak)
