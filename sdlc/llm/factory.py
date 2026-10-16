"""Provider selection from environment (12-factor). No keys are ever written to state or logs."""

from __future__ import annotations

import os
from pathlib import Path

from sdlc.llm.base import LLMProvider


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader (KEY=VALUE lines); existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_provider(name: str | None = None) -> LLMProvider:
    name = (name or os.environ.get("SDLC_PROVIDER", "anthropic")).lower()
    if name == "anthropic":
        from sdlc.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
            base_url=os.environ.get("ANTHROPIC_BASE_URL") or None,
            workspace_id=os.environ.get("ANTHROPIC_WORKSPACE_ID") or None,
        )
    if name in {"openai", "openai-compatible"}:
        from sdlc.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            api_key=os.environ.get("OPENAI_API_KEY"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )
    raise ValueError(f"unknown provider {name!r} (expected anthropic | openai)")


def default_models() -> tuple[str, str]:
    primary = os.environ.get("SDLC_MODEL", "claude-opus-5-5")
    return primary, os.environ.get("SDLC_FALLBACK_MODEL", primary)
