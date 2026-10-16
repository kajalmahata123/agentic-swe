"""Per-link click statistics.

Daily and hourly click counts are aggregated in SQL (``GROUP BY`` over a UTC day or
hour bucket), so memory is proportional to the number of buckets rather than the
number of clicks. Referrer counts also come from SQL ``GROUP BY`` and are ordered
here. All timestamps are naive UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core.errors import InvalidInputError, NotFoundError
from app.models import Link, to_naive_utc, utcnow
from app.repositories import ClickRepository, LinkRepository
from app.services import codes

TOP_REFERRERS_LIMIT = 10
MAX_HOURLY_RANGE_HOURS = 744
DEFAULT_HOURLY_LOOKBACK_HOURS = 24

_ONE_HOUR = timedelta(hours=1)
_OUT_OF_RANGE_DETAIL = "datetime out of range"


@dataclass
class LinkStats:
    """Aggregated analytics for one link."""

    code: str
    total_clicks: int
    clicks_per_day: list[tuple[date, int]]
    top_referrers: list[tuple[str | None, int]]


@dataclass
class HourlyStats:
    """Zero-filled clicks per UTC hour for one link over ``[start, end)``."""

    code: str
    start: datetime
    end: datetime
    total_clicks: int
    clicks_per_hour: list[tuple[datetime, int]]


def _floor_hour(value: datetime) -> datetime:
    """Truncate ``value`` to the top of its hour."""
    return value.replace(minute=0, second=0, microsecond=0)


class StatsService:
    """Read-only analytics over click events."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.link_repo = LinkRepository(session)
        self.click_repo = ClickRepository(session)

    def _get_link_or_404(self, code: str) -> Link:
        """Return the link for ``code`` (any state) or raise ``NotFoundError``."""
        if not codes.is_valid_code_format(code):
            raise NotFoundError()
        link = self.link_repo.get_by_code(code)
        if link is None:
            raise NotFoundError()
        return link

    def get_stats(self, code: str, days: int = 30, now: datetime | None = None) -> LinkStats:
        """Return stats for ``code`` (active, deactivated or expired).

        ``clicks_per_day`` covers the last ``days`` UTC calendar days including today and
        lists only days with clicks, ascending. ``total_clicks`` and ``top_referrers``
        are all-time; referrers are ordered by count desc, then referrer asc with
        ``None`` after non-null values on ties, limited to ``TOP_REFERRERS_LIMIT``.

        Raises:
            NotFoundError: the code is malformed or no such link exists.
        """
        link = self._get_link_or_404(code)

        now = utcnow() if now is None else to_naive_utc(now)

        start_date = now.date() - timedelta(days=days - 1)
        since = datetime.combine(start_date, time.min)

        total = self.click_repo.count_for_link(link.id)
        clicks_per_day = self.click_repo.daily_counts(link.id, since)

        refs = sorted(
            self.click_repo.referrer_counts(link.id),
            key=lambda r: (-r[1], r[0] is None, r[0] or ""),
        )[:TOP_REFERRERS_LIMIT]

        return LinkStats(
            code=link.code,
            total_clicks=total,
            clicks_per_day=clicks_per_day,
            top_referrers=refs,
        )

    def get_hourly_clicks(
        self,
        code: str,
        start: datetime | None = None,
        end: datetime | None = None,
        now: datetime | None = None,
    ) -> HourlyStats:
        """Return zero-filled clicks per UTC hour for ``code`` over ``[start, end)``.

        ``start`` is floored to the top of its UTC hour and ``end`` rounded up to the
        next top of hour (unchanged when already aligned); offsets are converted to
        UTC and naive values are treated as UTC. When both are omitted the range is
        the last ``DEFAULT_HOURLY_LOOKBACK_HOURS`` complete hours plus the current
        hour. Works for active, deactivated and expired links.

        Raises:
            InvalidInputError: only one bound given, an out-of-range datetime, an
                empty range after alignment, or more than ``MAX_HOURLY_RANGE_HOURS``.
            NotFoundError: the code is malformed or no such link exists.
        """
        if (start is None) != (end is None):
            raise InvalidInputError("start and end must be supplied together")

        try:
            if start is not None:
                start = to_naive_utc(start)
            if end is not None:
                end = to_naive_utc(end)
            now = utcnow() if now is None else to_naive_utc(now)
        except OverflowError:
            raise InvalidInputError(_OUT_OF_RANGE_DETAIL) from None

        try:
            if start is None or end is None:
                current_hour = _floor_hour(now)
                start = current_hour - timedelta(hours=DEFAULT_HOURLY_LOOKBACK_HOURS)
                end = current_hour + _ONE_HOUR
            else:
                start = _floor_hour(start)
                end_floor = _floor_hour(end)
                end = end_floor if end_floor == end else end_floor + _ONE_HOUR
        except OverflowError:
            raise InvalidInputError(_OUT_OF_RANGE_DETAIL) from None

        if start >= end:
            raise InvalidInputError("start must be before end")

        hours = int((end - start).total_seconds() // 3600)
        if hours > MAX_HOURLY_RANGE_HOURS:
            raise InvalidInputError(f"range must not exceed {MAX_HOURLY_RANGE_HOURS} hours")

        link = self._get_link_or_404(code)

        counts = dict(self.click_repo.hourly_counts(link.id, start, end))
        buckets: list[tuple[datetime, int]] = []
        for offset in range(hours):
            hour = start + timedelta(hours=offset)
            buckets.append((hour, counts.get(hour, 0)))
        total = sum(count for _, count in buckets)

        return HourlyStats(
            code=link.code,
            start=start,
            end=end,
            total_clicks=total,
            clicks_per_hour=buckets,
        )
