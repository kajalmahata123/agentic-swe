"""Pydantic request/response schemas."""

from app.schemas.health import HealthResponse
from app.schemas.links import (
    MAX_URL_LENGTH,
    DailyClicks,
    ErrorResponse,
    LinkCreate,
    LinkCreatedResponse,
    LinkDetailResponse,
    LinkStatsResponse,
    ReferrerCount,
    validate_target_url,
)

__all__ = [
    "MAX_URL_LENGTH",
    "DailyClicks",
    "ErrorResponse",
    "HealthResponse",
    "LinkCreate",
    "LinkCreatedResponse",
    "LinkDetailResponse",
    "LinkStatsResponse",
    "ReferrerCount",
    "validate_target_url",
]
