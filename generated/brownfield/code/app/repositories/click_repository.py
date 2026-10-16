"""Persistence access for click events. Flushes only; never commits or rolls back."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.click_event import ClickEvent


class ClickRepository:
    """Insert and aggregate ``ClickEvent`` rows within the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, event: ClickEvent) -> ClickEvent:
        """Add and flush a click event."""
        self.session.add(event)
        self.session.flush()
        return event

    def count_for_link(self, link_id: int) -> int:
        """Return the all-time number of click events for a link."""
        stmt = select(func.count(ClickEvent.id)).where(ClickEvent.link_id == link_id)
        return int(self.session.execute(stmt).scalar_one())

    def created_at_since(self, link_id: int, since: datetime) -> list[datetime]:
        """Return the timestamps of a link's clicks at or after ``since`` (naive UTC)."""
        stmt = select(ClickEvent.created_at).where(
            ClickEvent.link_id == link_id,
            ClickEvent.created_at >= since,
        )
        return list(self.session.execute(stmt).scalars().all())

    def referrer_counts(self, link_id: int) -> list[tuple[str | None, int]]:
        """Return (referrer, count) pairs for a link, unordered; NULL referrers group together."""
        stmt = (
            select(ClickEvent.referrer, func.count(ClickEvent.id))
            .where(ClickEvent.link_id == link_id)
            .group_by(ClickEvent.referrer)
        )
        return [(referrer, int(count)) for referrer, count in self.session.execute(stmt).all()]
