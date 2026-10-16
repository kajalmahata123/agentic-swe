"""Tests for per-link stats (FR-9)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest
from app.models import ClickEvent, Link
from app.services.stats_service import StatsService
from helpers import assert_json_error, create_link, new_session


def _link_id(client, code: str) -> int:
    with new_session(client) as s:
        return s.query(Link).filter_by(code=code).one().id


def _insert_clicks(client, link_id: int, rows):
    """rows: iterable of (created_at naive UTC, referrer)."""
    with new_session(client) as s:
        for ts, ref in rows:
            s.add(ClickEvent(link_id=link_id, created_at=ts, referrer=ref))
        s.commit()


def test_stats_shape_and_empty_link(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    resp = client.get(f"/api/links/{code}/stats")
    assert resp.status_code == 200
    assert resp.json() == {
        "code": code,
        "total_clicks": 0,
        "clicks_per_day": [],
        "top_referrers": [],
    }


def test_stats_after_redirects(client):
    """Covers: FR-9, FR-8"""
    code = create_link(client)["code"]
    for _ in range(3):
        client.get(f"/{code}", headers={"Referer": "https://a.example"}, follow_redirects=False)
    client.get(f"/{code}", follow_redirects=False)
    body = client.get(f"/api/links/{code}/stats").json()
    assert body["code"] == code
    assert body["total_clicks"] == 4
    today = datetime.now(UTC).date().isoformat()
    assert body["clicks_per_day"] == [{"date": today, "count": 4}]
    assert body["top_referrers"] == [
        {"referrer": "https://a.example", "count": 3},
        {"referrer": None, "count": 1},
    ]
    for entry in body["clicks_per_day"]:
        assert isinstance(entry["count"], int)
        date.fromisoformat(entry["date"])


def test_day_bucketing_across_utc_midnight(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    now = datetime.now(UTC).replace(tzinfo=None)
    midnight = datetime.combine(now.date(), time.min)
    yesterday = (midnight - timedelta(days=1)).date()
    _insert_clicks(
        client,
        link_id,
        [
            (midnight - timedelta(seconds=1), None),  # 23:59:59 yesterday
            (midnight - timedelta(seconds=1), None),
            (midnight - timedelta(hours=23, minutes=59), None),  # 00:01 yesterday
            (midnight + timedelta(seconds=1), None),  # 00:00:01 today
        ],
    )
    body = client.get(f"/api/links/{code}/stats").json()
    assert body["total_clicks"] == 4
    assert body["clicks_per_day"] == [
        {"date": yesterday.isoformat(), "count": 3},
        {"date": now.date().isoformat(), "count": 1},
    ]


def test_clicks_per_day_sorted_and_windowed(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    today = datetime.combine(datetime.now(UTC).date(), time(12, 0))
    _insert_clicks(
        client,
        link_id,
        [
            (today - timedelta(days=2), None),
            (today, None),
            (today - timedelta(days=10), None),
            (today - timedelta(days=29), None),  # inside default 30-day window
            (today - timedelta(days=30), None),  # outside default window
            (today - timedelta(days=100), None),
            (today - timedelta(days=2), None),
        ],
    )
    body = client.get(f"/api/links/{code}/stats").json()
    assert body["total_clicks"] == 7  # all-time
    dates = [d["date"] for d in body["clicks_per_day"]]
    assert dates == sorted(dates)
    expected = {
        (today - timedelta(days=29)).date().isoformat(): 1,
        (today - timedelta(days=10)).date().isoformat(): 1,
        (today - timedelta(days=2)).date().isoformat(): 2,
        today.date().isoformat(): 1,
    }
    assert {d["date"]: d["count"] for d in body["clicks_per_day"]} == expected

    one_day = client.get(f"/api/links/{code}/stats", params={"days": 1}).json()
    assert one_day["clicks_per_day"] == [{"date": today.date().isoformat(), "count": 1}]
    assert one_day["total_clicks"] == 7

    year = client.get(f"/api/links/{code}/stats", params={"days": 365}).json()
    assert sum(d["count"] for d in year["clicks_per_day"]) == 7
    assert len(year["clicks_per_day"]) == 6


@pytest.mark.parametrize("days", ["0", "366", "-1", "abc", "1.5"])
def test_days_out_of_range_returns_422(client, days):
    """Covers: FR-9, FR-11"""
    code = create_link(client)["code"]
    resp = client.get(f"/api/links/{code}/stats", params={"days": days})
    assert_json_error(resp, 422)


def test_top_referrers_ordering_and_null_grouping(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = (
        [(now, "https://b.example")] * 3
        + [(now, "https://a.example")] * 3
        + [(now, None)] * 2
        + [(now, "https://c.example")] * 5
        + [(now, "https://d.example")] * 1
    )
    _insert_clicks(client, link_id, rows)
    body = client.get(f"/api/links/{code}/stats").json()
    assert body["top_referrers"] == [
        {"referrer": "https://c.example", "count": 5},
        {"referrer": "https://a.example", "count": 3},
        {"referrer": "https://b.example", "count": 3},
        {"referrer": None, "count": 2},
        {"referrer": "https://d.example", "count": 1},
    ]


def test_top_referrers_limited_to_10(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for i in range(15):
        rows += [(now, f"https://r{i:02d}.example")] * (i + 1)
    _insert_clicks(client, link_id, rows)
    body = client.get(f"/api/links/{code}/stats").json()
    refs = body["top_referrers"]
    assert len(refs) == 10
    assert [r["count"] for r in refs] == list(range(15, 5, -1))
    assert refs[0]["referrer"] == "https://r14.example"
    assert body["total_clicks"] == sum(range(1, 16))


def test_stats_available_for_deactivated_link(client):
    """Covers: FR-9, FR-7"""
    code = create_link(client)["code"]
    client.get(f"/{code}", follow_redirects=False)
    client.delete(f"/api/links/{code}")
    resp = client.get(f"/api/links/{code}/stats")
    assert resp.status_code == 200
    assert resp.json()["total_clicks"] == 1


def test_stats_unknown_code_returns_404(client):
    """Covers: FR-9, FR-11"""
    assert_json_error(client.get("/api/links/Nothing/stats"), 404)
    assert_json_error(client.get("/api/links/bad-code/stats"), 404)


def test_stats_counts_only_own_link(client):
    """Covers: FR-9"""
    a = create_link(client)["code"]
    b = create_link(client)["code"]
    client.get(f"/{a}", follow_redirects=False)
    client.get(f"/{a}", follow_redirects=False)
    client.get(f"/{b}", follow_redirects=False)
    assert client.get(f"/api/links/{a}/stats").json()["total_clicks"] == 2
    assert client.get(f"/api/links/{b}/stats").json()["total_clicks"] == 1


def test_stats_service_with_fixed_now_buckets_by_utc_date(client):
    """Covers: FR-9"""
    code = create_link(client)["code"]
    link_id = _link_id(client, code)
    _insert_clicks(
        client,
        link_id,
        [
            (datetime(2024, 3, 9, 23, 59, 59), "x"),
            (datetime(2024, 3, 10, 0, 0, 1), "x"),
            (datetime(2024, 3, 10, 0, 0, 0), None),
            (datetime(2024, 2, 1, 12, 0, 0), None),
        ],
    )
    with new_session(client) as s:
        svc = StatsService(s)
        stats = svc.get_stats(code, days=30, now=datetime(2024, 3, 10, 12, 0, 0))
        assert stats.total_clicks == 4
        assert stats.clicks_per_day == [(date(2024, 3, 9), 1), (date(2024, 3, 10), 2)]
        assert stats.top_referrers == [("x", 2), (None, 2)]

        one = svc.get_stats(code, days=1, now=datetime(2024, 3, 10, 12, 0, 0))
        assert one.clicks_per_day == [(date(2024, 3, 10), 2)]

        # A tz-aware "now" is converted to UTC: 2024-03-10 01:00+02:00 == 03-09 23:00Z.
        from datetime import timezone

        aware = datetime(2024, 3, 10, 1, 0, 0, tzinfo=timezone(timedelta(hours=2)))
        tz_stats = svc.get_stats(code, days=1, now=aware)
        assert (date(2024, 3, 9), 1) in tz_stats.clicks_per_day
