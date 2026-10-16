"""Catch-all ``/{code}`` redirect endpoints.

This router must be included last so it never shadows fixed routes. Responses are
built as raw 307 ``Response`` objects rather than ``RedirectResponse`` because the
latter percent-quotes the URL, and the Location header must equal the stored URL.

404 (unknown or malformed code) and 410 (deactivated, or expired: "Link has
expired") responses carry ``Cache-Control: no-store`` so browsers and CDNs never
cache them; expiry changes and new links take effect immediately. Database failures
during the lookup propagate to the global handler and become a generic 503.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.deps import get_link_service
from app.core.errors import AppError, GoneError, NotFoundError
from app.schemas import ErrorResponse
from app.services.link_service import LinkService

router = APIRouter(tags=["redirect"])

LinkServiceDep = Annotated[LinkService, Depends(get_link_service)]


def _redirect_response(url: str) -> Response:
    """Build an uncacheable 307 response pointing at ``url`` exactly."""
    return Response(
        status_code=307,
        headers={"Location": url, "Cache-Control": "no-store"},
    )


def _error_response(exc: AppError) -> JSONResponse:
    """Build an uncacheable ``{"detail": ...}`` error response for ``exc``."""
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/{code}",
    status_code=307,
    response_class=Response,
    summary="Redirect to the original URL",
    responses={
        307: {"description": "Redirect to original URL"},
        404: {"model": ErrorResponse, "description": "Link not found"},
        410: {
            "model": ErrorResponse,
            "description": "Link has been deactivated, or has expired ('Link has expired')",
        },
        503: {"model": ErrorResponse, "description": "Database unavailable"},
    },
)
def redirect(code: str, request: Request, service: LinkServiceDep) -> Response:
    """Redirect an active, unexpired link with 307 and record a click (best-effort)."""
    try:
        url = service.resolve_and_record_click(
            code,
            request.headers.get("referer"),
            request.headers.get("user-agent"),
            request.client.host if request.client else None,
        )
    except (NotFoundError, GoneError) as exc:
        return _error_response(exc)
    return _redirect_response(url)


@router.head("/{code}", include_in_schema=False)
def redirect_head(code: str, service: LinkServiceDep) -> Response:
    """Same status and Location as GET, without recording a click."""
    try:
        url = service.resolve_for_redirect(code)
    except (NotFoundError, GoneError) as exc:
        return _error_response(exc)
    return _redirect_response(url)
