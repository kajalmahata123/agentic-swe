"""FastAPI dependency providers and small HTTP-layer helpers.

Every provider obtains its database session from :func:`app.core.db.get_db`, which is
the single override point used by tests.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.db import get_db
from app.services.link_service import LinkService
from app.services.stats_service import StatsService

API_KEY_HEADER = "X-API-Key"
UNAUTHORIZED_DETAIL = "Invalid or missing API key"

_api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    scheme_name="ApiKeyAuth",
    description="Shared management API key (API_KEY setting).",
)


def get_settings(request: Request) -> Settings:
    """Return the settings of the application serving this request."""
    settings: Settings = request.app.state.settings
    return settings


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    provided: Annotated[str | None, Security(_api_key_scheme)] = None,
) -> None:
    """Reject the request with 401 unless ``X-API-Key`` matches the configured key.

    The comparison runs in constant time. If no ``API_KEY`` is configured, every
    request is rejected (fail closed), so the management API is never exposed
    unauthenticated by accident.
    """
    expected = settings.api_key
    if (
        expected is None
        or not provided
        or not hmac.compare_digest(
            provided.encode("utf-8"), expected.get_secret_value().encode("utf-8")
        )
    ):
        raise HTTPException(
            status_code=401,
            detail=UNAUTHORIZED_DETAIL,
            headers={"WWW-Authenticate": "ApiKey"},
        )


def get_link_service(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> LinkService:
    """Build a request-scoped :class:`LinkService` configured from the app settings.

    ``app.state.clock`` (an optional callable returning the current datetime) is read
    on every request so tests can control "now"; ``None`` means the real UTC clock.
    """
    return LinkService(
        db,
        code_length=settings.short_code_length,
        max_attempts=settings.code_max_attempts,
        ip_hash_salt=settings.ip_hash_salt,
        clock=getattr(request.app.state, "clock", None),
    )


def get_stats_service(db: Annotated[Session, Depends(get_db)]) -> StatsService:
    """Build a request-scoped :class:`StatsService`."""
    return StatsService(db)


def build_short_url(base_url: str, code: str) -> str:
    """Join the public base URL (trailing slashes stripped) and a short code."""
    return base_url.rstrip("/") + "/" + code
