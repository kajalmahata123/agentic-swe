"""Tests for random, URL-safe, collision-safe short codes (FR-3, NFR-5)."""

from __future__ import annotations

import logging
import re

import pytest
from app.models import IdempotencyKey, Link
from app.services import codes
from helpers import assert_json_error, count_rows, create_link, new_session
from sqlalchemy import UniqueConstraint
from sqlalchemy.exc import IntegrityError

CODE_RE = re.compile(r"^[A-Za-z0-9]{7}$")


def _scripted(sequence):
    """Return a fake generate_code that yields ``sequence`` items in order."""
    calls = []
    it = iter(sequence)

    def fake(length: int) -> str:
        calls.append(length)
        return next(it)

    fake.calls = calls
    return fake


def test_generated_codes_match_base62_7_by_default(client):
    """Covers: FR-3"""
    for _ in range(20):
        assert CODE_RE.fullmatch(create_link(client)["code"])
    for _ in range(200):
        assert CODE_RE.fullmatch(codes.generate_code(7))


def test_generate_code_unit_and_helpers():
    """Covers: FR-3, NFR-2"""
    assert re.fullmatch(r"[A-Za-z0-9]{12}", codes.generate_code(12))
    assert set(codes.ALPHABET) == set(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    )
    for word in ("healthz", "READYZ", "Api", "docs", "redoc", "openapi.json"):
        assert codes.is_reserved(word)
    assert not codes.is_reserved("abcDEF1")
    assert codes.is_valid_code_format("abc123")
    assert not codes.is_valid_code_format("abc-123")
    assert not codes.is_valid_code_format("a" * 33)
    assert not codes.is_valid_code_format("")


def test_code_length_is_configurable(make_client):
    """Covers: FR-3, FR-12"""
    client = make_client(short_code_length=10)
    code = create_link(client)["code"]
    assert re.fullmatch(r"[A-Za-z0-9]{10}", code)


def test_links_code_has_unique_constraint_and_duplicate_raises(client):
    """Covers: FR-3, NFR-5"""
    uniques = [
        c for c in Link.__table__.constraints if isinstance(c, UniqueConstraint)
    ]
    assert any([col.name for col in c.columns] == ["code"] for c in uniques)

    with new_session(client) as s:
        s.add(Link(code="DupCode", url="https://a.example"))
        s.commit()
    with new_session(client) as s:
        s.add(Link(code="DupCode", url="https://b.example"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_idempotency_key_has_unique_constraint_and_duplicate_raises(client):
    """Covers: FR-4, NFR-5"""
    uniques = [
        c for c in IdempotencyKey.__table__.constraints if isinstance(c, UniqueConstraint)
    ]
    assert any([col.name for col in c.columns] == ["key"] for c in uniques)
    link_id = None
    with new_session(client) as s:
        link = Link(code="Idem001", url="https://a.example")
        s.add(link)
        s.commit()
        link_id = link.id
        s.add(IdempotencyKey(key="k1", request_hash="h", link_id=link_id))
        s.commit()
    with new_session(client) as s:
        s.add(IdempotencyKey(key="k1", request_hash="h", link_id=link_id))
        with pytest.raises(IntegrityError):
            s.commit()


def test_forced_collision_retries_with_fresh_code(client, monkeypatch):
    """Covers: FR-3, NFR-5"""
    existing = create_link(client, "https://example.com/first")["code"]
    fake = _scripted([existing, "Fresh01"])
    monkeypatch.setattr("app.services.codes.generate_code", fake)

    resp = client.post("/api/links", json={"url": "https://example.com/second"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["code"] == "Fresh01"
    assert len(fake.calls) == 2
    assert count_rows(client, Link) == 2
    # The original link is untouched.
    assert client.get(f"/api/links/{existing}").json()["url"] == "https://example.com/first"


def test_all_attempts_collide_returns_503_without_traceback(client, monkeypatch, caplog):
    """Covers: FR-3, FR-11, NFR-3, NFR-5"""
    existing = create_link(client)["code"]
    calls = []

    def always_existing(length: int) -> str:
        calls.append(length)
        return existing

    monkeypatch.setattr("app.services.codes.generate_code", always_existing)
    with caplog.at_level(logging.INFO):
        resp = client.post("/api/links", json={"url": "https://example.com/other"})
    assert_json_error(resp, 503)
    assert isinstance(resp.json()["detail"], str)
    assert "Traceback" not in resp.text
    assert "IntegrityError" not in resp.text
    assert "UNIQUE" not in resp.text
    assert len(calls) == 5  # default CODE_MAX_ATTEMPTS
    assert count_rows(client, Link) == 1


def test_max_attempts_is_configurable(make_client, monkeypatch):
    """Covers: FR-3, FR-12"""
    client = make_client(code_max_attempts=2)
    existing = create_link(client)["code"]
    calls = []

    def always_existing(length: int) -> str:
        calls.append(length)
        return existing

    monkeypatch.setattr("app.services.codes.generate_code", always_existing)
    resp = client.post("/api/links", json={"url": "https://example.com/other"})
    assert_json_error(resp, 503)
    assert len(calls) == 2


@pytest.mark.parametrize("reserved", ["healthz", "readyz", "api", "docs", "redoc", "openapi.json"])
def test_reserved_candidate_is_discarded(client, monkeypatch, reserved):
    """Covers: FR-3, FR-10"""
    fake = _scripted([reserved, "Fresh02"])
    monkeypatch.setattr("app.services.codes.generate_code", fake)
    resp = client.post("/api/links", json={"url": "https://example.com/r"})
    assert resp.status_code == 201
    assert resp.json()["code"] == "Fresh02"
    assert count_rows(client, Link) == 1


def test_reserved_only_candidates_exhaust_to_503(client, monkeypatch):
    """Covers: FR-3"""
    monkeypatch.setattr("app.services.codes.generate_code", lambda length: "Healthz")
    resp = client.post("/api/links", json={"url": "https://example.com/r"})
    assert_json_error(resp, 503)
    assert count_rows(client, Link) == 0


def test_1000_links_have_unique_codes(client):
    """Covers: FR-3"""
    seen = set()
    for i in range(1000):
        resp = client.post("/api/links", json={"url": f"https://example.com/{i}"})
        assert resp.status_code == 201
        code = resp.json()["code"]
        assert CODE_RE.fullmatch(code)
        seen.add(code)
    assert len(seen) == 1000
    assert count_rows(client, Link) == 1000
