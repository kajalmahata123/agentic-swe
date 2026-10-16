"""Persistence access for links. Flushes only; never commits or rolls back."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.link import Link


@dataclass(frozen=True, slots=True)
class RedirectTarget:
    """The minimal projection of a link needed to serve a redirect.

    ``expires_at`` is naive UTC; ``None`` means the link never expires.
    """

    id: int
    url: str
    is_active: bool
    expires_at: datetime | None


class LinkRepository:
    """Query and insert ``Link`` rows within the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_code(self, code: str) -> Link | None:
        """Return the link with the given code, or None."""
        stmt = select(Link).where(Link.code == code)
        return self.session.execute(stmt).scalar_one_or_none()

    def get_redirect_target(self, code: str) -> RedirectTarget | None:
        """Return the redirect projection for ``code``, or None.

        Issues exactly one SELECT of four columns and loads no ORM entity, which
        keeps the redirect hot path lean.
        """
        stmt = select(Link.id, Link.url, Link.is_active, Link.expires_at).where(
            Link.code == code
        )
        row = self.session.execute(stmt).first()
        if row is None:
            return None
        return RedirectTarget(
            id=row.id,
            url=row.url,
            is_active=bool(row.is_active),
            expires_at=row.expires_at,
        )

    def add(self, link: Link) -> Link:
        """Add and flush a link; an ``IntegrityError`` (e.g. duplicate code) propagates."""
        self.session.add(link)
        self.session.flush()
        return link
