"""Deterministic scripted provider — used ONLY by the orchestrator's own test-suite.

Lets tests drive the real agent loop, tool guard, gates and engine without network access.
A handler per agent role receives a `FakeTurn` and returns the next `LLMResponse`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sdlc.llm.base import LLMResponse, ToolCall, ToolResult, ToolSpec, Usage

_ids = itertools.count(1)


@dataclass
class FakeTurn:
    role: str
    turn: int
    model: str
    prompt: str
    last_results: list[ToolResult] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)


def call(name: str, **inputs: Any) -> ToolCall:
    return ToolCall(f"call_{next(_ids)}", name, inputs)


def respond(*calls: ToolCall, text: str = "", cost: float = 0.01) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=list(calls),
        stop_reason="tool_use" if calls else "end_turn",
        model="fake",
        usage=Usage(100, 50),
        cost_usd=cost,
    )


Handler = Callable[[FakeTurn], LLMResponse]


class FakeProvider:
    name = "fake"

    def __init__(self, handlers: dict[str, Handler]) -> None:
        self.handlers = handlers
        self.calls: list[FakeTurn] = []

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
        m = re.search(r"ROLE:\s*([\w-]+)", system)
        role = m.group(1) if m else "unknown"
        user_turns = [x for x in messages if x["role"] == "user"]
        last = user_turns[-1]["content"] if user_turns else ""
        results = last if isinstance(last, list) else []
        turn = FakeTurn(
            role=role,
            turn=len([x for x in messages if x["role"] == "assistant"]) + 1,
            model=model,
            prompt=messages[0]["content"],
            last_results=results,
            tools=[t.name for t in tools],
        )
        self.calls.append(turn)
        if role not in self.handlers:
            raise AssertionError(f"FakeProvider has no handler for role {role!r}")
        return self.handlers[role](turn)

    def initial_messages(self, prompt: str) -> list[Any]:
        return [{"role": "user", "content": prompt}]

    def append_exchange(self, messages: list[Any], response: LLMResponse, results: list[ToolResult]) -> None:
        messages.append({"role": "assistant", "content": response})
        if results:
            messages.append({"role": "user", "content": results})

    def append_user(self, messages: list[Any], text: str) -> None:
        messages.append({"role": "user", "content": text})
