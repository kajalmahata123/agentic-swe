"""CR-142 link expiration: validation, 410 on redirect, metadata, OpenAPI (FR-5..FR-7, FR-13)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from app.models import ClickEvent, Link
from helpers import assert_json_error, count_rows, new_session
from sqlalchemy import event, select, update

URL = "https://example.com/exp"
NOW = datetime(2029, 6, 1, 12, 0, 0)  # controlled clock (naive UTC)
EXP = "2030-01-01T12:00:00Z"
EXP_DT = datetime(2030, 1, 1, 12, 0, 0)


@pytest.fixture
def clocked(client):
    client.app.state.clock = lambda: NOW
    return client


def _create(client, **fields):
    resp = client.post("/api/links", json={"url": URL, **fields})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set_clock(client, value):
    client.app.state.clock = lambda: value


# ---------------------------------------------------------------- FR-5


@pytest.mark.parametrize(
    "value", ["2030-01-01T12:00:00Z", "2030-01-01T14:00:00+02:00", "2030-01-01T12:00:00",
              "2030-01-01T07:00:00-05:00", "2030-01-01 12:00"]
)
def test_expires_at_normalised_to_utc(clocked, value):
    """Covers: FR-5, FR-7"""
    body = _create(clocked, expires_at=value)
    assert body["expires_at"] == "2030-01-01T12:00:00Z"
    assert body["is_expired"] is False
    with new_session(clocked) as s:
        assert s.scalar(select(Link.expires_at)) == EXP_DT
    meta = clocked.get(f"/api/links/{body['code']}").json()
    assert meta["expires_at"] == "2030-01-01T12:00:00Z"


def test_relative_future_expiry_with_real_clock(client):
    """Covers: FR-5"""
    exp = datetime.now(UTC) + timedelta(hours=1)
    body = _create(client, expires_at=exp.isoformat())
    assert body["is_expired"] is False
    assert body["expires_at"].endswith("Z")


@pytest.mark.parametrize(
    "value", ["2029-06-01T12:00:00Z", "2029-06-01T11:59:59Z", "2000-01-01T00:00:00Z",
              "2029-06-01T14:00:00+02:00"]
)
def test_expires_at_not_in_future_returns_422(clocked, value):
    """Covers: FR-5, NFR-2"""
    resp = clocked.post("/api/links", json={"url": URL, "expires_at": value})
    assert_json_error(resp, 422)
    assert "future" in str(resp.json()["detail"]).lower()
    assert set(resp.json()) == {"detail"}
    assert "Traceback" not in resp.text
    assert count_rows(clocked, Link) == 0


def test_past_expiry_with_real_clock_returns_422(client):
    """Covers: FR-5"""
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    assert_json_error(client.post("/api/links", json={"url": URL, "expires_at": past}), 422)
    assert count_rows(client, Link) == 0


@pytest.mark.parametrize(
    "value", [1893456000, "1893456000", "tomorrow", "2030-13-01T00:00:00Z", "", True, 1.5,
              [], {}, "2030-01-01", "2030-02-30T00:00:00Z"]
)
def test_invalid_expires_at_returns_422(clocked, value):
    """Covers: FR-5, NFR-2"""
    resp = clocked.post("/api/links", json={"url": URL, "expires_at": value})
    assert_json_error(resp, 422)
    assert "Traceback" not in resp.text
    assert count_rows(clocked, Link) == 0


@pytest.mark.parametrize("fields", [{}, {"expires_at": None}])
def test_no_expiry_never_expires(clocked, fields):
    """Covers: FR-5, FR-7"""
    body = _create(clocked, **fields)
    assert body["expires_at"] is None
    assert body["is_expired"] is False
    with new_session(clocked) as s:
        assert s.scalar(select(Link.expires_at)) is None
    _set_clock(clocked, datetime(2999, 1, 1))
    assert clocked.get(f"/{body['code']}", follow_redirects=False).status_code == 307
    assert clocked.get(f"/api/links/{body['code']}").json()["is_expired"] is False


def test_expiry_combined_with_alias(clocked):
    """Covers: FR-5, FR-1"""
    body = _create(clocked, alias="soon-gone", expires_at=EXP)
    assert body["code"] == "soon-gone"
    assert body["expires_at"] == EXP
    with new_session(clocked) as s:
        link = s.scalar(select(Link))
        assert (link.code, link.expires_at) == ("soon-gone", EXP_DT)


def test_aware_clock_is_supported(client):
    """Covers: FR-5, FR-6"""
    client.app.state.clock = lambda: datetime(2030, 1, 1, 14, 0, tzinfo=UTC) - timedelta(hours=3)
    code = _create(client, expires_at=EXP)["code"]
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    client.app.state.clock = lambda: datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


def test_monkeypatched_utcnow_is_used_by_default_clock(client, monkeypatch):
    """Covers: FR-6, NFR-5"""
    import app.services.link_service as ls

    code = _create(client, expires_at=EXP)["code"]
    monkeypatch.setattr(ls, "utcnow", lambda: EXP_DT + timedelta(seconds=1))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410


# ---------------------------------------------------------------- FR-6


def test_expired_link_get_returns_410_without_location_or_click(clocked):
    """Covers: FR-6, NFR-2"""
    code = _create(clocked, expires_at=EXP)["code"]
    assert clocked.get(f"/{code}", follow_redirects=False).status_code == 307
    assert count_rows(clocked, ClickEvent) == 1

    _set_clock(clocked, EXP_DT + timedelta(minutes=1))
    resp = clocked.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 410
    assert resp.json() == {"detail": "Link has expired"}
    assert "location" not in resp.headers
    assert "Traceback" not in resp.text
    assert count_rows(clocked, ClickEvent) == 1


def test_expired_link_head_returns_410_no_click(clocked):
    """Covers: FR-6"""
    code = _create(clocked, expires_at=EXP)["code"]
    _set_clock(clocked, EXP_DT + timedelta(days=1))
    resp = clocked.head(f"/{code}", follow_redirects=False)
    assert resp.status_code == 410
    assert "location" not in resp.headers
    assert count_rows(clocked, ClickEvent) == 0


def test_exact_expiry_instant_counts_as_expired(clocked):
    """Covers: FR-6"""
    code = _create(clocked, expires_at=EXP)["code"]
    _set_clock(clocked, EXP_DT - timedelta(microseconds=1))
    assert clocked.get(f"/{code}", follow_redirects=False).status_code == 307
    _set_clock(clocked, EXP_DT)
    resp = clocked.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 410
    assert resp.json()["detail"] == "Link has expired"
    assert clocked.head(f"/{code}").status_code == 410


def test_future_expiry_redirects_and_records_click(clocked):
    """Covers: FR-6, FR-12"""
    code = _create(clocked, expires_at=EXP)["code"]
    resp = clocked.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == URL
    assert resp.headers["cache-control"] == "no-store"
    assert count_rows(clocked, ClickEvent) == 1


def test_expired_via_direct_db_update(client):
    """Covers: FR-6"""
    code = _create(client)["code"]
    with new_session(client) as s:
        s.execute(update(Link).values(expires_at=datetime(2001, 1, 1)))
        s.commit()
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 410
    assert resp.json()["detail"] == "Link has expired"


def test_deactivated_and_expired_returns_deactivated_detail(clocked):
    """Covers: FR-6"""
    code = _create(clocked, expires_at=EXP)["code"]
    assert clocked.delete(f"/api/links/{code}").status_code == 204
    _set_clock(clocked, EXP_DT + timedelta(days=1))
    for method in ("GET", "HEAD"):
        resp = clocked.request(method, f"/{code}", follow_redirects=False)
        assert resp.status_code == 410
        assert "location" not in resp.headers
    assert clocked.get(f"/{code}", follow_redirects=False).json() == {
        "detail": "Link has been deactivated"
    }
    assert count_rows(clocked, ClickEvent) == 0


def test_deactivated_not_expired_410_unchanged(clocked):
    """Covers: FR-6, FR-12"""
    code = _create(clocked)["code"]
    clocked.delete(f"/api/links/{code}")
    resp = clocked.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 410
    assert resp.json() == {"detail": "Link has been deactivated"}


def test_management_endpoints_work_for_expired_links(clocked):
    """Covers: FR-6, FR-7"""
    code = _create(clocked, alias="exp-mgmt", expires_at=EXP)["code"]
    clocked.get(f"/{code}", follow_redirects=False)
    clocked.get(f"/{code}", follow_redirects=False, headers={"Referer": "https://ref.example/"})
    _set_clock(clocked, EXP_DT + timedelta(hours=1))

    stats = clocked.get(f"/api/links/{code}/stats")
    assert stats.status_code == 200
    assert stats.json()["total_clicks"] == 2

    meta = clocked.get(f"/api/links/{code}")
    assert meta.status_code == 200
    m = meta.json()
    assert m["is_expired"] is True
    assert m["is_active"] is True
    assert m["total_clicks"] == 2

    assert clocked.delete(f"/api/links/{code}").status_code == 204
    m2 = clocked.get(f"/api/links/{code}").json()
    assert m2["is_active"] is False
    assert m2["is_expired"] is True
    assert m2["deactivated_at"] is not None


# ---------------------------------------------------------------- FR-7


def test_metadata_contains_new_and_existing_fields(clocked):
    """Covers: FR-7, FR-12"""
    created = _create(clocked, expires_at=EXP)
    assert {"code", "short_url", "url", "is_active", "created_at", "expires_at",
            "is_expired"} <= set(created)
    meta = clocked.get(f"/api/links/{created['code']}").json()
    for field in ("code", "short_url", "url", "is_active", "created_at", "deactivated_at",
                  "total_clicks", "expires_at", "is_expired"):
        assert field in meta
    assert meta["code"] == created["code"]
    assert meta["url"] == URL
    assert meta["short_url"] == created["short_url"]
    assert meta["created_at"] == created["created_at"]
    assert meta["created_at"].endswith("Z")
    assert meta["is_active"] is True
    assert meta["deactivated_at"] is None
    assert meta["total_clicks"] == 0
    assert meta["is_expired"] is False


def test_url_only_create_response_is_superset_of_old(client):
    """Covers: FR-12, FR-7"""
    body = _create(client)
    assert set(body) == {"code", "short_url", "url", "is_active", "created_at", "expires_at",
                         "is_expired"}
    assert body["expires_at"] is None and body["is_expired"] is False
    assert body["is_active"] is True


def test_openapi_documents_new_fields_and_statuses(client):
    """Covers: FR-7, FR-13"""
    spec = client.get("/openapi.json").json()
    schemas = spec["components"]["schemas"]
    for name in ("LinkCreate", "LinkCreatedResponse", "LinkDetailResponse"):
        assert name in schemas
    assert {"alias", "expires_at"} <= set(schemas["LinkCreate"]["properties"])
    for name in ("LinkCreatedResponse", "LinkDetailResponse"):
        assert {"expires_at", "is_expired"} <= set(schemas[name]["properties"])

    post = spec["paths"]["/api/links"]["post"]["responses"]
    assert "409" in post and "422" in post
    redirect = spec["paths"]["/{code}"]
    assert "410" in redirect["get"]["responses"]
    assert "head" in redirect
    assert "410" in redirect["head"]["responses"]


# ---------------------------------------------------------------- NFR-1


def _count_selects(client, path):
    engine = client.app.state.engine
    statements: list[str] = []

    def listener(conn, cursor, statement, params, context, executemany):
        statements.append(statement.strip().upper())

    event.listen(engine, "before_cursor_execute", listener)
    try:
        status = client.get(path, follow_redirects=False).status_code
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return status, [s for s in statements if s.startswith("SELECT")]


def test_expiry_check_adds_no_queries(clocked):
    """Covers: NFR-1"""
    active = _create(clocked)["code"]
    expiring = _create(clocked, expires_at=EXP)["code"]
    deact = _create(clocked)["code"]
    clocked.delete(f"/api/links/{deact}")
    _set_clock(clocked, EXP_DT + timedelta(days=1))

    s_active, q_active = _count_selects(clocked, f"/{active}")
    s_exp, q_exp = _count_selects(clocked, f"/{expiring}")
    s_deact, q_deact = _count_selects(clocked, f"/{deact}")
    assert (s_active, s_exp, s_deact) == (307, 410, 410)
    assert len(q_exp) == len(q_deact) == len(q_active) == 1
