"""Anthropic Messages API provider (default).

* Streaming + `get_final_message()` so long code-writing turns never hit HTTP timeouts.
* Adaptive thinking with an explicit effort level.
* Prompt caching: the frozen system prompt (and the tool list rendered before it) carries a
  cache breakpoint, and top-level auto-caching covers the growing message history — agent
  loops re-send the whole conversation every turn, so this is the main cost lever.
* SDK errors are mapped to transient (retry) vs fatal (fail the attempt).
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx
import httpx2

from sdlc.llm.base import (
    FatalLLMError,
    LLMResponse,
    RefusalError,
    ToolCall,
    ToolResult,
    ToolSpec,
    TransientLLMError,
    Usage,
)
from sdlc.llm.pricing import cost_usd


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_s: float = 900,
        workspace_id: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {"max_retries": 3, "timeout": timeout_s}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        if workspace_id:  # org-level keys must name the workspace on every request
            kwargs["default_headers"] = {"anthropic-workspace-id": workspace_id}
        self.client = anthropic.Anthropic(**kwargs)

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[ToolSpec],
        model: str,
        max_tokens: int,
        effort: str,
    ) -> LLMResponse:
        tool_defs = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
        ]
        try:
            with self.client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tool_defs,
                messages=messages,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                cache_control={"type": "ephemeral"},
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as exc:
            raise FatalLLMError(
                f"Anthropic authentication failed — set ANTHROPIC_API_KEY ({exc.message})"
            ) from exc
        except anthropic.PermissionDeniedError as exc:
            raise FatalLLMError(f"Anthropic permission denied: {exc.message}") from exc
        except anthropic.NotFoundError as exc:
            raise FatalLLMError(f"model/endpoint not found ({model}): {exc.message}") from exc
        except anthropic.BadRequestError as exc:
            raise FatalLLMError(f"bad request: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise TransientLLMError(f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise TransientLLMError(f"server error {exc.status_code}: {exc.message}") from exc
            raise FatalLLMError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise TransientLLMError(f"connection error: {exc}") from exc
        except (httpx2.TransportError, httpx.TransportError) as exc:
            # A connection dropped *mid-stream* surfaces as a raw transport error, not an SDK error.
            raise TransientLLMError(f"stream interrupted: {type(exc).__name__}: {exc}") from exc

        if msg.stop_reason == "refusal":
            details = getattr(msg, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise RefusalError(f"model declined (category={category})")

        u = msg.usage
        usage = Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in msg.content if b.type == "tool_use"]
        return LLMResponse(
            text=text,
            tool_calls=calls,
            stop_reason=msg.stop_reason or "",
            model=msg.model,
            usage=usage,
            cost_usd=cost_usd(model, usage),
            native=msg.content,
        )

    # ------------------------------------------------------------ messages
    def initial_messages(self, prompt: str) -> list[Any]:
        return [{"role": "user", "content": prompt}]

    def append_exchange(self, messages: list[Any], response: LLMResponse, results: list[ToolResult]) -> None:
        messages.append({"role": "assistant", "content": response.native})
        if results:
            # All results for one assistant turn go back in a single user message.
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in results
                    ],
                }
            )

    def append_user(self, messages: list[Any], text: str) -> None:
        messages.append({"role": "user", "content": text})
