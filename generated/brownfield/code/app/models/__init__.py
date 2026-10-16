"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from app.models.base import Base, utcnow
from app.models.click_event import ClickEvent
from app.models.idempotency_key import IdempotencyKey
from app.models.link import Link

__all__ = ["Base", "utcnow", "Link", "ClickEvent", "IdempotencyKey"]
