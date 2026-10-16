"""Pydantic v2 request/response models for link management and stats.

Also hosts the strict target-URL validation rules (FR-2) and the parsing and
UTC normalisation of link expiry instants (FR-23, FR-24). Whether an expiry is
in the future depends on the server clock and is checked in the service layer,
not here. This module imports nothing from other ``app`` packages.
"""

import datetime as dt
import urllib.parse
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator

MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_forbidden_char(c: str) -> bool:
    """Return True for whitespace, ASCII control and non-ASCII characters."""
    code = ord(c)
    return code <= 0x20 or code == 0x7F or code > 0x7E or c.isspace()


def validate_target_url(value: str) -> str:
    """Validate a target URL and return it unchanged.

    Accepts only absolute ``http``/``https`` URLs (scheme case-insensitive) with a
    non-empty host, at most ``MAX_URL_LENGTH`` characters, containing no
    whitespace, control or non-ASCII characters. No normalization is applied.

    Raises:
        ValueError: with a short message describing the first failed rule.

    >>> validate_target_url("HTTPS://example.com")
    'HTTPS://example.com'
    >>> validate_target_url("ftp://example.com")
    Traceback (most recent call last):
    ...
    ValueError: URL scheme must be http or https
    """
    if len(value) == 0:
        raise ValueError("URL must not be empty")
    if len(value) > MAX_URL_LENGTH:
        raise ValueError(f"URL must be at most {MAX_URL_LENGTH} characters")
    if any(_is_forbidden_char(c) for c in value):
        raise ValueError("URL must not contain whitespace, control or non-ASCII characters")
    try:
        parts = urllib.parse.urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ValueError("invalid URL") from None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError("URL scheme must be http or https")
    if not value[len(parts.scheme):].startswith("://"):
        raise ValueError("URL must be absolute")
    if not parts.netloc or not parts.hostname:
        raise ValueError("URL must include a host")
    return value


def _as_utc(v: dt.datetime | None) -> dt.datetime | None:
    """Return ``v`` as a tz-aware UTC datetime; naive values are treated as UTC."""
    if v is None:
        return None
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        return v.replace(tzinfo=dt.UTC)
    return v.astimezone(dt.UTC)


def _to_naive_utc(v: dt.datetime) -> dt.datetime:
    """Return ``v`` as a naive UTC datetime; naive values are assumed to be UTC.

    Raises:
        ValueError: if converting an offset value to UTC leaves the supported
            datetime range (e.g. year 9999 with a negative offset).
    """
    if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
        return v
    try:
        return v.astimezone(dt.UTC).replace(tzinfo=None)
    except OverflowError:
        raise ValueError("datetime is out of range") from None


def _require_datetime_input(v: Any) -> Any:
    """Accept only ``None``, strings or datetimes as raw ``expires_at`` input.

    Pydantic's lax mode would otherwise interpret numbers (and booleans) as Unix
    timestamps; the API contract only allows ISO 8601 strings or ``null``.
    """
    if v is None or isinstance(v, (str, dt.datetime)):
        return v
    raise ValueError("expires_at must be an ISO 8601 datetime string or null")


def _normalise_expiry(v: dt.datetime | None) -> dt.datetime | None:
    """Normalise a parsed ``expires_at`` to naive UTC, keeping ``None``."""
    return None if v is None else _to_naive_utc(v)


class LinkCreate(BaseModel):
    """Request body for ``POST /api/links``.

    ``expires_at`` is optional; offset values are converted to UTC and naive
    values are treated as UTC. The result is stored as naive UTC.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"url": "https://example.com/a?b=1"},
                {"url": "https://example.com/campaign", "expires_at": "2025-12-31T23:59:00Z"},
            ]
        }
    )

    url: StrictStr
    expires_at: dt.datetime | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_target_url(value)

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at_input(cls, v: Any) -> Any:
        return _require_datetime_input(v)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_naive_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _normalise_expiry(v)


class LinkExpiryUpdate(BaseModel):
    """Request body for PATCH /api/links/{code}; null clears the expiry."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"expires_at": "2025-12-31T23:59:00Z"},
                {"expires_at": None},
            ]
        },
    )

    expires_at: dt.datetime | None

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at_input(cls, v: Any) -> Any:
        return _require_datetime_input(v)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_naive_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _normalise_expiry(v)


class LinkCreatedResponse(BaseModel):
    """Metadata returned after creating a short link."""

    code: str
    short_url: str
    url: str
    is_active: bool
    created_at: dt.datetime
    expires_at: dt.datetime | None = None

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_utc(cls, v: dt.datetime) -> dt.datetime | None:
        return _as_utc(v)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _as_utc(v)


class LinkDetailResponse(LinkCreatedResponse):
    """Full link metadata, including deactivation and expiry state and click count."""

    deactivated_at: dt.datetime | None = None
    is_expired: bool = False
    total_clicks: int

    @field_validator("deactivated_at", mode="after")
    @classmethod
    def _deactivated_at_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _as_utc(v)


class DailyClicks(BaseModel):
    """Number of clicks on one UTC calendar date."""

    date: dt.date
    count: int


class ReferrerCount(BaseModel):
    """Number of clicks for one referrer (``None`` means no referrer)."""

    referrer: str | None
    count: int


class LinkStatsResponse(BaseModel):
    """Aggregated click statistics for a link."""

    code: str
    total_clicks: int
    clicks_per_day: list[DailyClicks]
    top_referrers: list[ReferrerCount]


class HourlyClicks(BaseModel):
    """Number of clicks in one UTC hour; ``hour`` is the top of that hour."""

    hour: dt.datetime
    count: int

    @field_validator("hour", mode="after")
    @classmethod
    def _hour_utc(cls, v: dt.datetime) -> dt.datetime | None:
        return _as_utc(v)


class LinkHourlyStatsResponse(BaseModel):
    """Zero-filled clicks per UTC hour for a link over the half-open range [start, end)."""

    code: str
    start: dt.datetime
    end: dt.datetime
    total_clicks: int
    clicks_per_hour: list[HourlyClicks]

    @field_validator("start", "end", mode="after")
    @classmethod
    def _bounds_utc(cls, v: dt.datetime) -> dt.datetime | None:
        return _as_utc(v)


class ErrorResponse(BaseModel):
    """Error body shape used by every error response: ``{"detail": ...}``."""

    detail: Any
