"""Link expiry: enforcement on redirect (FR-22), expiry on create (FR-23), PATCH
expiry (FR-24) and timezone-safe time handling (NFR-22).

The service clock is frozen with the ``clock`` fixture (see conftest), because the
campaign instants used here (2025-12-31T23:59:00Z) must be compared to a controlled
"now", not the wall clock.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timedelta, timezone

import pytest
from app.core.errors import GoneError, InvalidInputError, NotFoundError
from app.models import ClickEvent, IdempotencyKey, Link
from app.services.link_service import LinkService
from helpers import AUTH_HEADERS, assert_json_error, count_rows, create_link, new_session

CAMPAIGN_END = "2025-12-31T23:59:00Z"
CAMPAIGN_END_DT = datetime(2025, 12, 31, 23, 59, 0)
EXPIRED_DETAIL = {"detail": "Link has expired"}
DEACTIVATED_DETAIL = {"detail": "Link has been deactivated"}


def _create(client, url="https://example.com/campaign", expires_at=CAMPAIGN_END, **kw):
    body = {"url": url}
    if expires_at is not ...:
        body["expires_at"] = expires_at
    return client.post("/api/links", json=body, **kw)


def _clicks(client) -> int:
    return count_rows(client, ClickEvent)


def _stored(client, code) -> Link:
    with new_session(client) as s:
        link = s.query(Link).filter_by(code=code).one()
        s.expunge(link)
        return link


def _assert_expired_response(resp):
    assert resp.status_code == 410
    assert "location" not in resp.headers
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers["content-type"].startswith("application/json")


# =========================================================================== FR-22


def test_expiry_boundary_with_injected_clock(client, clock):
    """Covers: FR-22, NFR-22, NFR-15"""
    url = "https://example.com/tv"
    code = _create(client, url).json()["code"]

    clock.set(datetime(2025, 12, 31, 23, 58, 59, 999999))
    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == url
    assert _clicks(client) == 1
    assert client.head(f"/{code}", follow_redirects=False).status_code == 307
    assert _clicks(client) == 1

    clock.set(CAMPAIGN_END_DT)
    get_resp = client.get(f"/{code}", follow_redirects=False)
    _assert_expired_response(get_resp)
    assert get_resp.json() == EXPIRED_DETAIL
    head_resp = client.head(f"/{code}", follow_redirects=False)
    _assert_expired_response(head_resp)
    assert _clicks(client) == 1

    clock.set(CAMPAIGN_END_DT + timedelta(seconds=1))
    for method in ("get", "head"):
        _assert_expired_response(getattr(client, method)(f"/{code}", follow_redirects=False))
    assert client.get(f"/{code}", follow_redirects=False).json() == EXPIRED_DETAIL

    clock.set(datetime(2030, 6, 1))
    _assert_expired_response(client.get(f"/{code}", follow_redirects=False))
    assert _clicks(client) == 1


def test_expiry_checked_every_request_never_cached(client, clock):
    """Covers: FR-22, NFR-22"""
    code = _create(client).json()["code"]
    for now, status in (
        (CAMPAIGN_END_DT - timedelta(microseconds=1), 307),
        (CAMPAIGN_END_DT, 410),
        (CAMPAIGN_END_DT - timedelta(microseconds=1), 307),
        (CAMPAIGN_END_DT + timedelta(microseconds=1), 410),
    ):
        clock.set(now)
        assert client.get(f"/{code}", follow_redirects=False).status_code == status
        assert client.head(f"/{code}", follow_redirects=False).status_code == status


def test_deactivated_and_expired_returns_deactivated_detail(client, clock):
    """Covers: FR-22"""
    code = _create(client).json()["code"]
    assert client.delete(f"/api/links/{code}").status_code == 204
    clock.set(datetime(2026, 2, 1))
    for method in ("get", "head"):
        resp = getattr(client, method)(f"/{code}", follow_redirects=False)
        assert resp.status_code == 410
        assert "location" not in resp.headers
    assert client.get(f"/{code}", follow_redirects=False).json() == DEACTIVATED_DETAIL
    assert _clicks(client) == 0


def test_null_expires_at_never_expires(client, clock):
    """Covers: FR-22, FR-21"""
    code = create_link(client)["code"]
    for now in (datetime(2025, 12, 31, 23, 59), datetime(2999, 1, 1), datetime(9999, 12, 31)):
        clock.set(now)
        assert client.get(f"/{code}", follow_redirects=False).status_code == 307
        assert client.head(f"/{code}", follow_redirects=False).status_code == 307
    assert client.get(f"/api/links/{code}").json()["is_expired"] is False


def test_expired_link_keeps_detail_and_stats(client, clock):
    """Covers: FR-22, FR-18, FR-25"""
    code = _create(client).json()["code"]
    for _ in range(3):
        client.get(f"/{code}", follow_redirects=False)
    clock.set(datetime(2026, 1, 2))
    _assert_expired_response(client.get(f"/{code}", follow_redirects=False))

    detail = client.get(f"/api/links/{code}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["is_expired"] is True
    assert body["is_active"] is True
    assert body["expires_at"] == CAMPAIGN_END
    assert body["total_clicks"] == 3

    stats = client.get(f"/api/links/{code}/stats", params={"days": 365})
    assert stats.status_code == 200
    assert stats.json()["total_clicks"] == 3

    now = datetime.now(UTC).replace(tzinfo=None)
    hourly = client.get(
        f"/api/links/{code}/stats/hourly",
        params={
            "start": (now - timedelta(hours=2)).isoformat() + "Z",
            "end": (now + timedelta(hours=1)).isoformat() + "Z",
        },
    )
    assert hourly.status_code == 200
    assert hourly.json()["total_clicks"] == 3


def test_openapi_documents_expired_410_on_redirect(client):
    """Covers: FR-22"""
    spec = client.get("/openapi.json").json()
    responses = spec["paths"]["/{code}"]["get"]["responses"]
    assert "410" in responses
    assert "expired" in responses["410"]["description"].lower()
    assert "deactivated" in responses["410"]["description"].lower()


def test_patching_shared_helper_clock_affects_get_and_head(client, monkeypatch):
    """Covers: FR-22"""
    code = _create(client, expires_at=...).json()["code"]
    # No expiry: patching the clock alone must not matter.
    monkeypatch.setattr(LinkService, "_utcnow", lambda self: datetime(9999, 1, 1))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    with new_session(client) as s:
        s.query(Link).filter_by(code=code).one().expires_at = datetime(2030, 1, 1)
        s.commit()
    assert client.get(f"/{code}", follow_redirects=False).json() == EXPIRED_DETAIL
    assert client.head(f"/{code}", follow_redirects=False).status_code == 410
    monkeypatch.setattr(LinkService, "_utcnow", lambda self: datetime(2029, 12, 31))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert client.head(f"/{code}", follow_redirects=False).status_code == 307


def test_get_and_head_share_check_redirectable_helper(client, monkeypatch):
    """Covers: FR-22"""
    code = create_link(client)["code"]
    seen: list[str] = []
    real = LinkService._check_redirectable

    def spy(self, target):
        seen.append(target.url)
        return real(self, target)

    monkeypatch.setattr(LinkService, "_check_redirectable", spy)
    client.get(f"/{code}", follow_redirects=False)
    client.head(f"/{code}", follow_redirects=False)
    assert len(seen) == 2


def test_service_injectable_clock_unit(client):
    """Covers: FR-22, NFR-22"""
    code = _create(client, expires_at=...).json()["code"]
    with new_session(client) as s:
        s.query(Link).filter_by(code=code).one().expires_at = CAMPAIGN_END_DT
        s.commit()
    with new_session(client) as s:
        before = LinkService(s, now=lambda: datetime(2025, 12, 31, 23, 58, 59, 999999))
        assert before.resolve_for_redirect(code) == "https://example.com/campaign"
        # An aware clock is normalised to naive UTC: 00:59+01:00 == 23:59Z.
        aware = LinkService(
            s, now=lambda: datetime(2026, 1, 1, 0, 59, tzinfo=timezone(timedelta(hours=1)))
        )
        with pytest.raises(GoneError) as exc:
            aware.resolve_for_redirect(code)
        assert exc.value.detail == "Link has expired"
        with pytest.raises(NotFoundError):
            aware.resolve_for_redirect("bad-code")
        assert aware.get_link_detail(code).is_expired is True
        assert before.get_link_detail(code).is_expired is False


def test_expired_redirect_logs_no_ip(client, clock, caplog):
    """Covers: NFR-14, FR-22"""
    code = _create(client).json()["code"]
    clock.set(datetime(2026, 1, 1))
    with caplog.at_level(logging.DEBUG):
        client.get(
            f"/{code}",
            headers={"X-Forwarded-For": "203.0.113.99", "User-Agent": "Exp-UA/1.0"},
            follow_redirects=False,
        )
    assert "testclient" not in caplog.text
    assert "203.0.113.99" not in caplog.text
    for r in caplog.records:
        assert "203.0.113.99" not in str(r.__dict__)
        assert "testclient" not in str(r.__dict__)


# =========================================================================== FR-23


def test_create_with_expires_at_z(client, clock):
    """Covers: FR-23"""
    resp = _create(client)
    assert resp.status_code == 201
    body = resp.json()
    assert body["expires_at"] == CAMPAIGN_END
    detail = client.get(f"/api/links/{body['code']}").json()
    assert detail["expires_at"] == CAMPAIGN_END
    assert detail["is_expired"] is False
    assert _stored(client, body["code"]).expires_at == CAMPAIGN_END_DT


def test_create_without_expires_at_returns_null(client):
    """Covers: FR-23, FR-21"""
    resp = _create(client, expires_at=...)
    assert resp.status_code == 201
    body = resp.json()
    assert body["expires_at"] is None
    assert set(body) == {"code", "short_url", "url", "is_active", "created_at", "expires_at"}
    detail = client.get(f"/api/links/{body['code']}").json()
    assert detail["expires_at"] is None
    assert detail["is_expired"] is False
    explicit_null = _create(client, expires_at=None)
    assert explicit_null.status_code == 201
    assert explicit_null.json()["expires_at"] is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("2026-01-01T00:59:00+01:00", "2025-12-31T23:59:00Z"),
        ("2025-12-31T23:59:00", "2025-12-31T23:59:00Z"),
        ("2025-12-31T18:59:00-05:00", "2025-12-31T23:59:00Z"),
        ("2026-01-01T05:29:00+05:30", "2025-12-31T23:59:00Z"),
        ("2025-12-31T23:59:00+00:00", "2025-12-31T23:59:00Z"),
        ("2025-12-31T23:59:00.250000Z", "2025-12-31T23:59:00.250000Z"),
    ],
)
def test_create_normalises_offsets_to_utc(client, clock, given, expected):
    """Covers: FR-23, NFR-22"""
    resp = _create(client, expires_at=given)
    assert resp.status_code == 201, resp.text
    assert resp.json()["expires_at"] == expected
    code = resp.json()["code"]
    assert client.get(f"/api/links/{code}").json()["expires_at"] == expected
    stored = _stored(client, code).expires_at
    assert stored.tzinfo is None
    assert stored.replace(microsecond=0) == CAMPAIGN_END_DT


@pytest.mark.parametrize(
    "value",
    ["2025-11-20T12:00:00Z", "2025-11-20T11:59:59Z", "2020-01-01T00:00:00Z",
     "2025-11-20T13:00:00+01:00", "2025-11-20T06:59:59-05:00"],
)
def test_create_with_past_or_now_expiry_returns_422(client, clock, value):
    """Covers: FR-23, NFR-22"""
    resp = _create(client, expires_at=value)
    assert_json_error(resp, 422)
    assert resp.json() == {"detail": "expires_at must be in the future"}
    assert count_rows(client, Link) == 0


def test_create_one_microsecond_in_future_accepted(client, clock):
    """Covers: FR-23, NFR-22"""
    resp = _create(client, expires_at="2025-11-20T12:00:00.000001Z")
    assert resp.status_code == 201


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "", "2025-13-01T00:00:00Z", "tomorrow", 12345, 1767225540.0, True, False,
     {"at": "2025"}, ["2025-12-31T23:59:00Z"]],
)
def test_create_with_non_datetime_expiry_returns_422(client, clock, value):
    """Covers: FR-23"""
    resp = _create(client, expires_at=value)
    assert_json_error(resp, 422)
    assert count_rows(client, Link) == 0


def test_create_with_out_of_range_offset_returns_422_not_500(client, clock):
    """Covers: FR-23, NFR-22"""
    resp = _create(client, expires_at="9999-12-31T23:00:00-05:00")
    assert_json_error(resp, 422)
    assert count_rows(client, Link) == 0
    far = _create(client, expires_at="9999-12-31T23:59:59Z")
    assert far.status_code == 201


def test_idempotent_replay_same_instant_different_notation(client, clock):
    """Covers: FR-23, FR-21"""
    headers = {"Idempotency-Key": "campaign-1"}
    first = _create(client, expires_at="2025-12-31T23:59:00Z", headers=headers)
    second = _create(client, expires_at="2026-01-01T00:59:00+01:00", headers=headers)
    third = _create(client, expires_at="2025-12-31T23:59:00", headers=headers)
    assert first.status_code == 201
    assert second.status_code == 201 and third.status_code == 201
    assert second.headers.get("idempotent-replayed") == "true"
    assert third.headers.get("idempotent-replayed") == "true"
    assert second.json() == first.json() == third.json()
    assert count_rows(client, Link) == 1


@pytest.mark.parametrize(
    ("first_exp", "second_exp"),
    [
        (CAMPAIGN_END, "2025-12-31T23:59:01Z"),
        (..., CAMPAIGN_END),
        (CAMPAIGN_END, ...),
        (CAMPAIGN_END, None),
        (None, CAMPAIGN_END),
    ],
)
def test_idempotency_conflict_when_expiry_differs(client, clock, first_exp, second_exp):
    """Covers: FR-23, FR-21"""
    headers = {"Idempotency-Key": "campaign-2"}
    assert _create(client, expires_at=first_exp, headers=headers).status_code == 201
    resp = _create(client, expires_at=second_exp, headers=headers)
    assert_json_error(resp, 409)
    assert count_rows(client, Link) == 1


def test_pre_deploy_url_only_key_replays_without_expires_at(client, clock):
    """Covers: FR-23, FR-21"""
    url = "https://example.com/legacy"
    with new_session(client) as s:
        link = Link(code="Legacy01", url=url, is_active=True)
        s.add(link)
        s.flush()
        s.add(
            IdempotencyKey(
                key="legacy-key",
                request_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
                link_id=link.id,
            )
        )
        s.commit()
    for exp in (..., None):
        resp = _create(client, url=url, expires_at=exp, headers={"Idempotency-Key": "legacy-key"})
        assert resp.status_code == 201, resp.text
        assert resp.headers.get("idempotent-replayed") == "true"
        assert resp.json()["code"] == "Legacy01"
        assert resp.json()["expires_at"] is None
    assert_json_error(
        _create(client, url=url, headers={"Idempotency-Key": "legacy-key"}), 409
    )
    assert count_rows(client, Link) == 1


def test_request_hash_is_64_hex_with_and_without_expiry(client, clock):
    """Covers: FR-23"""
    _create(client, headers={"Idempotency-Key": "h1"})
    _create(client, expires_at=..., headers={"Idempotency-Key": "h2"})
    with new_session(client) as s:
        hashes = {r.key: r.request_hash for r in s.query(IdempotencyKey).all()}
    assert set(hashes) == {"h1", "h2"}
    for value in hashes.values():
        assert len(value) == 64
        int(value, 16)
    assert hashes["h2"] == hashlib.sha256(b"https://example.com/campaign").hexdigest()
    assert hashes["h1"] != hashes["h2"]


def test_replay_after_stored_expiry_returns_stored_link_unchanged(client, clock):
    """Covers: FR-23"""
    headers = {"Idempotency-Key": "late-replay"}
    first = _create(client, headers=headers)
    clock.set(datetime(2026, 3, 1))
    replay = _create(client, headers=headers)
    assert replay.status_code == 201
    assert replay.headers.get("idempotent-replayed") == "true"
    assert replay.json() == first.json()
    # A fresh request with the same past instant is rejected.
    assert_json_error(_create(client), 422)
    assert count_rows(client, Link) == 1


def test_openapi_schemas_include_expiry_fields(client):
    """Covers: FR-23, FR-24"""
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "expires_at" in schemas["LinkCreate"]["properties"]
    assert "expires_at" not in schemas["LinkCreate"].get("required", [])
    assert "expires_at" in schemas["LinkCreatedResponse"]["properties"]
    assert "expires_at" in schemas["LinkDetailResponse"]["properties"]
    assert "is_expired" in schemas["LinkDetailResponse"]["properties"]
    assert "LinkExpiryUpdate" in schemas


def test_service_create_and_set_expiry_raise_invalid_input(client):
    """Covers: FR-23, FR-24"""
    code = create_link(client)["code"]
    with new_session(client) as s:
        svc = LinkService(s, now=lambda: datetime(2025, 11, 20))
        with pytest.raises(InvalidInputError):
            svc.create_link("https://example.com/s", expires_at=datetime(2025, 11, 20))
        with pytest.raises(InvalidInputError):
            svc.set_expiry(code, datetime(2025, 11, 19, 23, 0, tzinfo=UTC))
        result = svc.create_link(
            "https://example.com/s",
            expires_at=datetime(2026, 1, 1, 0, 59, tzinfo=timezone(timedelta(hours=1))),
        )
        assert result.link.expires_at == CAMPAIGN_END_DT


# =========================================================================== FR-24


def test_patch_sets_future_expiry_and_redirect_follows(client, clock):
    """Covers: FR-24, FR-22"""
    code = create_link(client, "https://example.com/billboard")["code"]
    resp = client.patch(f"/api/links/{code}", json={"expires_at": CAMPAIGN_END})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == code
    assert body["expires_at"] == CAMPAIGN_END
    assert body["is_expired"] is False
    assert body["is_active"] is True
    assert set(body) == {
        "code", "short_url", "url", "is_active", "created_at", "expires_at",
        "deactivated_at", "is_expired", "total_clicks",
    }
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    clock.set(CAMPAIGN_END_DT)
    assert client.get(f"/{code}", follow_redirects=False).json() == EXPIRED_DETAIL
    assert client.get(f"/api/links/{code}").json()["is_expired"] is True


def test_patch_null_clears_expiry_and_revives_expired_link(client, clock):
    """Covers: FR-24"""
    code = _create(client).json()["code"]
    clock.set(datetime(2026, 1, 5))
    assert client.get(f"/{code}", follow_redirects=False).status_code == 410
    resp = client.patch(f"/api/links/{code}", json={"expires_at": None})
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is None
    assert resp.json()["is_expired"] is False
    assert client.get(f"/{code}", follow_redirects=False).status_code == 307
    assert _stored(client, code).expires_at is None


def test_patch_extends_expired_link(client, clock):
    """Covers: FR-24"""
    code = _create(client).json()["code"]
    clock.set(datetime(2026, 1, 5))
    resp = client.patch(f"/api/links/{code}", json={"expires_at": "2026-01-31T23:59:00Z"})
    assert resp.status_code == 200
    assert client.head(f"/{code}", follow_redirects=False).status_code == 307


@pytest.mark.parametrize(
    "body",
    [
        {"expires_at": "2025-11-20T12:00:00Z"},
        {"expires_at": "2025-11-20T11:00:00Z"},
        {"expires_at": "2025-11-20T13:00:00+01:00"},
        {"expires_at": "not-a-date"},
        {"expires_at": 1767225540},
        {"expires_at": True},
        {},
        {"expires_at": CAMPAIGN_END, "url": "https://evil.example"},
        {"expires_at": CAMPAIGN_END, "is_active": True},
        {"expires_at": "9999-12-31T23:00:00-05:00"},
    ],
)
def test_patch_invalid_returns_422_and_keeps_stored_value(client, clock, body):
    """Covers: FR-24, NFR-22"""
    code = _create(client, expires_at="2025-12-01T00:00:00Z").json()["code"]
    resp = client.patch(f"/api/links/{code}", json=body)
    assert_json_error(resp, 422)
    stored = _stored(client, code)
    assert stored.expires_at == datetime(2025, 12, 1)
    assert stored.url == "https://example.com/campaign"


def test_patch_non_json_body_returns_422(client, clock):
    """Covers: FR-24"""
    code = create_link(client)["code"]
    resp = client.patch(
        f"/api/links/{code}", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert_json_error(resp, 422)


@pytest.mark.parametrize("bad_headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong-key-0123456789"}])
def test_patch_requires_api_key(make_client, clock, bad_headers):
    """Covers: FR-24, FR-21, NFR-15"""
    client = make_client(send_api_key=False)
    code = client.post(
        "/api/links", json={"url": "https://example.com/k"}, headers=AUTH_HEADERS
    ).json()["code"]
    resp = client.patch(f"/api/links/{code}", json={"expires_at": CAMPAIGN_END}, headers=bad_headers)
    assert_json_error(resp, 401)
    assert resp.json() == {"detail": "Invalid or missing API key"}
    assert resp.headers["www-authenticate"] == "ApiKey"
    assert _stored(client, code).expires_at is None
    # 401 takes precedence over body validation and unknown codes.
    assert client.patch("/api/links/Nope123", json={}, headers=bad_headers).status_code == 401


@pytest.mark.parametrize("code", ["Unknown1", "bad-code", "a" * 40])
def test_patch_unknown_or_malformed_code_404(client, clock, code):
    """Covers: FR-24"""
    resp = client.patch(f"/api/links/{code}", json={"expires_at": CAMPAIGN_END})
    assert_json_error(resp, 404)
    assert resp.json() == {"detail": "Link not found"}


def test_patch_on_deactivated_link_updates_but_stays_410(client, clock):
    """Covers: FR-24"""
    code = create_link(client)["code"]
    client.delete(f"/api/links/{code}")
    before = client.get(f"/api/links/{code}").json()
    resp = client.patch(f"/api/links/{code}", json={"expires_at": CAMPAIGN_END})
    assert resp.status_code == 200
    body = resp.json()
    assert body["expires_at"] == CAMPAIGN_END
    assert body["is_active"] is False
    assert body["deactivated_at"] == before["deactivated_at"]
    assert client.get(f"/{code}", follow_redirects=False).json() == DEACTIVATED_DETAIL
    cleared = client.patch(f"/api/links/{code}", json={"expires_at": None})
    assert cleared.status_code == 200
    assert cleared.json()["is_active"] is False
    assert client.head(f"/{code}", follow_redirects=False).status_code == 410


def test_patch_changes_only_expires_at(client, clock):
    """Covers: FR-24"""
    created = create_link(
        client, "https://example.com/only", headers={"Idempotency-Key": "patch-only"}
    )
    code = created["code"]
    before = _stored(client, code)
    with new_session(client) as s:
        idem_before = [(r.key, r.request_hash, r.link_id) for r in s.query(IdempotencyKey).all()]
    resp = client.patch(f"/api/links/{code}", json={"expires_at": CAMPAIGN_END})
    assert resp.status_code == 200
    after = _stored(client, code)
    assert after.expires_at == CAMPAIGN_END_DT
    for field in ("id", "url", "code", "is_active", "created_at", "deactivated_at"):
        assert getattr(after, field) == getattr(before, field), field
    with new_session(client) as s:
        idem_after = [(r.key, r.request_hash, r.link_id) for r in s.query(IdempotencyKey).all()]
    assert idem_after == idem_before
    # The original idempotent create still replays (url-only hash, no expires_at).
    replay = client.post(
        "/api/links", json={"url": "https://example.com/only"},
        headers={"Idempotency-Key": "patch-only"},
    )
    assert replay.status_code == 201
    assert replay.json()["code"] == code


@pytest.mark.parametrize(
    "given",
    ["2026-01-01T00:59:00+01:00", "2025-12-31T18:59:00-05:00", "2026-01-01T05:29:00+05:30",
     "2025-12-31T23:59:00"],
)
def test_patch_normalises_offsets(client, clock, given):
    """Covers: FR-24, NFR-22"""
    code = create_link(client)["code"]
    resp = client.patch(f"/api/links/{code}", json={"expires_at": given})
    assert resp.status_code == 200
    assert resp.json()["expires_at"] == CAMPAIGN_END
    assert _stored(client, code).expires_at == CAMPAIGN_END_DT


def test_openapi_documents_patch(client):
    """Covers: FR-24"""
    spec = client.get("/openapi.json").json()
    patch = spec["paths"]["/api/links/{code}"]["patch"]
    body_schema = patch["requestBody"]["content"]["application/json"]["schema"]
    assert body_schema["$ref"].endswith("/LinkExpiryUpdate")
    assert {"200", "401", "404", "422"} <= set(patch["responses"])
    ok = patch["responses"]["200"]["content"]["application/json"]["schema"]
    assert ok["$ref"].endswith("/LinkDetailResponse")
    assert {"ApiKeyAuth": []} in patch["security"]
    update = spec["components"]["schemas"]["LinkExpiryUpdate"]
    assert update["required"] == ["expires_at"]
    assert update.get("additionalProperties") is False
