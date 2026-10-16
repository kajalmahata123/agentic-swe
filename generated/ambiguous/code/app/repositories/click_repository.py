"""Persistence access for click events. Flushes only; never commits or rolls back.

Daily and hourly statistics are aggregated in SQL (``GROUP BY`` over a
dialect-specific bucket expression), so memory is proportional to the number of
buckets rather than the number of clicks. The bucket expressions contain no bound
parameters, so the ``GROUP BY`` expression matches the selected one on every
dialect. On SQLite the stored text is cut to whole seconds before the date functions
see it, so clicks just before midnight or the top of an hour land in the right bucket.
The statement builders are module-level so tests can compile them for
SQLite, PostgreSQL and MySQL without a database connection.
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, Date, Select, cast, func, literal_column, select
from sqlalchemy.orm import Session

from app.models.base import to_naive_utc
from app.models.click_event import ClickEvent

_MYSQL_DIALECTS = frozenset({"mysql", "mariadb"})


def _sqlite_whole_seconds() -> ColumnElement[Any]:
    """Return ``created_at`` as stored on SQLite, cut to ``YYYY-MM-DD HH:MM:SS``.

    SQLAlchemy stores SQLite datetimes as text with microseconds
    (``2025-12-31 23:59:59.999999``). SQLite's date functions do not handle that
    fractional part exactly: they round to milliseconds, and some versions (e.g.
    3.45.1) even return the wrong day for ``2025-12-31 23:59:59.999999``. Cutting the
    text to whole seconds first makes day and hour bucketing exact. The positions are
    literal SQL, so the bucket expression carries no bound parameters.
    """
    return func.substr(ClickEvent.created_at, literal_column("1"), literal_column("19"))


def day_bucket_expression(dialect_name: str) -> ColumnElement[Any]:
    """Return the SQL expression truncating ``ClickEvent.created_at`` to its UTC day."""
    if dialect_name == "sqlite":
        return func.date(_sqlite_whole_seconds())
    if dialect_name in _MYSQL_DIALECTS:
        return func.date(ClickEvent.created_at)
    return cast(ClickEvent.created_at, Date)


def hour_bucket_expression(dialect_name: str) -> ColumnElement[Any]:
    """Return the SQL expression truncating ``ClickEvent.created_at`` to its UTC hour."""
    if dialect_name == "sqlite":
        return func.strftime(literal_column("'%Y-%m-%d %H:00:00'"), _sqlite_whole_seconds())
    if dialect_name in _MYSQL_DIALECTS:
        return func.timestampadd(
            literal_column("HOUR"),
            func.hour(ClickEvent.created_at),
            func.date(ClickEvent.created_at),
        )
    return func.date_trunc(literal_column("'hour'"), ClickEvent.created_at)


def build_daily_counts_stmt(link_id: int, since: datetime, dialect_name: str) -> Select[Any]:
    """Build ``SELECT day, COUNT(id)`` for a link's clicks at or after ``since``."""
    bucket = day_bucket_expression(dialect_name).label("bucket")
    return (
        select(bucket, func.count(ClickEvent.id))
        .where(ClickEvent.link_id == link_id, ClickEvent.created_at >= since)
        .group_by(bucket)
        .order_by(bucket)
    )


def build_hourly_counts_stmt(
    link_id: int, start: datetime, end: datetime, dialect_name: str
) -> Select[Any]:
    """Build ``SELECT hour, COUNT(id)`` for a link's clicks in ``[start, end)``."""
    bucket = hour_bucket_expression(dialect_name).label("bucket")
    return (
        select(bucket, func.count(ClickEvent.id))
        .where(
            ClickEvent.link_id == link_id,
            ClickEvent.created_at >= start,
            ClickEvent.created_at < end,
        )
        .group_by(bucket)
        .order_by(bucket)
    )


def _as_date(value: str | datetime | date) -> date:
    """Normalise a day-bucket value returned by the driver to a ``date``."""
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        return value.date()
    return value


def _as_hour(value: str | datetime) -> datetime:
    """Normalise an hour-bucket value returned by the driver to a naive UTC ``datetime``."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return to_naive_utc(parsed).replace(minute=0, second=0, microsecond=0)


class ClickRepository:
    """Insert and aggregate ``ClickEvent`` rows within the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def _dialect_name(self) -> str:
        return self.session.get_bind().dialect.name

    def add(self, event: ClickEvent) -> ClickEvent:
        """Add and flush a click event."""
        self.session.add(event)
        self.session.flush()
        return event

    def count_for_link(self, link_id: int) -> int:
        """Return the all-time number of click events for a link."""
        stmt = select(func.count(ClickEvent.id)).where(ClickEvent.link_id == link_id)
        return int(self.session.execute(stmt).scalar_one())

    def daily_counts(self, link_id: int, since: datetime) -> list[tuple[date, int]]:
        """Return ascending (UTC day, count) pairs for clicks at or after ``since``.

        Days without clicks are omitted. ``since`` is naive UTC.
        """
        stmt = build_daily_counts_stmt(link_id, since, self._dialect_name())
        rows = self.session.execute(stmt).all()
        return sorted((_as_date(bucket), int(count)) for bucket, count in rows)

    def hourly_counts(
        self, link_id: int, start: datetime, end: datetime
    ) -> list[tuple[datetime, int]]:
        """Return ascending (UTC hour, count) pairs for clicks in ``[start, end)``.

        Hours without clicks are omitted; ``start`` and ``end`` are naive UTC.
        """
        stmt = build_hourly_counts_stmt(link_id, start, end, self._dialect_name())
        rows = self.session.execute(stmt).all()
        return sorted((_as_hour(bucket), int(count)) for bucket, count in rows)

    def referrer_counts(self, link_id: int) -> list[tuple[str | None, int]]:
        """Return (referrer, count) pairs for a link, unordered; NULL referrers group together."""
        stmt = (
            select(ClickEvent.referrer, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link_id)
            .group_by(ClickEvent.referrer)
        )
        return [(referrer, int(count)) for referrer, count in self.session.execute(stmt).all()]
