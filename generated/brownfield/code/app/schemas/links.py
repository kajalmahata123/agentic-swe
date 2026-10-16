"""Pydantic v2 request/response models for link management and stats.

Also hosts the strict target-URL validation rules (FR-2). This module imports
nothing from other ``app`` packages.
"""

import datetime as dt
import re
import urllib.parse
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

MAX_URL_LENGTH = 2048
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Alias format rule. It mirrors app.services.codes.ALIAS_PATTERN, which is the
# authoritative copy; it is repeated here because schemas must not import app.*.
ALIAS_REGEX = r"^[A-Za-z0-9_-]+$"
ALIAS_MIN_LENGTH = 3
ALIAS_MAX_LENGTH = 32
_ALIAS_FULL_RE = re.compile(r"[A-Za-z0-9_-]{3,32}")

# Strict ISO-8601 date-time shape accepted for ``expires_at`` (used with fullmatch):
# YYYY-MM-DD[T ]HH:MM[:SS[.ffffff]][Z|+HH:MM|-HH:MM]
_ISO_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?([Zz]|[+-]\d{2}:\d{2})?"
)


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


class LinkCreate(BaseModel):
    """Request body for ``POST /api/links``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"url": "https://example.com/a?b=1"},
                {
                    "url": "https://example.com/a?b=1",
                    "alias": "my-link_1",
                    "expires_at": "2030-01-01T12:00:00Z",
                },
            ]
        }
    )

    url: StrictStr
    alias: StrictStr | None = Field(
        default=None,
        min_length=ALIAS_MIN_LENGTH,
        max_length=ALIAS_MAX_LENGTH,
        pattern=ALIAS_REGEX,
        description=(
            "Custom short code (3-32 chars of A-Z a-z 0-9 _ -), case preserved; "
            "reserved words are rejected"
        ),
    )
    expires_at: dt.datetime | None = Field(
        default=None,
        description=(
            "Optional expiry, ISO-8601 date-time; naive values are UTC; "
            "must be in the future"
        ),
    )

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_target_url(value)

    @field_validator("alias", mode="after")
    @classmethod
    def _validate_alias(cls, v: str | None) -> str | None:
        """Re-check the full alias format (defence in depth); case is preserved."""
        if v is not None and not _ALIAS_FULL_RE.fullmatch(v):
            raise ValueError("alias must be 3-32 characters of A-Z, a-z, 0-9, _ or -")
        return v

    @field_validator("expires_at", mode="before")
    @classmethod
    def _expires_at_iso_only(cls, value: Any) -> Any:
        """Accept only None, datetime instances or strict ISO-8601 date-time strings.

        This blocks Pydantic's lax coercion of numbers and numeric strings into
        unix timestamps. Calendar validity (e.g. month 13) is left to Pydantic.
        """
        if value is None or isinstance(value, dt.datetime):
            return value
        if not isinstance(value, str) or not _ISO_DATETIME_RE.fullmatch(value):
            raise ValueError("expires_at must be an ISO-8601 date-time string")
        return value

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        """Normalise to aware UTC; the future check is done by the service."""
        return _as_utc(v)


class LinkCreatedResponse(BaseModel):
    """Metadata returned after creating a short link."""

    code: str
    short_url: str
    url: str
    is_active: bool
    created_at: dt.datetime
    expires_at: dt.datetime | None = None
    is_expired: bool = False

    @field_validator("created_at", mode="after")
    @classmethod
    def _created_at_utc(cls, v: dt.datetime) -> dt.datetime | None:
        return _as_utc(v)

    @field_validator("expires_at", mode="after")
    @classmethod
    def _expires_at_utc(cls, v: dt.datetime | None) -> dt.datetime | None:
        return _as_utc(v)


class LinkDetailResponse(LinkCreatedResponse):
    """Full link metadata, including deactivation state and click count."""

    deactivated_at: dt.datetime | None = None
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


class ErrorResponse(BaseModel):
    """Error body shape used by every error response: ``{"detail": ...}``."""

    detail: Any
