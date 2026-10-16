"""Per-link click statistics.

Day bucketing is done in Python on naive-UTC timestamps so the query stays portable
across databases; referrer counts come from SQL GROUP BY and are sorted here.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.models import utcnow
from app.repositories import ClickRepository, LinkRepository
from app.services import codes

TOP_REFERRERS_LIMIT = 10


@dataclass
class LinkStats:
    """Aggregated analytics for one link."""

    code: str
    total_clicks: int
    clicks_per_day: list[tuple[date, int]]
    top_referrers: list[tuple[str | None, int]]


class StatsService:
    """Read-only analytics over click events."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.link_repo = LinkRepository(session)
        self.click_repo = ClickRepository(session)

    def get_stats(self, code: str, days: int = 30, now: datetime | None = None) -> LinkStats:
        """Return stats for ``code`` (active, deactivated or expired).

        ``clicks_per_day`` covers the last ``days`` UTC calendar days including today and
        lists only days with clicks, ascending. ``total_clicks`` and ``top_referrers``
        are all-time; referrers are ordered by count desc, then referrer asc with
        ``None`` after non-null values on ties, limited to ``TOP_REFERRERS_LIMIT``.

        Raises:
            NotFoundError: the code is malformed or no such link exists.
        """
        if not codes.is_valid_lookup_code(code):
            raise NotFoundError()
        link = self.link_repo.get_by_code(code)
        if link is None:
            raise NotFoundError()

        if now is None:
            now = utcnow()
        elif now.tzinfo is not None:
            now = now.astimezone(UTC).replace(tzinfo=None)

        start_date = now.date() - timedelta(days=days - 1)
        since = datetime.combine(start_date, time.min)

        total = self.click_repo.count_for_link(link.id)

        per_day = Counter(ts.date() for ts in self.click_repo.created_at_since(link.id, since))
        clicks_per_day = sorted(per_day.items())

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
