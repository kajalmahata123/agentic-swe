"""Liveness (``/healthz``) and readiness (``/readyz``) probes.

Both probes answer GET (JSON body, documented in the OpenAPI schema) and HEAD
(empty body, excluded from the schema) so that external uptime monitors that
probe with HEAD get a real answer instead of falling through to the catch-all
``HEAD /{code}`` redirect route. The liveness probe never touches the database;
the readiness probe runs a trivial ``SELECT 1``.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import ServiceUnavailableError
from app.schemas import ErrorResponse, HealthResponse

router = APIRouter(tags=["health"])

logger = logging.getLogger("app.health")


def _check_database(db: Session) -> None:
    """Run ``SELECT 1``; raise a 503 domain error without leaking details on failure."""
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readiness check failed")
        raise ServiceUnavailableError("Database is not ready") from None


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe")
def healthz() -> HealthResponse:
    """Report that the process is alive; never touches the database."""
    return HealthResponse(status="ok")


@router.head("/healthz", include_in_schema=False)
def healthz_head() -> Response:
    """HEAD liveness probe: 200 with an empty body; never touches the database."""
    return Response(status_code=200)


@router.get(
    "/readyz",
    response_model=HealthResponse,
    summary="Readiness probe",
    responses={503: {"model": ErrorResponse, "description": "Database is not ready"}},
)
def readyz(db: Annotated[Session, Depends(get_db)]) -> HealthResponse:
    """Report readiness after a trivial ``SELECT 1`` succeeds."""
    _check_database(db)
    return HealthResponse(status="ready")


@router.head("/readyz", include_in_schema=False)
def readyz_head(db: Annotated[Session, Depends(get_db)]) -> Response:
    """HEAD readiness probe: 200 with an empty body, or 503 if ``SELECT 1`` fails."""
    _check_database(db)
    return Response(status_code=200)
