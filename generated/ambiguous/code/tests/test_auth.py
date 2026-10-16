"""Tests for API-key protection of the /api/links management router (SEC-1).

The management endpoints (create, lookup, deactivate, stats) require the X-API-Key
header; the public redirect and the health probes stay open.
"""

from __future__ import annotations

import pytest
from app.core.config import Settings
from app.models import ClickEvent, Link
from helpers import AUTH_HEADERS, TEST_API_KEY, assert_json_error, count_rows, create_link


def _management_calls(code: str):
    return [
        ("post", "/api/links", {"json": {"url": "https://example.com/x"}}),
        ("get", f"/api/links/{code}", {}),
        ("delete", f"/api/links/{code}", {}),
        ("get", f"/api/links/{code}/stats", {}),
    ]


@pytest.mark.parametrize("bad_headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong-key-0123456789"}])
def test_management_endpoints_reject_missing_or_wrong_key(make_client, bad_headers):
    """Covers: FR-11, FR-7, FR-9, FR-1"""
    anon = make_client(send_api_key=False)
    # Seed one link with an explicitly authenticated request.
    anon_code = anon.post(
        "/api/links", json={"url": "https://example.com/a"}, headers=AUTH_HEADERS
    ).json()["code"]
    for method, path, kwargs in _management_calls(anon_code):
        resp = getattr(anon, method)(path, headers=bad_headers, **kwargs)
        assert_json_error(resp, 401)
        assert resp.json() == {"detail": "Invalid or missing API key"}
        assert resp.headers["www-authenticate"] == "ApiKey"
    # Nothing was created or deactivated by the rejected requests.
    assert count_rows(anon, Link) == 1
    with anon.app.state.session_factory() as s:
        assert s.query(Link).one().is_active is True


def test_valid_key_allows_full_management_flow(make_client):
    """Covers: FR-1, FR-6, FR-7, FR-9"""
    client = make_client(send_api_key=False)
    resp = client.post("/api/links", json={"url": "https://example.com/z"}, headers=AUTH_HEADERS)
    assert resp.status_code == 201
    code = resp.json()["code"]
    assert client.get(f"/api/links/{code}", headers=AUTH_HEADERS).status_code == 200
    assert client.get(f"/api/links/{code}/stats", headers=AUTH_HEADERS).status_code == 200
    assert client.delete(f"/api/links/{code}", headers=AUTH_HEADERS).status_code == 204


def test_redirect_and_probes_stay_public(make_client):
    """Covers: FR-5, FR-8, FR-10"""
    authed = make_client()
    code = create_link(authed, "https://example.com/pub")["code"]
    authed.headers.pop("X-API-Key")
    resp = authed.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "https://example.com/pub"
    assert authed.head(f"/{code}", follow_redirects=False).status_code == 307
    assert authed.get("/healthz").status_code == 200
    assert authed.get("/readyz").status_code == 200
    assert count_rows(authed, ClickEvent) == 1


def test_unconfigured_api_key_fails_closed(make_client, caplog):
    """Covers: FR-11, FR-12"""
    client = make_client(api_key=None)
    resp = client.post("/api/links", json={"url": "https://example.com/x"})
    assert_json_error(resp, 401)
    assert count_rows(client, Link) == 0
    assert "API_KEY is not configured" in caplog.text


def test_api_key_setting_from_env_and_validation(monkeypatch):
    """Covers: FR-12"""
    monkeypatch.delenv("API_KEY", raising=False)
    assert Settings().api_key is None
    monkeypatch.setenv("API_KEY", "   ")
    assert Settings().api_key is None
    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    s = Settings()
    assert s.api_key is not None and s.api_key.get_secret_value() == TEST_API_KEY
    assert TEST_API_KEY not in repr(s)
    with pytest.raises(ValueError):
        Settings(api_key="short")


def test_api_key_is_not_logged(client, caplog):
    """Covers: NFR-8"""
    caplog.set_level("DEBUG", logger="app")
    create_link(client)
    client.get("/api/links/doesnotexist", headers={"X-API-Key": "wrong-key-0123456789"})
    assert TEST_API_KEY not in caplog.text
    assert "wrong-key-0123456789" not in caplog.text


def test_openapi_documents_api_key_security(client):
    """Covers: FR-13"""
    spec = client.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["ApiKeyAuth"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": "Shared management API key (API_KEY setting).",
    }
    post = spec["paths"]["/api/links"]["post"]
    assert {"ApiKeyAuth": []} in post["security"]
    assert "401" in post["responses"]
    assert "security" not in spec["paths"]["/{code}"]["get"]
    assert "security" not in spec["paths"]["/healthz"]["get"]
