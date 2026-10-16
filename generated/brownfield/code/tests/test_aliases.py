"""CR-142 custom aliases: creation, reserved words, uniqueness and lookup (FR-1..FR-4, FR-10)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from app.models import ClickEvent, IdempotencyKey, Link
from app.repositories import LinkRepository
from app.services import codes
from helpers import assert_json_error, count_rows, create_link, new_session
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

URL = "https://example.com/target?x=1"


def _post(client, alias, url=URL, **extra):
    body = {"url": url, "alias": alias}
    body.update(extra)
    return client.post("/api/links", json=body)


def _assert_no_traceback(resp):
    assert set(resp.json().keys()) == {"detail"}
    assert "Traceback" not in resp.text


# ---------------------------------------------------------------- FR-1


def test_create_with_alias_returns_201_with_code_and_location(client):
    """Covers: FR-1"""
    resp = _post(client, "my-link_1")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["code"] == "my-link_1"
    assert body["short_url"].endswith("/my-link_1")
    assert resp.headers["location"].endswith("/api/links/my-link_1")
    assert body["url"] == URL
    assert count_rows(client, Link) == 1


@pytest.mark.parametrize("alias", ["abc", "A" * 16 + "b-_" + "9" * 13])
def test_alias_boundary_lengths_accepted(client, alias):
    """Covers: FR-1"""
    assert len(alias) in (3, 32)
    resp = _post(client, alias)
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == alias


@pytest.mark.parametrize(
    "alias",
    ["ab", "a" * 33, "", "a.b", "a b", "a/b", "\u00e4-link", "abc%20", "abc\n", 123, True, []],
)
def test_alias_format_errors_return_422_list_and_persist_nothing(client, alias):
    """Covers: FR-1, NFR-2"""
    resp = _post(client, alias)
    assert_json_error(resp, 422)
    assert isinstance(resp.json()["detail"], list)
    assert "Traceback" not in resp.text
    assert count_rows(client, Link) == 0


@pytest.mark.parametrize("body_extra", [{}, {"alias": None}])
def test_omitted_or_null_alias_generates_base62_code(client, body_extra):
    """Covers: FR-1, FR-12"""
    resp = client.post("/api/links", json={"url": URL, **body_extra})
    assert resp.status_code == 201, resp.text
    code = resp.json()["code"]
    assert re.fullmatch(r"[A-Za-z0-9]{7}", code)


def test_aliases_are_case_sensitive(client):
    """Covers: FR-1"""
    assert _post(client, "MyLink", url="https://example.com/upper").status_code == 201
    assert _post(client, "mylink", url="https://example.com/lower").status_code == 201
    r1 = client.get("/MyLink", follow_redirects=False)
    r2 = client.get("/mylink", follow_redirects=False)
    assert r1.status_code == 307 and r1.headers["location"] == "https://example.com/upper"
    assert r2.status_code == 307 and r2.headers["location"] == "https://example.com/lower"


def test_alias_create_requires_api_key(make_client):
    """Covers: FR-1, NFR-2"""
    anon = make_client(send_api_key=False)
    resp = _post(anon, "my-alias")
    assert_json_error(resp, 401)
    assert count_rows(anon, Link) == 0


# ---------------------------------------------------------------- FR-2 / FR-10


@pytest.mark.parametrize(
    "alias",
    ["api", "docs", "redoc", "healthz", "readyz", "metrics", "static",
     "API", "Docs", "METRICS", "HealthZ", "Static"],
)
def test_reserved_alias_returns_422(client, alias):
    """Covers: FR-2, FR-10, NFR-2"""
    resp = _post(client, alias)
    assert_json_error(resp, 422)
    _assert_no_traceback(resp)
    assert count_rows(client, Link) == 0


@pytest.mark.parametrize("alias", ["openapi.json", "favicon.ico"])
def test_reserved_names_with_dot_return_422(client, alias):
    """Covers: FR-2"""
    assert_json_error(_post(client, alias), 422)
    assert count_rows(client, Link) == 0


def test_reserved_list_contents():
    """Covers: FR-2, FR-10, NFR-5"""
    required = {"api", "docs", "redoc", "healthz", "readyz", "metrics", "openapi.json",
                "static", "favicon.ico"}
    assert required <= set(codes.RESERVED_CODES)
    assert codes.is_reserved("metrics")
    assert codes.is_reserved("METRICS")
    assert codes.is_reserved("Favicon.ICO")


@pytest.mark.parametrize("alias", ["api-docs", "my-metrics", "docs_1", "healthzz"])
def test_alias_containing_reserved_word_is_accepted(client, alias):
    """Covers: FR-2"""
    resp = _post(client, alias)
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == alias


def test_probe_and_docs_routes_not_shadowed(client):
    """Covers: FR-2, FR-12"""
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200
    spec = client.get("/openapi.json")
    assert spec.status_code == 200
    assert "paths" in spec.json()


def test_generated_reserved_metrics_candidate_is_discarded(client, monkeypatch):
    """Covers: FR-10, FR-3"""
    seq = iter(["metrics", "Good123"])
    monkeypatch.setattr(codes, "generate_code", lambda length: next(seq))
    resp = client.post("/api/links", json={"url": URL})
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "Good123"


def test_generated_code_exhaustion_still_returns_503(client, monkeypatch):
    """Covers: FR-3, FR-12"""
    monkeypatch.setattr(codes, "generate_code", lambda length: "metrics")
    resp = client.post("/api/links", json={"url": URL})
    assert_json_error(resp, 503)
    assert count_rows(client, Link) == 0


def test_generated_code_collision_retries(client, monkeypatch):
    """Covers: FR-3, FR-12"""
    _post(client, "Taken12")
    seq = iter(["Taken12", "Fresh12"])
    monkeypatch.setattr(codes, "generate_code", lambda length: next(seq))
    resp = client.post("/api/links", json={"url": URL})
    assert resp.status_code == 201
    assert resp.json()["code"] == "Fresh12"


# ---------------------------------------------------------------- FR-3


def test_duplicate_alias_returns_409(client):
    """Covers: FR-3, NFR-2"""
    assert _post(client, "promo").status_code == 201
    for url in (URL, "https://other.example.com/"):
        resp = _post(client, "promo", url=url)
        assert_json_error(resp, 409)
        _assert_no_traceback(resp)
        assert "alias" in resp.json()["detail"].lower()
    assert count_rows(client, Link) == 1


def test_alias_equal_to_generated_code_returns_409(client):
    """Covers: FR-3"""
    code = create_link(client, URL)["code"]
    assert_json_error(_post(client, code), 409)
    assert count_rows(client, Link) == 1


def test_alias_equal_to_deactivated_code_returns_409(client):
    """Covers: FR-3"""
    assert _post(client, "gone-link").status_code == 201
    assert client.delete("/api/links/gone-link").status_code == 204
    assert_json_error(_post(client, "gone-link"), 409)
    assert count_rows(client, Link) == 1


def test_alias_equal_to_expired_code_returns_409(client):
    """Covers: FR-3"""
    assert _post(client, "old-link").status_code == 201
    with new_session(client) as s:
        s.execute(update(Link).values(expires_at=datetime(2000, 1, 1)))
        s.commit()
    assert client.get("/old-link", follow_redirects=False).status_code == 410
    assert_json_error(_post(client, "old-link"), 409)
    assert count_rows(client, Link) == 1


def _patch_race(monkeypatch):
    """Make LinkRepository.add flush, then fail like a concurrent insert won the race."""
    def boom(self, link):
        raise IntegrityError("INSERT INTO links", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(LinkRepository, "add", boom)

    calls = []

    def no_generate(length):
        calls.append(length)
        return "Should0"

    monkeypatch.setattr(codes, "generate_code", no_generate)
    return calls


def test_alias_insert_race_maps_to_409_without_generating(client, monkeypatch):
    """Covers: FR-3, NFR-4"""
    calls = _patch_race(monkeypatch)
    resp = _post(client, "racy")
    assert_json_error(resp, 409)
    assert "alias" in resp.json()["detail"].lower()
    assert calls == []
    monkeypatch.undo()
    assert count_rows(client, Link) == 0
    assert count_rows(client, IdempotencyKey) == 0


def test_alias_insert_race_with_idempotency_key_leaves_no_rows(client, monkeypatch):
    """Covers: FR-3, NFR-4, FR-9"""
    calls = _patch_race(monkeypatch)
    resp = client.post(
        "/api/links", json={"url": URL, "alias": "racy2"}, headers={"Idempotency-Key": "k-race"}
    )
    assert_json_error(resp, 409)
    assert calls == []
    monkeypatch.undo()
    assert count_rows(client, Link) == 0
    assert count_rows(client, IdempotencyKey) == 0
    # The session was rolled back: the same request now succeeds normally.
    ok = client.post(
        "/api/links", json={"url": URL, "alias": "racy2"}, headers={"Idempotency-Key": "k-race"}
    )
    assert ok.status_code == 201


def test_real_unique_constraint_backs_alias_uniqueness(client, monkeypatch):
    """Covers: FR-3, NFR-4"""
    assert _post(client, "dupe").status_code == 201
    # Bypass the friendly pre-check: the UNIQUE constraint must still produce 409.
    monkeypatch.setattr(LinkRepository, "get_by_code", lambda self, code: None)
    resp = _post(client, "dupe", url="https://other.example.com/")
    assert_json_error(resp, 409)
    monkeypatch.undo()
    assert count_rows(client, Link) == 1


# ---------------------------------------------------------------- FR-4


def _clicks(client) -> int:
    return count_rows(client, ClickEvent)


def test_alias_resolves_on_all_code_endpoints(client):
    """Covers: FR-4, FR-12"""
    target = "https://example.com/p?q=a%20b"
    assert _post(client, "my-link_1", url=target).status_code == 201

    head = client.head("/my-link_1", follow_redirects=False)
    assert head.status_code == 307
    assert head.headers["location"] == target
    assert _clicks(client) == 0

    get = client.get("/my-link_1", follow_redirects=False)
    assert get.status_code == 307
    assert get.headers["location"] == target
    assert get.headers["cache-control"] == "no-store"
    assert _clicks(client) == 1

    meta = client.get("/api/links/my-link_1")
    assert meta.status_code == 200
    assert meta.json()["code"] == "my-link_1"
    assert meta.json()["total_clicks"] == 1

    stats = client.get("/api/links/my-link_1/stats")
    assert stats.status_code == 200
    assert stats.json()["total_clicks"] == 1

    assert client.delete("/api/links/my-link_1").status_code == 204
    gone = client.get("/my-link_1", follow_redirects=False)
    assert_json_error(gone, 410)
    assert gone.json()["detail"] == "Link has been deactivated"
    assert _clicks(client) == 1


@pytest.mark.parametrize("bad", ["a.b", "a" * 33, "abc%20", "ab%2Ec", "%C3%A4bc"])
def test_malformed_codes_return_404_without_db_lookup(client, monkeypatch, bad):
    """Covers: FR-4"""
    def fail(self, code):
        raise AssertionError("DB lookup for malformed code")

    monkeypatch.setattr(LinkRepository, "get_by_code", fail)
    for method, path in (
        ("GET", f"/{bad}"),
        ("HEAD", f"/{bad}"),
        ("GET", f"/api/links/{bad}"),
        ("DELETE", f"/api/links/{bad}"),
        ("GET", f"/api/links/{bad}/stats"),
    ):
        resp = client.request(method, path, follow_redirects=False)
        assert resp.status_code == 404, (method, path, resp.text)
        if method != "HEAD":
            assert resp.json() == {"detail": "Link not found"} or "detail" in resp.json()


@pytest.mark.parametrize("unknown", ["abc-def", "abc_def", "no-such_link", "x"])
def test_valid_format_unknown_codes_return_404(client, unknown):
    """Covers: FR-4"""
    for method, path in (
        ("GET", f"/{unknown}"),
        ("GET", f"/api/links/{unknown}"),
        ("DELETE", f"/api/links/{unknown}"),
        ("GET", f"/api/links/{unknown}/stats"),
    ):
        assert_json_error(client.request(method, path, follow_redirects=False), 404)
    assert client.head(f"/{unknown}").status_code == 404


def test_code_validators():
    """Covers: FR-4, FR-1"""
    assert not codes.is_valid_code_format("abc-123")
    assert codes.is_valid_lookup_code("abc-123")
    assert codes.is_valid_lookup_code("a")
    assert not codes.is_valid_lookup_code("a" * 33)
    assert not codes.is_valid_lookup_code("a.b")
    assert not codes.is_valid_lookup_code("abc\n")
    assert codes.is_valid_alias("my-link_1")
    assert not codes.is_valid_alias("ab")
    assert not codes.is_valid_alias("a" * 33)
    assert not codes.is_valid_alias("\u00e4bc")


def test_service_defensively_rejects_invalid_alias(client):
    """Covers: FR-1, NFR-2"""
    from app.core.errors import UnprocessableEntityError
    from app.services.link_service import LinkService

    with new_session(client) as s:
        service = LinkService(s)
        for bad in ("a.b", "ab", "x" * 33):
            with pytest.raises(UnprocessableEntityError):
                service.create_link(URL, alias=bad)
    assert count_rows(client, Link) == 0


def test_alias_stored_exactly_as_given(client):
    """Covers: FR-1"""
    assert _post(client, "MiXeD-Case_9").status_code == 201
    with new_session(client) as s:
        assert s.scalar(select(Link.code)) == "MiXeD-Case_9"


def test_alias_combined_with_expiry_in_db(client):
    """Covers: FR-1, FR-5"""
    exp = (datetime.utcnow() + timedelta(days=1)).replace(microsecond=0)
    resp = _post(client, "combo", expires_at=exp.isoformat() + "Z")
    assert resp.status_code == 201, resp.text
    with new_session(client) as s:
        link = s.scalar(select(Link).where(Link.code == "combo"))
        assert link.expires_at == exp
