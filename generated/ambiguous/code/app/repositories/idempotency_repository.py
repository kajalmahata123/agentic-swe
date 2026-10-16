"""Persistence access for idempotency keys. Flushes only; never commits or rolls back."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.idempotency_key import IdempotencyKey


class IdempotencyRepository:
    """Query and insert ``IdempotencyKey`` rows within the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_by_key(self, key: str) -> IdempotencyKey | None:
        """Return the record for the given Idempotency-Key, or None."""
        stmt = select(IdempotencyKey).where(IdempotencyKey.key == key)
        return self.session.execute(stmt).scalar_one_or_none()

    def add(self, record: IdempotencyKey) -> IdempotencyKey:
        """Add and flush a record; an ``IntegrityError`` (duplicate key) propagates."""
        self.session.add(record)
        self.session.flush()
        return record
