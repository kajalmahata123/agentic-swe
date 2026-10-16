"""Provider-neutral LLM interface used by the agent loop.

Conversation state stays in each provider's *native* message format (so e.g. Anthropic
thinking blocks are passed back untouched); the agent loop only manipulates it through the
provider's `initial_messages` / `append_exchange` / `append_user` helpers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class LLMError(Exception):
    """Base class. `transient` errors are retried by the engine; others fail the attempt."""

    transient = False


class TransientLLMError(LLMError):
    transient = True


class FatalLLMError(LLMError):
    pass


class RefusalError(LLMError):
    """The model declined. Handled by escalating to the fallback model."""

    transient = True


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    content: str
    is_error: bool = False


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    stop_reason: str
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    native: Any = None  # provider-native assistant content, appended back verbatim


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


class LLMProvider(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
        effort: str,
    ) -> LLMResponse: ...

    def initial_messages(self, prompt: str) -> list[Any]: ...

    def append_exchange(
        self, messages: list[Any], response: LLMResponse, results: list[ToolResult]
    ) -> None: ...

    def append_user(self, messages: list[Any], text: str) -> None: ...
