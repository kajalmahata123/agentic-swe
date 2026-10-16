"""Bounded-memory daily stats aggregation (FR-18, NFR-13)."""

from __future__ import annotations

import time
import tracemalloc
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime

import pytest
from app.models import ClickEvent, Link
from app.repositories.click_repository import (
    ClickRepository,
    build_daily_counts_stmt,
    day_bucket_expression,
)
from app.services.stats_service import StatsService
from helpers import create_link, new_session
from sqlalchemy import insert
from sqlalchemy.dialects import mysql, postgresql, sqlite


def _link_id(client, code: str) -> int:
    with new_session(client) as s:
        return s.query(Link).filter_by(code=code).one().id


def _insert(client, link_id, timestamps, referrer=None):
    with new_session(client) as s:
        for ts in timestamps:
            s.add(ClickEvent(link_id=link_id, created_at=ts, referrer=referrer))
        s.commit()


def test_repository_returns_one_row_per_day_not_per_click(client):
    """Covers: FR-18, NFR-13"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    _insert(client, link_id, [datetime(2024, 3, 9, 12, m) for m in range(40)])
    _insert(client, link_id, [datetime(2024, 3, 10, 0, 0, s) for s in range(25)])
    _insert(client, link_id, [datetime(2024, 3, 1, 5)])
    with new_session(client) as s:
        repo = ClickRepository(s)
        rows = repo.daily_counts(link_id, datetime(2024, 3, 5))
    assert rows == [(date(2024, 3, 9), 40), (date(2024, 3, 10), 25)]
    for d, c in rows:
        assert type(d) is date and type(c) is int


def test_per_click_timestamp_api_removed():
    """Covers: FR-18, NFR-13"""
    assert not hasattr(ClickRepository, "created_at_since")


def test_stats_service_does_not_materialise_click_rows(client, monkeypatch):
    """Covers: FR-18, NFR-13"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    now = datetime.now(UTC).replace(tzinfo=None)
    _insert(client, link_id, [now] * 30)
    with new_session(client) as s:
        loaded: list[object] = []
        from sqlalchemy import event

        @event.listens_for(s, "loaded_as_persistent")
        def _track(session, instance):
            loaded.append(instance)

        stats = StatsService(s).get_stats(code, days=30)
    assert stats.total_clicks == 30
    assert not any(isinstance(o, ClickEvent) for o in loaded)


def test_empty_referrer_grouped_as_null(client):
    """Covers: FR-18, FR-21"""
    code = create_link(client)["code"]
    client.get(f"/{code}", headers={"Referer": ""}, follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)
    client.get(f"/{code}", headers={"Referer": "https://r.example"}, follow_redirects=False)
    body = client.get(f"/api/links/{code}/stats").json()
    assert body["top_referrers"] == [
        {"referrer": None, "count": 2},
        {"referrer": "https://r.example", "count": 1},
    ]


def test_null_referrer_sorted_last_on_ties(client):
    """Covers: FR-18"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    now = datetime.now(UTC).replace(tzinfo=None)
    _insert(client, link_id, [now] * 2, referrer=None)
    _insert(client, link_id, [now] * 2, referrer="https://z.example")
    _insert(client, link_id, [now] * 2, referrer="https://a.example")
    refs = client.get(f"/api/links/{code}/stats").json()["top_referrers"]
    assert [r["referrer"] for r in refs] == ["https://a.example", "https://z.example", None]


def test_daily_window_edges_with_fixed_now(client):
    """Covers: FR-18"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    _insert(
        client,
        link_id,
        [datetime(2024, 3, 4, 23, 59, 59), datetime(2024, 3, 5, 0, 0, 0),
         datetime(2024, 3, 11, 23, 59, 59), datetime(2024, 3, 12, 0, 0, 0)],
    )
    with new_session(client) as s:
        stats = StatsService(s).get_stats(code, days=7, now=datetime(2024, 3, 11, 8, 0))
    # Window: 2024-03-05 .. 2024-03-11 inclusive; future clicks after "now" are still
    # bucketed as before (the window only has a lower bound).
    assert stats.clicks_per_day[0] == (date(2024, 3, 5), 1)
    assert (date(2024, 3, 4), 1) not in stats.clicks_per_day
    assert (date(2024, 3, 11), 1) in stats.clicks_per_day
    assert stats.total_clicks == 4


def test_daily_bucketing_exact_for_sub_millisecond_timestamps(client):
    """Covers: FR-18, NFR-22

    A click stored at 23:59:59.999999 (microsecond precision from utcnow()) belongs to
    that UTC day, not the next one.
    """
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    _insert(client, link_id, [datetime(2024, 3, 9, 23, 59, 59, 999999)])
    with new_session(client) as s:
        stats = StatsService(s).get_stats(code, days=30, now=datetime(2024, 3, 10, 12, 0))
    assert stats.clicks_per_day == [(date(2024, 3, 9), 1)]


def test_daily_bucketing_exact_at_year_end_sub_millisecond(client):
    """Covers: FR-18, NFR-22

    Regression: SQLite's date() maps '2025-12-31 23:59:59.999999' to 2026-01-01 on
    some versions; the bucket must be the stored UTC day.
    """
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    _insert(
        client,
        link_id,
        [datetime(2025, 12, 31, 23, 59, 59, 999999), datetime(2025, 12, 31, 23, 59, 59, 999500),
         datetime(2026, 1, 1, 0, 0, 0)],
    )
    with new_session(client) as s:
        stats = StatsService(s).get_stats(code, days=30, now=datetime(2026, 1, 1, 12, 0))
    assert stats.clicks_per_day == [(date(2025, 12, 31), 2), (date(2026, 1, 1), 1)]


def test_daily_stats_for_expired_link(client, clock):
    """Covers: FR-18, FR-22"""
    code = client.post(
        "/api/links", json={"url": "https://example.com/x", "expires_at": "2025-12-31T23:59:00Z"}
    ).json()["code"]
    client.get(f"/{code}", follow_redirects=False)
    clock.set(datetime(2026, 1, 1))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410
    resp = client.get(f"/api/links/{code}/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_clicks"] == 1
    assert sum(d["count"] for d in body["clicks_per_day"]) == 1
    assert set(body) == {"code", "total_clicks", "clicks_per_day", "top_referrers"}


@pytest.mark.parametrize(
    ("dialect", "marker"),
    [
        (sqlite.dialect(), "date(substr(click_events.created_at, 1, 19))"),
        (postgresql.dialect(), "CAST(click_events.created_at AS DATE)"),
        (mysql.dialect(), "date(click_events.created_at)"),
    ],
)
def test_daily_bucket_compiles_for_all_dialects(dialect, marker):
    """Covers: FR-18"""
    stmt = build_daily_counts_stmt(1, datetime(2025, 1, 1), dialect.name)
    compiled = stmt.compile(dialect=dialect)
    sql = " ".join(str(compiled).split())
    assert marker.lower() in sql.lower()
    assert "GROUP BY" in sql and "ORDER BY" in sql
    assert "count(click_events.id)" in sql.lower()
    assert len(compiled.params) == 2
    assert day_bucket_expression(dialect.name).compile(dialect=dialect).params == {}


# --------------------------------------------------------------------------- scale


def _seed(client, link_id, n, days):
    today = datetime.combine(datetime.now(UTC).date(), dtime(0, 0))
    first = today - timedelta(days=days - 1)
    span = days * 86400 - 1
    rows = [
        {"link_id": link_id, "created_at": first + timedelta(seconds=(i * span) // n),
         "referrer": f"https://r{i % 7}.example"}
        for i in range(n)
    ]
    with new_session(client) as s:
        s.execute(insert(ClickEvent), rows)
        s.commit()


def _measure(client, code):
    client.get(f"/api/links/{code}/stats", params={"days": 365})
    tracemalloc.start()
    try:
        t0 = time.perf_counter()
        resp = client.get(f"/api/links/{code}/stats", params={"days": 365})
        elapsed = time.perf_counter() - t0
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return resp, elapsed, peak


@pytest.mark.slow
def test_daily_stats_200k_clicks_fast_and_bounded_memory(client):
    """Covers: FR-18, NFR-13"""
    small = create_link(client)["code"]
    big = create_link(client)["code"]
    _seed(client, _link_id(client, small), 2_000, 300)
    _seed(client, _link_id(client, big), 200_000, 300)
    small_resp, _, small_peak = _measure(client, small)
    big_resp, big_elapsed, big_peak = _measure(client, big)
    assert small_resp.status_code == 200
    assert big_resp.status_code == 200
    body = big_resp.json()
    assert body["total_clicks"] == 200_000
    assert sum(d["count"] for d in body["clicks_per_day"]) == 200_000
    assert len(body["clicks_per_day"]) == 300
    assert big_elapsed < 2.0, big_elapsed
    assert big_peak <= 2 * small_peak, (big_peak, small_peak)
