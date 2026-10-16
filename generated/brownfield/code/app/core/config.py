"""Application settings loaded from environment variables.

Environment variable names are the upper-cased field names, e.g. ``DATABASE_URL``,
``BASE_URL``, ``SHORT_CODE_LENGTH``, ``CODE_MAX_ATTEMPTS``, ``IP_HASH_SALT``,
``LOG_LEVEL`` and ``API_KEY``. No module-level instance is created: ``create_app`` builds a fresh
``Settings`` per application so tests stay isolated.
"""

from __future__ import annotations

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
MIN_API_KEY_LENGTH = 16


class Settings(BaseSettings):
    """Runtime configuration for the URL shortener."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    database_url: str = "sqlite:///./shortener.db"
    base_url: str = "http://localhost:8000"
    short_code_length: int = Field(7, ge=4, le=32)
    code_max_attempts: int = Field(5, ge=1, le=20)
    ip_hash_salt: str | None = None
    log_level: str = "INFO"
    # Shared secret required in the X-API-Key header for every /api/links* request.
    # When unset, the management API rejects all requests (fail closed).
    api_key: SecretStr | None = None

    @field_validator("ip_hash_salt", mode="before")
    @classmethod
    def _blank_salt_is_none(cls, value: object) -> object:
        """Treat an empty or whitespace-only salt as "not configured"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_key", mode="before")
    @classmethod
    def _blank_api_key_is_none(cls, value: object) -> object:
        """Treat an empty or whitespace-only API key as "not configured"."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("api_key", mode="after")
    @classmethod
    def _api_key_min_length(cls, value: SecretStr | None) -> SecretStr | None:
        """Reject trivially guessable API keys."""
        if value is not None and len(value.get_secret_value()) < MIN_API_KEY_LENGTH:
            raise ValueError(f"api_key must be at least {MIN_API_KEY_LENGTH} characters")
        return value

    @field_validator("log_level", mode="after")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """Upper-case the level and reject names the logging module does not know."""
        level = value.strip().upper()
        if level not in _ALLOWED_LOG_LEVELS:
            allowed = ", ".join(sorted(_ALLOWED_LOG_LEVELS))
            raise ValueError(f"log_level must be one of: {allowed}")
        return level
