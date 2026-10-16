"""Persistence access for links. Flushes only; never commits or rolls back."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.link import Link


class LinkRepository:
    """Query and insert ``Link`` rows within the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_code(self, code: str) -> Link | None:
        """Return the link with the given code, or None."""
        stmt = select(Link).where(Link.code == code)
        return self.session.execute(stmt).scalar_one_or_none()

    def add(self, link: Link) -> Link:
        """Add and flush a link; an ``IntegrityError`` (e.g. duplicate code) propagates."""
        self.session.add(link)
        self.session.flush()
        return link
