"""Documentation accuracy (FR-20), the campaign runbook checklist (NFR-20) and the
no-new-dependency rule (NFR-17)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from app.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
API = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
OPS = (ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """Return the body of the '## heading' section up to the next '## ' heading."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, flags=re.MULTILINE)
    assert match, f"missing section: {heading}"
    rest = text[match.end():]
    nxt = re.search(r"^## ", rest, flags=re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _unreleased() -> str:
    match = re.search(r"^## \[Unreleased\]\s*$", CHANGELOG, flags=re.MULTILINE)
    assert match
    rest = CHANGELOG[match.end():]
    nxt = re.search(r"^## \[", rest, flags=re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


@pytest.mark.parametrize("name,text", [("README", README), ("API", API), ("OPS", OPS)])
def test_docs_describe_api_key_auth(name, text):
    """Covers: FR-20, FR-21"""
    lowered = text.lower()
    for phrase in (
        r"\bno authentication\b",
        r"\bwithout authentication\b",
        r"authentication is not (built in|implemented|required)",
        r"\bno auth\b",
    ):
        assert not re.search(phrase, lowered), (name, phrase)
    assert "X-API-Key" in text, name
    assert "401" in text, name
    assert "fail closed" in lowered or "fails closed" in lowered, name
    assert "16 characters" in text, name
    assert "API_KEY" in text, name


def test_readme_lists_every_settings_env_var():
    """Covers: FR-20"""
    config = _section(README, "Configuration")
    for field in Settings.model_fields:
        env = field.upper()
        assert f"`{env}`" in config, env
    for env in ("API_KEY", "SQLITE_BUSY_TIMEOUT_MS", "DB_POOL_SIZE", "DB_MAX_OVERFLOW",
                "DB_POOL_TIMEOUT_SECONDS"):
        assert f"`{env}`" in config


def test_api_doc_covers_new_endpoints_and_errors():
    """Covers: FR-20, FR-14, FR-23, FR-24, FR-25, FR-22"""
    for needle in (
        "HEAD /healthz",
        "HEAD /readyz",
        "expires_at",
        "is_expired",
        "PATCH /api/links/{code}",
        "GET /api/links/{code}/stats/hourly",
        "744",
        "%2B",
        "count\": 0",
        "Link has expired",
        "Link has been deactivated",
        "start and end must be supplied together",
        "expires_at must be in the future",
    ):
        assert needle in API, needle
    errors = _section(API, "Error format")
    for status in ("| 401 |", "| 422 |", "| 410 |", "| 503 |"):
        assert status in errors, status
    hourly = _section(API, "GET /api/links/{code}/stats/hourly")
    assert '"clicks_per_hour"' in hourly
    assert "zero" in hourly.lower() or "count: 0" in hourly


def test_operations_doc_covers_wal_backups_locking_pool_and_upgrade():
    """Covers: FR-20, FR-15, FR-16, FR-26"""
    for needle in (
        "-wal",
        "-shm",
        ".backup",
        "all three files",
        "network",
        "local",
        "SQLITE_BUSY_TIMEOUT_MS",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
        "ALTER TABLE links ADD COLUMN expires_at DATETIME",
        "ALTER TABLE links ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE",
        "schema upgrade: links.expires_at added",
        "schema upgrade: links.expires_at already present",
        "schema upgrade failed: could not add links.expires_at",
        "failed to record click for link_id=",
    ):
        assert needle in OPS, needle
    assert re.search(r"back up .*before deploying", OPS, flags=re.IGNORECASE)
    locking = OPS.lower()
    assert "readers are not blocked" in locking
    assert "still returns 307" in locking
    assert "503" in OPS


def test_operations_doc_lookup_failure_returns_503():
    """Covers: FR-15, FR-20"""
    locking = _section(OPS, "SQLite: WAL, backups and locking")
    assert re.search(r"link lookup itself fails.*?503", locking, flags=re.DOTALL | re.IGNORECASE)


def test_campaign_deployment_section():
    """Covers: FR-20, NFR-20"""
    section = _section(OPS, "Campaign deployment")
    for needle in (
        "~50",
        "p95 < 100 ms",
        "Best effort",
        "One instance",
        "--workers 1",
        "WAL",
        "SQLITE_BUSY_TIMEOUT_MS",
        "DB_POOL_SIZE",
        "expires_at",
        "PATCH",
        "POST",
        "GET /api/links/{code}",
        "is_expired",
        "/stats/hourly",
        "UTC",
        "%2B",
        "+05:30",
        "HEAD /readyz",
        "curl -I",
        "backup",
        "schema upgrade",
        "tuning line",
        "journal_mode=wal",
        "Pre-launch checklist",
        "Restart and recovery",
    ):
        assert needle in section, needle
    checklist = section.split("Pre-launch checklist", 1)[1].split("Restart and recovery", 1)[0]
    assert checklist.count("- [ ]") >= 6
    for item in ("healthz", "readyz", "backup", "expires_at", "hourly", "50 RPS", "p95 < 100 ms"):
        assert item in checklist, item


def test_scaling_beyond_campaign_section():
    """Covers: FR-20"""
    section = _section(OPS, "Scaling beyond the campaign profile")
    lowered = section.lower()
    assert "multiple uvicorn workers" in lowered
    assert "multiple replicas" in lowered
    assert "50 rps" in lowered
    assert "requires postgresql" in lowered
    assert "no postgresql driver ships in this release" in lowered
    for step in ("driver", "DATABASE_URL", "fresh", "Copy the data", "pool"):
        assert step.lower() in lowered, step
    assert "DB_POOL_SIZE + DB_MAX_OVERFLOW" in section


def test_changelog_unreleased_records_all_changes():
    """Covers: FR-20"""
    text = _unreleased().lower()
    for needle in (
        "api-key",
        "head /healthz",
        "head /readyz",
        "wal",
        "sqlite_busy_timeout_ms",
        "db_pool_size",
        "aggregated in sql",
        "links.expires_at",
        "schema upgrade",
        "link has expired",
        "patch /api/links/{code}",
        "/stats/hourly",
        "collation",
    ):
        assert needle in text, needle


def test_requirements_pinned_and_no_new_runtime_dependency():
    """Covers: NFR-17"""
    lines = [
        ln.strip()
        for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    names = {ln.split("==")[0].lower() for ln in lines}
    assert names == {
        "fastapi", "uvicorn", "sqlalchemy", "pydantic", "pydantic-settings", "httpx",
        "pytest", "pytest-cov",
    }
    assert all("==" in ln for ln in lines)
    assert not any(d in names for d in ("psycopg", "psycopg2", "psycopg2-binary", "pymysql",
                                        "mysqlclient", "alembic"))
