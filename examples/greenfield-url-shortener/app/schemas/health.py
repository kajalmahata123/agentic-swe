"""Pydantic models for the health and readiness probes."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Probe result, e.g. ``{"status": "ok"}`` or ``{"status": "ready"}``."""

    status: str
