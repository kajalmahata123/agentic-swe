"""Pydantic request/response schemas."""

from app.schemas.health import HealthResponse
from app.schemas.links import (
    MAX_URL_LENGTH,
    DailyClicks,
    ErrorResponse,
    HourlyClicks,
    LinkCreate,
    LinkCreatedResponse,
    LinkDetailResponse,
    LinkExpiryUpdate,
    LinkHourlyStatsResponse,
    LinkStatsResponse,
    ReferrerCount,
    validate_target_url,
)

__all__ = [
    "MAX_URL_LENGTH",
    "DailyClicks",
    "ErrorResponse",
    "HealthResponse",
    "HourlyClicks",
    "LinkCreate",
    "LinkCreatedResponse",
    "LinkDetailResponse",
    "LinkExpiryUpdate",
    "LinkHourlyStatsResponse",
    "LinkStatsResponse",
    "ReferrerCount",
    "validate_target_url",
]
