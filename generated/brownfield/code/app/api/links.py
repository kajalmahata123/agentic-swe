"""Link management endpoints under ``/api/links``.

Every endpoint on this router requires the ``X-API-Key`` header to match the
configured ``API_KEY`` (router-level dependency, constant-time comparison). Missing
or wrong keys, or an unconfigured ``API_KEY``, yield ``401 {"detail": ...}``. The
public redirect ``GET/HEAD /{code}`` lives in another router and stays open.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.deps import (
    UNAUTHORIZED_DETAIL,
    build_short_url,
    get_link_service,
    get_settings,
    get_stats_service,
    require_api_key,
)
from app.core.config import Settings
from app.schemas import (
    DailyClicks,
    ErrorResponse,
    LinkCreate,
    LinkCreatedResponse,
    LinkDetailResponse,
    LinkStatsResponse,
    ReferrerCount,
)
from app.services.link_service import LinkService
from app.services.stats_service import StatsService

router = APIRouter(
    prefix="/api/links",
    tags=["links"],
    dependencies=[Depends(require_api_key)],
    responses={401: {"model": ErrorResponse, "description": UNAUTHORIZED_DETAIL}},
)

LinkServiceDep = Annotated[LinkService, Depends(get_link_service)]
StatsServiceDep = Annotated[StatsService, Depends(get_stats_service)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Link not found"},
}


@router.post(
    "",
    status_code=201,
    response_model=LinkCreatedResponse,
    summary="Create a short link",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid Idempotency-Key header"},
        409: {
            "model": ErrorResponse,
            "description": (
                "Alias already in use, or Idempotency-Key already used with a "
                "different payload"
            ),
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "Invalid request body, URL, alias (format, length or reserved word) "
                "or expires_at (not ISO-8601 or not in the future)"
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": "Short code allocation exhausted or database unavailable",
        },
    },
)
def create_link(
    payload: LinkCreate,
    response: Response,
    service: LinkServiceDep,
    settings: SettingsDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> LinkCreatedResponse:
    """Create a new active short link.

    An optional ``alias`` (3-32 characters of ``A-Z a-z 0-9 _ -``, case preserved)
    becomes the short code; reserved words give 422 and a taken alias gives 409.
    Without it a random code is generated. An optional ``expires_at`` (ISO-8601,
    naive values are UTC) must lie in the future; after it passes the redirect
    returns 410.

    With an ``Idempotency-Key`` header, a repeat request with the same key and the
    same payload (url, alias, expires_at) returns the original link and the header
    ``Idempotent-Replayed: true``.
    """
    result = service.create_link(
        payload.url,
        idempotency_key,
        alias=payload.alias,
        expires_at=payload.expires_at,
    )
    link = result.link
    response.headers["Location"] = f"/api/links/{link.code}"
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return LinkCreatedResponse(
        code=link.code,
        short_url=build_short_url(settings.base_url, link.code),
        url=link.url,
        is_active=link.is_active,
        created_at=link.created_at,
        expires_at=link.expires_at,
        is_expired=result.is_expired,
    )


@router.get(
    "/{code}",
    response_model=LinkDetailResponse,
    summary="Get link metadata",
    responses=_NOT_FOUND,
)
def get_link(code: str, service: LinkServiceDep, settings: SettingsDep) -> LinkDetailResponse:
    """Return metadata for a link, including deactivated and expired links."""
    detail = service.get_link_detail(code)
    link = detail.link
    return LinkDetailResponse(
        code=link.code,
        short_url=build_short_url(settings.base_url, link.code),
        url=link.url,
        is_active=link.is_active,
        created_at=link.created_at,
        expires_at=link.expires_at,
        is_expired=detail.is_expired,
        deactivated_at=link.deactivated_at,
        total_clicks=detail.total_clicks,
    )


@router.delete(
    "/{code}",
    status_code=204,
    response_class=Response,
    summary="Deactivate (soft-delete) a link",
    responses=_NOT_FOUND,
)
def deactivate_link(code: str, service: LinkServiceDep) -> Response:
    """Mark a link inactive. Repeat calls are no-ops that also return 204."""
    service.deactivate(code)
    return Response(status_code=204)


@router.get(
    "/{code}/stats",
    response_model=LinkStatsResponse,
    summary="Get click statistics for a link",
    responses={
        **_NOT_FOUND,
        422: {"model": ErrorResponse, "description": "Invalid 'days' query parameter"},
    },
)
def get_stats(
    code: str,
    service: StatsServiceDep,
    days: Annotated[
        int, Query(ge=1, le=365, description="Window for clicks_per_day, in UTC days")
    ] = 30,
) -> LinkStatsResponse:
    """Return all-time totals and top referrers plus daily clicks for the window."""
    stats = service.get_stats(code, days=days)
    return LinkStatsResponse(
        code=stats.code,
        total_clicks=stats.total_clicks,
        clicks_per_day=[DailyClicks(date=d, count=c) for d, c in stats.clicks_per_day],
        top_referrers=[ReferrerCount(referrer=r, count=c) for r, c in stats.top_referrers],
    )
