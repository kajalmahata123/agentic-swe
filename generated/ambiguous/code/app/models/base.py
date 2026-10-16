"""Declarative base and shared helpers for the ORM layer."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (portable across databases)."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_naive_utc(value: datetime) -> datetime:
    """Return ``value`` as a naive UTC datetime.

    Naive values are assumed to already be UTC and are returned unchanged. Aware values
    are converted to UTC and stripped of their tzinfo. May raise ``OverflowError`` for
    values at the edge of the datetime range; callers handle it.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
