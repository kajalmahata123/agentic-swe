"""Small helpers shared by the test modules (no fixtures here)."""

from __future__ import annotations

from sqlalchemy import func, select

TEST_BASE_URL = "http://sho.rt"
TEST_API_KEY = "test-api-key-0123456789abcdef"
AUTH_HEADERS = {"X-API-Key": TEST_API_KEY}


def new_session(client):
    """Open an ORM session against the client's (temporary) database."""
    return client.app.state.session_factory()


def count_rows(client, model) -> int:
    with new_session(client) as s:
        return int(s.scalar(select(func.count()).select_from(model)))


def create_link(client, url: str = "https://example.com/a?b=1", **kwargs) -> dict:
    resp = client.post("/api/links", json={"url": url}, **kwargs)
    assert resp.status_code == 201, resp.text
    return resp.json()


def assert_json_error(resp, status: int) -> None:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "detail" in resp.json()
