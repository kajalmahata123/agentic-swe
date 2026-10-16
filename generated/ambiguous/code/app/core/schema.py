"""Idempotent startup schema upgrade.

``Base.metadata.create_all`` never alters existing tables, so columns added after a
database was created are applied here. Only literal SQL is executed.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect

logger = logging.getLogger("app.db")


def ensure_links_expires_at(engine: Engine) -> bool:
    """Add the nullable ``links.expires_at`` column if it is missing.

    Returns True if the column was added, False if it was already present. Any failure
    is logged and re-raised so application startup aborts.
    """
    try:
        with engine.connect() as conn:
            columns = {column["name"] for column in inspect(conn).get_columns("links")}
        if "expires_at" in columns:
            logger.info(
                "schema upgrade: links.expires_at already present",
                extra={"schema_upgrade": "already_present"},
            )
            return False
        with engine.begin() as conn:
            if engine.dialect.name == "postgresql":
                conn.exec_driver_sql(
                    "ALTER TABLE links ADD COLUMN expires_at TIMESTAMP WITHOUT TIME ZONE"
                )
            else:
                conn.exec_driver_sql("ALTER TABLE links ADD COLUMN expires_at DATETIME")
        logger.info(
            "schema upgrade: links.expires_at added",
            extra={"schema_upgrade": "added"},
        )
        return True
    except Exception:
        logger.exception("schema upgrade failed: could not add links.expires_at")
        raise
