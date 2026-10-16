"""Link management endpoints under ``/api/links``.

Every endpoint on this router requires the ``X-API-Key`` header to match the
configured ``API_KEY`` (router-level dependency, constant-time comparison). Missing
or wrong keys, or an unconfigured ``API_KEY``, yield ``401 {"detail": ...}``. The
public redirect ``GET/HEAD /{code}`` lives in another router and stays open.

Endpoints: create (with optional ``expires_at``), detail (with ``expires_at`` and
``is_expired``), ``PATCH`` to set or clear the expiry, deactivate, daily stats and
zero-filled hourly stats over a half-open UTC range.
"""

from __future__ import annotations

from datetime import datetime
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
    HourlyClicks,
    LinkCreate,
    LinkCreatedResponse,
    LinkDetailResponse,
    LinkExpiryUpdate,
    LinkHourlyStatsResponse,
    LinkStatsResponse,
    ReferrerCount,
)
from app.services.link_service import LinkDetail, LinkService
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


def _detail_response(detail: LinkDetail, settings: Settings) -> LinkDetailResponse:
    """Map a service-level :class:`LinkDetail` to the public detail response."""
    link = detail.link
    return LinkDetailResponse(
        code=link.code,
        short_url=build_short_url(settings.base_url, link.code),
        url=link.url,
        is_active=link.is_active,
        created_at=link.created_at,
        deactivated_at=link.deactivated_at,
        expires_at=link.expires_at,
        is_expired=detail.is_expired,
        total_clicks=detail.total_clicks,
    )


@router.post(
    "",
    status_code=201,
    response_model=LinkCreatedResponse,
    summary="Create a short link",
    responses={
        400: {"model": ErrorResponse, "description": "Invalid Idempotency-Key header"},
        409: {
            "model": ErrorResponse,
            "description": "Idempotency-Key already used with a different payload",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "Invalid request body, URL or expires_at "
                "(must be a future ISO 8601 datetime)"
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
    """Create a new active short link, optionally with an expiry instant.

    With an ``Idempotency-Key`` header, a repeat request with the same key and payload
    (URL and, if given, the same ``expires_at`` instant) returns the original link and
    the header ``Idempotent-Replayed: true``.
    """
    result = service.create_link(payload.url, idempotency_key, payload.expires_at)
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
    )


@router.get(
    "/{code}",
    response_model=LinkDetailResponse,
    summary="Get link metadata",
    responses=_NOT_FOUND,
)
def get_link(code: str, service: LinkServiceDep, settings: SettingsDep) -> LinkDetailResponse:
    """Return metadata for a link, including deactivated and expired links."""
    return _detail_response(service.get_link_detail(code), settings)


@router.patch(
    "/{code}",
    response_model=LinkDetailResponse,
    summary="Set or clear a link's expiry",
    responses={
        **_NOT_FOUND,
        422: {
            "model": ErrorResponse,
            "description": (
                "Invalid expires_at (missing, not a datetime, or not in the future) "
                "or unknown fields"
            ),
        },
    },
)
def update_link_expiry(
    code: str,
    payload: LinkExpiryUpdate,
    service: LinkServiceDep,
    settings: SettingsDep,
) -> LinkDetailResponse:
    """Set (future instant) or clear (``null``) the expiry of an existing link.

    Only ``expires_at`` changes; a deactivated link is never reactivated.
    """
    return _detail_response(service.set_expiry(code, payload.expires_at), settings)


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


@router.get(
    "/{code}/stats/hourly",
    response_model=LinkHourlyStatsResponse,
    summary="Get clicks per UTC hour for a link",
    responses={
        **_NOT_FOUND,
        422: {
            "model": ErrorResponse,
            "description": (
                "Invalid start/end (both or neither, start < end, at most 744 hours, "
                "ISO 8601)"
            ),
        },
    },
)
def get_hourly_stats(
    code: str,
    service: StatsServiceDep,
    start: Annotated[
        datetime | None,
        Query(description="Inclusive range start (ISO 8601; floored to the UTC hour)"),
    ] = None,
    end: Annotated[
        datetime | None,
        Query(description="Exclusive range end (ISO 8601; rounded up to the next UTC hour)"),
    ] = None,
) -> LinkHourlyStatsResponse:
    """Return zero-filled clicks per UTC hour over ``[start, end)``.

    Supply both ``start`` and ``end`` or neither; without them the range is the last
    24 complete hours plus the current hour. Works for deactivated and expired links.
    """
    stats = service.get_hourly_clicks(code, start=start, end=end)
    return LinkHourlyStatsResponse(
        code=stats.code,
        start=stats.start,
        end=stats.end,
        total_clicks=stats.total_clicks,
        clicks_per_hour=[HourlyClicks(hour=h, count=c) for h, c in stats.clicks_per_hour],
    )
