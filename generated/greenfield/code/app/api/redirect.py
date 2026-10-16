"""Catch-all ``/{code}`` redirect endpoints.

This router must be included last so it never shadows fixed routes. Responses are
built as raw 307 ``Response`` objects rather than ``RedirectResponse`` because the
latter percent-quotes the URL, and the Location header must equal the stored URL.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import get_link_service
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


@router.get(
    "/{code}",
    status_code=307,
    response_class=Response,
    summary="Redirect to the original URL",
    responses={
        307: {"description": "Redirect to original URL"},
        404: {"model": ErrorResponse, "description": "Link not found"},
        410: {"model": ErrorResponse, "description": "Link has been deactivated"},
    },
)
def redirect(code: str, request: Request, service: LinkServiceDep) -> Response:
    """Redirect an active link with 307 and record a click (best-effort)."""
    url = service.resolve_and_record_click(
        code,
        request.headers.get("referer"),
        request.headers.get("user-agent"),
        request.client.host if request.client else None,
    )
    return _redirect_response(url)


@router.head("/{code}", include_in_schema=False)
def redirect_head(code: str, service: LinkServiceDep) -> Response:
    """Same status and Location as GET, without recording a click."""
    return _redirect_response(service.resolve_for_redirect(code))
