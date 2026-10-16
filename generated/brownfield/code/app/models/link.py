"""Link ORM model (table: links)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class Link(Base):
    """A shortened link. Rows are never deleted; deactivation is a soft delete."""

    __tablename__ = "links"
    __table_args__ = (UniqueConstraint("code", name="uq_links_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Naive UTC; NULL means the link never expires (CR-142).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def is_expired_at(self, now: datetime) -> bool:
        """Return True when the link has an expiry at or before ``now``.

        Both ``expires_at`` and ``now`` are naive UTC; the exact instant counts as expired.
        """
        return self.expires_at is not None and self.expires_at <= now
