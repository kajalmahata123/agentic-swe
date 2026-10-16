"""Shared fixtures: every test gets its own app wired to a fresh temporary SQLite DB."""

from __future__ import annotations

import os
import tempfile

# Make sure importing app.main (which builds a module-level app) can never point at the
# developer database, and that no ambient configuration leaks into the tests.
_IMPORT_DB_DIR = tempfile.mkdtemp(prefix="shortener-import-")
os.environ["DATABASE_URL"] = f"sqlite:///{_IMPORT_DB_DIR}/import.db"
for _var in (
    "BASE_URL",
    "SHORT_CODE_LENGTH",
    "CODE_MAX_ATTEMPTS",
    "IP_HASH_SALT",
    "LOG_LEVEL",
    "API_KEY",
):
    os.environ.pop(_var, None)

import pytest  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from helpers import TEST_API_KEY  # noqa: E402

TEST_BASE_URL = "http://sho.rt"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "slow: performance tests (NFR-4)")


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
