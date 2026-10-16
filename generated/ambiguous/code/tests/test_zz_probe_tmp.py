"""Regression: SQLite day/hour bucket expressions are exact for microsecond timestamps.

SQLite's date functions round fractional seconds, and some versions (3.45.1) return
the wrong day for '2025-12-31 23:59:59.999999'. The bucket expressions cut the stored
text to whole seconds first (FR-18, FR-25, NFR-22).
"""

from __future__ import annotations

from datetime import datetime

import pytest
from app.models import Base, ClickEvent, Link
from app.repositories.click_repository import day_bucket_expression, hour_bucket_expression
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


@pytest.mark.parametrize(
    ("ts", "day", "hour"),
    [
        (datetime(2025, 12, 31, 23, 59, 59, 999999), "2025-12-31", "2025-12-31 23:00:00"),
        (datetime(2026, 1, 1, 0, 59, 59, 999700), "2026-01-01", "2026-01-01 00:00:00"),
        (datetime(2024, 2, 29, 23, 59, 59, 999500), "2024-02-29", "2024-02-29 23:00:00"),
        (datetime(2026, 1, 1, 0, 0, 0), "2026-01-01", "2026-01-01 00:00:00"),
    ],
)
def test_sqlite_bucket_expressions_exact(ts, day, hour):
    """Covers: FR-18, FR-25, NFR-22"""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as s:
            link = Link(code="Probe1", url="https://example.com/p", is_active=True)
            s.add(link)
            s.flush()
            s.add(ClickEvent(link_id=link.id, created_at=ts))
            s.commit()
            got = s.execute(
                select(day_bucket_expression("sqlite"), hour_bucket_expression("sqlite"))
            ).one()
        assert tuple(got) == (day, hour)
    finally:
        engine.dispose()
