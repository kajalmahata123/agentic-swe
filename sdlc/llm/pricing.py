"""Per-model token pricing (USD per 1M tokens) used for budget enforcement.

First-party Anthropic list prices; unknown models fall back to the most expensive entry so
budgets fail safe rather than under-count. Cache writes are billed at 1.25x input.
"""

from __future__ import annotations

from sdlc.llm.base import Usage

PRICES: dict[str, tuple[float, float, float]] = {
    # model: (input, output, cache_read)
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
_FALLBACK = (10.0, 50.0, 1.0)


def cost_usd(model: str, usage: Usage) -> float:
    inp, out, cache_read = PRICES.get(model, _FALLBACK)
    return (
        usage.input_tokens * inp
        + usage.output_tokens * out
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * inp * 1.25
    ) / 1_000_000
