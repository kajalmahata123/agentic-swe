"""Redirect hot-path SQL budget (FR-17), including that the expiry check adds no
query (FR-22)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime

import pytest
from helpers import assert_json_error, create_link
from sqlalchemy import event


@contextmanager
def sql_counter(engine):
    record = {"statements": [], "commits": 0}

    def _before(conn, cursor, statement, parameters, context, executemany):
        record["statements"].append(statement)

    def _commit(conn):
        record["commits"] += 1

    event.listen(engine, "before_cursor_execute", _before)
    event.listen(engine, "commit", _commit)
    try:
        yield record
    finally:
        event.remove(engine, "before_cursor_execute", _before)
        event.remove(engine, "commit", _commit)


def _norm(statement: str) -> str:
    return " ".join(statement.split()).upper()


def _selects_on_links(statements):
    return [s for s in statements if _norm(s).startswith("SELECT") and "FROM LINKS" in _norm(s)]


def _click_inserts(statements):
    return [s for s in statements if _norm(s).startswith("INSERT INTO CLICK_EVENTS")]


def _warm(client, code):
    client.head(f"/{code}", follow_redirects=False)


def test_successful_get_one_select_one_insert_one_commit(client):
    """Covers: FR-17"""
    code = create_link(client)["code"]
    _warm(client, code)
    with sql_counter(client.app.state.engine) as rec:
        resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    statements = rec["statements"]
    assert len(_selects_on_links(statements)) == 1
    assert len(_click_inserts(statements)) == 1
    assert len(statements) == 2, statements
    assert rec["commits"] == 1


def test_redirect_lookup_selects_only_needed_columns(client):
    """Covers: FR-17"""
    code = create_link(client)["code"]
    _warm(client, code)
    with sql_counter(client.app.state.engine) as rec:
        client.get(f"/{code}", follow_redirects=False)
    (select_sql,) = _selects_on_links(rec["statements"])
    select_list = _norm(select_sql).split(" FROM ")[0]
    for needed in ("LINKS.ID", "LINKS.URL", "LINKS.IS_ACTIVE", "LINKS.EXPIRES_AT"):
        assert needed in select_list
    for unneeded in ("LINKS.CREATED_AT", "LINKS.DEACTIVATED_AT", "LINKS.CODE"):
        assert unneeded not in select_list


def test_head_one_select_no_insert_no_commit(client):
    """Covers: FR-17"""
    code = create_link(client)["code"]
    _warm(client, code)
    with sql_counter(client.app.state.engine) as rec:
        resp = client.head(f"/{code}", follow_redirects=False)
    assert resp.status_code == 307
    assert len(rec["statements"]) == 1
    assert len(_selects_on_links(rec["statements"])) == 1
    assert _click_inserts(rec["statements"]) == []
    assert rec["commits"] == 0


@pytest.mark.parametrize("path", ["/bad-code", "/a.b", "/" + "x" * 33, "/%C3%A9t%C3%A9", "/a_b"])
def test_malformed_code_issues_no_sql(client, path):
    """Covers: FR-17"""
    with sql_counter(client.app.state.engine) as rec:
        get_resp = client.get(path, follow_redirects=False)
        head_resp = client.head(path, follow_redirects=False)
    assert_json_error(get_resp, 404)
    assert head_resp.status_code == 404
    assert rec["statements"] == []
    assert rec["commits"] == 0


def test_unknown_deactivated_expired_one_select_no_insert(client, clock):
    """Covers: FR-17, FR-22"""
    gone = create_link(client)["code"]
    client.delete(f"/api/links/{gone}")
    expired = client.post(
        "/api/links", json={"url": "https://example.com/x", "expires_at": "2025-12-31T23:59:00Z"}
    ).json()["code"]
    clock.set(datetime(2026, 1, 1))
    _warm(client, gone)
    for code, status in (("Unknown7", 404), (gone, 410), (expired, 410)):
        for method in ("get", "head"):
            with sql_counter(client.app.state.engine) as rec:
                resp = getattr(client, method)(f"/{code}", follow_redirects=False)
            assert resp.status_code == status
            assert len(rec["statements"]) == 1, (code, method, rec["statements"])
            assert len(_selects_on_links(rec["statements"])) == 1
            assert _click_inserts(rec["statements"]) == []
            assert rec["commits"] == 0


def test_expiry_check_adds_no_query_for_unexpired_link(client, clock):
    """Covers: FR-17, FR-22"""
    with_expiry = client.post(
        "/api/links", json={"url": "https://example.com/y", "expires_at": "2025-12-31T23:59:00Z"}
    ).json()["code"]
    without = create_link(client)["code"]
    _warm(client, without)
    counts = []
    for code in (with_expiry, without):
        with sql_counter(client.app.state.engine) as rec:
            assert client.get(f"/{code}", follow_redirects=False).status_code == 307
        counts.append((len(rec["statements"]), rec["commits"]))
    assert counts == [(2, 1), (2, 1)]


def test_location_byte_identical_to_stored_url(client):
    """Covers: FR-17, FR-21"""
    url = "https://example.com/%7Euser/a%2Fb?q=%E2%82%AC&x=a+b&y=[z]&w='v'#Frag%20x"
    code = create_link(client, url)["code"]
    for method in ("get", "head"):
        resp = getattr(client, method)(f"/{code}", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == url
        assert resp.headers.raw and any(
            k.lower() == b"location" and v == url.encode("ascii") for k, v in resp.headers.raw
        )
