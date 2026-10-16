"""OpenAI-compatible Chat Completions provider (OpenAI, Gemini's OpenAI endpoint, Ollama, vLLM...).

Exists so the orchestrator is not locked to one vendor; the Anthropic provider remains the
default and the one the scenarios are tuned for.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from sdlc.llm.base import (
    FatalLLMError,
    LLMResponse,
    ToolCall,
    ToolResult,
    ToolSpec,
    TransientLLMError,
    Usage,
)


class OpenAICompatProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 900,
        price_per_mtok: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.timeout = timeout_s
        self.price = price_per_mtok

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
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions", json=body, headers=self.headers, timeout=self.timeout
            )
        except httpx.HTTPError as exc:
            raise TransientLLMError(f"connection error: {exc}") from exc
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientLLMError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code >= 400:
            raise FatalLLMError(f"HTTP {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": tc["function"].get("arguments")}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args))
        u = data.get("usage") or {}
        usage = Usage(input_tokens=u.get("prompt_tokens", 0), output_tokens=u.get("completion_tokens", 0))
        cost = (usage.input_tokens * self.price[0] + usage.output_tokens * self.price[1]) / 1_000_000
        stop = (
            "tool_use" if calls else ("max_tokens" if choice.get("finish_reason") == "length" else "end_turn")
        )
        return LLMResponse(
            text=msg.get("content") or "",
            tool_calls=calls,
            stop_reason=stop,
            model=data.get("model", model),
            usage=usage,
            cost_usd=cost,
            native=msg,
        )

    def initial_messages(self, prompt: str) -> list[Any]:
        return [{"role": "user", "content": prompt}]

    def append_exchange(self, messages: list[Any], response: LLMResponse, results: list[ToolResult]) -> None:
        messages.append(response.native)
        for r in results:
            messages.append({"role": "tool", "tool_call_id": r.call_id, "content": r.content})

    def append_user(self, messages: list[Any], text: str) -> None:
        messages.append({"role": "user", "content": text})
