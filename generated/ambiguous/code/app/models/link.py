"""Link ORM model (table: links)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, utcnow


class Link(Base):
    """A shortened link. Rows are never deleted; deactivation is a soft delete.

    ``expires_at`` is a naive UTC instant from which the link stops redirecting;
    NULL means the link never expires. On MySQL/MariaDB ``code`` uses the binary
    ``utf8mb4_bin`` collation so short codes match case-sensitively.
    """

    __tablename__ = "links"
    __table_args__ = (UniqueConstraint("code", name="uq_links_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        String(32).with_variant(String(32, collation="utf8mb4_bin"), "mysql", "mariadb"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
