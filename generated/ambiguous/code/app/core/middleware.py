"""Request logging middleware that also acts as the last-resort 500 handler."""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

_request_logger = logging.getLogger("app.request")
_error_logger = logging.getLogger("app.errors")


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Log one line per request and turn escaped exceptions into a generic 500.

    Only method, path (without query string), status and duration are logged; the
    client address, forwarding headers and other headers are never recorded.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _error_logger.exception("unhandled exception")
            response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        elapsed_ms = (time.perf_counter() - start) * 1000
        _request_logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed_ms, 2),
            },
        )
        return response
