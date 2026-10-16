"""BUG-77 regression tests: URL scheme validation is unchanged (FR-11, NFR-2)."""

from __future__ import annotations

import pytest
from app.models import Link
from app.schemas.links import validate_target_url
from helpers import assert_json_error, count_rows

JS_VARIANTS = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "JAVASCRIPT:alert(1)",
    "javascript://example.com/%0aalert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "java\nscript:alert(1)",
    "http:javascript:alert(1)",
]

EXTRA_FIELDS = [
    {},
    {"alias": "bug77-alias"},
    {"expires_at": "2099-01-01T00:00:00Z"},
    {"alias": "bug77-both", "expires_at": "2099-01-01T00:00:00Z"},
]


@pytest.mark.parametrize("extra", EXTRA_FIELDS)
def test_uppercase_http_scheme_accepted_unchanged(client, extra):
    """Covers: FR-11, NFR-2, BUG-77"""
    resp = client.post("/api/links", json={"url": "HTTP://Example.com", **extra})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["url"] == "HTTP://Example.com"
    redirect = client.get(f"/{body['code']}", follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"] == "HTTP://Example.com"
    assert client.get(f"/api/links/{body['code']}").json()["url"] == "HTTP://Example.com"


@pytest.mark.parametrize("extra", EXTRA_FIELDS)
def test_uppercase_https_with_path_accepted_unchanged(client, extra):
    """Covers: FR-11, BUG-77"""
    resp = client.post("/api/links", json={"url": "HTTPS://EXAMPLE.COM/Path", **extra})
    assert resp.status_code == 201, resp.text
    assert resp.json()["url"] == "HTTPS://EXAMPLE.COM/Path"


@pytest.mark.parametrize("extra", EXTRA_FIELDS)
@pytest.mark.parametrize("url", JS_VARIANTS)
def test_javascript_variants_rejected(client, url, extra):
    """Covers: FR-11, NFR-2, BUG-77"""
    resp = client.post("/api/links", json={"url": url, **extra})
    assert_json_error(resp, 422)
    assert "Traceback" not in resp.text
    assert count_rows(client, Link) == 0


@pytest.mark.parametrize("url", JS_VARIANTS)
def test_validator_rejects_javascript_directly(url):
    """Covers: FR-11, BUG-77"""
    with pytest.raises(ValueError):
        validate_target_url(url)


def test_validator_returns_uppercase_scheme_unchanged():
    """Covers: FR-11, BUG-77"""
    assert validate_target_url("HTTP://Example.com") == "HTTP://Example.com"
    assert validate_target_url("HTTPS://EXAMPLE.COM/Path") == "HTTPS://EXAMPLE.COM/Path"
