"""Liveness (``/healthz``) and readiness (``/readyz``) probes."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ServiceUnavailableError
from app.schemas import ErrorResponse, HealthResponse

router = APIRouter(tags=["health"])

logger = logging.getLogger("app.health")


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
def healthz() -> HealthResponse:
    """Report that the process is alive; never touches the database."""
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    response_model=HealthResponse,
    summary="Readiness probe",
    responses={503: {"model": ErrorResponse, "description": "Database is not ready"}},
)
def readyz(db: Annotated[Session, Depends(get_db)]) -> HealthResponse:
    """Report readiness after a trivial ``SELECT 1`` succeeds."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readiness check failed")
        raise ServiceUnavailableError("Database is not ready") from None
    return HealthResponse(status="ready")
