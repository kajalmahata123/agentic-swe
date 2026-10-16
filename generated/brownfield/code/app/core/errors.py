"""Domain exceptions and the JSON exception handlers.

The exception classes are framework-agnostic so services can raise them without
importing FastAPI. ``register_exception_handlers`` maps them, plus framework and
database errors, to ``{"detail": ...}`` JSON bodies that never contain exception text.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("app.errors")

INTERNAL_SERVER_ERROR_DETAIL = "Internal Server Error"
SERVICE_UNAVAILABLE_DETAIL = "Service temporarily unavailable"
LINK_EXPIRED_DETAIL = "Link has expired"


class AppError(Exception):
    """Base class for domain errors carrying an HTTP status code and a safe detail."""

    status_code: int = 500
    default_detail: str = INTERNAL_SERVER_ERROR_DETAIL

    def __init__(self, detail: str | None = None) -> None:
        self.detail: str = detail if detail is not None else self.default_detail
        super().__init__(self.detail)


class BadRequestError(AppError):
    status_code = 400
    default_detail = "Bad request"


class NotFoundError(AppError):
    status_code = 404
    default_detail = "Link not found"


class ConflictError(AppError):
    status_code = 409
    default_detail = "Conflict"


class GoneError(AppError):
    status_code = 410
    default_detail = "Link has been deactivated"


class LinkExpiredError(GoneError):
    """The link exists but its ``expires_at`` has passed (410, distinct detail)."""

    default_detail = LINK_EXPIRED_DETAIL


class UnprocessableEntityError(AppError):
    """A business-rule validation failure detected by a service (422)."""

    status_code = 422
    default_detail = "Unprocessable entity"


class ServiceUnavailableError(AppError):
    status_code = 503
    default_detail = SERVICE_UNAVAILABLE_DETAIL


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


async def _http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    return JSONResponse(
        {"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse({"detail": jsonable_encoder(exc.errors())}, status_code=422)


async def _sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("database error", exc_info=exc)
    return JSONResponse({"detail": SERVICE_UNAVAILABLE_DETAIL}, status_code=503)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception", exc_info=exc)
    return JSONResponse({"detail": INTERNAL_SERVER_ERROR_DETAIL}, status_code=500)


def register_exception_handlers(app: FastAPI) -> None:
    """Install the JSON error handlers on ``app``."""
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(SQLAlchemyError, _sqlalchemy_error_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
