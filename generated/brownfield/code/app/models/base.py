"""Declarative base and shared helpers for the ORM layer."""

from datetime import UTC, datetime

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base class for all ORM models."""


def utcnow() -> datetime:
    """Return the current UTC time as a naive datetime (portable across databases)."""
    return datetime.now(UTC).replace(tzinfo=None)
