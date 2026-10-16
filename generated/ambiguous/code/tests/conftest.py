"""Shared fixtures: every test gets its own app wired to a fresh temporary SQLite DB."""

from __future__ import annotations

import os
import tempfile

# Make sure importing app.main (which builds a module-level app) can never point at the
# developer database, and that no ambient configuration leaks into the tests.
_IMPORT_DB_DIR = tempfile.mkdtemp(prefix="shortener-import-")
os.environ["DATABASE_URL"] = f"sqlite:///{_IMPORT_DB_DIR}/import.db"
STRIPPED_ENV_VARS = (
    "BASE_URL",
    "SHORT_CODE_LENGTH",
    "CODE_MAX_ATTEMPTS",
    "IP_HASH_SALT",
    "LOG_LEVEL",
    "API_KEY",
    "SQLITE_BUSY_TIMEOUT_MS",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "DB_POOL_TIMEOUT_SECONDS",
)
for _var in STRIPPED_ENV_VARS:
    os.environ.pop(_var, None)

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from helpers import TEST_API_KEY  # noqa: E402

TEST_BASE_URL = "http://sho.rt"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: performance/scale tests (NFR-4, NFR-10, NFR-11, NFR-13, NFR-19, NFR-21, FR-18)",
    )


@pytest.fixture
def make_client(tmp_path):
    """Factory building an isolated app + TestClient (lifespan entered) per call.

    The app is configured with ``TEST_API_KEY`` and the client sends it as
    ``X-API-Key`` on every request, so management endpoints are authenticated by
    default. Pass ``send_api_key=False`` to get a client without the header.
    """
    opened: list[TestClient] = []

    def _make(send_api_key: bool = True, **overrides) -> TestClient:
        params = {
            "database_url": f"sqlite:///{tmp_path}/test-{len(opened)}.db",
            "base_url": TEST_BASE_URL,
            "api_key": TEST_API_KEY,
        }
        params.update(overrides)
        app = create_app(Settings(**params))
        headers = {"X-API-Key": TEST_API_KEY} if send_api_key else None
        tc = TestClient(app, raise_server_exceptions=False, headers=headers)
        tc.__enter__()
        opened.append(tc)
        return tc

    yield _make
    for tc in opened:
        tc.app.dependency_overrides.clear()
        tc.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()


class FrozenClock:
    """A settable 'now' (naive UTC) patched into the service modules' ``utcnow``."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def set(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FrozenClock:
    """Freeze the service-layer clock (LinkService._utcnow and StatsService default now).

    ``LinkService._utcnow`` resolves ``app.services.link_service.utcnow`` at call time,
    so patching the module attribute drives the expiry rule for GET, HEAD, detail,
    create and PATCH deterministically. Row timestamps (``created_at``) still use the
    real clock.
    """
    frozen = FrozenClock(datetime(2025, 11, 20, 12, 0, 0))
    monkeypatch.setattr("app.services.link_service.utcnow", frozen)
    monkeypatch.setattr("app.services.stats_service.utcnow", frozen)
    return frozen
