"""The agent loop: model <-> governed tools until the agent submits a schema-valid result.

Bounded on every axis: turns, cost (checked against the run budget every turn), wall-clock
(enforced by the engine), and cooperative cancellation (stop / re-plan). The full
transcript is written to disk for audit; the audit log gets a per-turn summary.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdlc.agents.tools import SubmitAccepted, Toolbox
from sdlc.llm.base import LLMProvider, ToolResult, Usage


class AgentBudgetExceeded(Exception):
    pass


class AgentCancelled(Exception):
    pass


class AgentNoResult(Exception):
    pass


@dataclass
class AgentOutcome:
    result: dict[str, Any]
    turns: int
    usage: Usage
    cost_usd: float
    tool_calls: int
    denied_calls: int
    files_written: list[str]
    commands: list[str]
    model: str
    transcript: str
    final_text: str = ""


@dataclass
class AgentConfig:
    role: str
    system: str
    model: str
    effort: str = "high"
    max_turns: int = 60
    max_tokens: int = 32000
    extra: dict[str, Any] = field(default_factory=dict)


def run_agent(
    provider: LLMProvider,
    config: AgentConfig,
    prompt: str,
    toolbox: Toolbox,
    transcript_path: Path,
    audit: Callable[..., Any],
    budget_check: Callable[[float], None],
    cancel: threading.Event,
) -> AgentOutcome:
    messages = provider.initial_messages(prompt)
    tools = toolbox.specs()
    usage = Usage()
    cost = 0.0
    nudges = 0
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("w") as tx:
        tx.write(
            json.dumps({"type": "start", "role": config.role, "model": config.model, "prompt": prompt}) + "\n"
        )
        for turn in range(1, config.max_turns + 1):
            if cancel.is_set():
                raise AgentCancelled("cancelled by orchestrator (stop or re-plan)")
            t0 = time.time()
            resp = provider.complete(
                system=config.system,
                messages=messages,
                tools=tools,
                model=config.model,
                max_tokens=config.max_tokens,
                effort=config.effort,
            )
            usage.add(resp.usage)
            cost += resp.cost_usd
            budget_check(resp.cost_usd)
            results: list[ToolResult] = []
            submitted: dict[str, Any] | None = None
            for call in resp.tool_calls:
                if submitted is not None:
                    results.append(ToolResult(call.id, "ignored: result already submitted", True))
                    continue
                try:
                    results.append(toolbox.execute(call))
                except SubmitAccepted as ok:
                    submitted = ok.value
                    results.append(ToolResult(call.id, "accepted"))
            tx.write(
                json.dumps(
                    {
                        "type": "turn",
                        "turn": turn,
                        "latency_s": round(time.time() - t0, 2),
                        "stop": resp.stop_reason,
                        "text": resp.text[:4000],
                        "calls": [{"name": c.name, "input": _summarise(c.input)} for c in resp.tool_calls],
                        "results": [{"error": r.is_error, "content": r.content[:1500]} for r in results],
                        "cost_usd": round(resp.cost_usd, 5),
                    }
                )
                + "\n"
            )
            tx.flush()
            audit(
                "agent.turn",
                role=config.role,
                turn=turn,
                model=resp.model,
                stop=resp.stop_reason,
                tools=[c.name for c in resp.tool_calls],
                errors=sum(r.is_error for r in results),
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
                cache_read=resp.usage.cache_read_tokens,
                cache_write=resp.usage.cache_write_tokens,
                cost_usd=round(resp.cost_usd, 5),
            )
            if submitted is not None:
                return AgentOutcome(
                    result=submitted,
                    turns=turn,
                    usage=usage,
                    cost_usd=cost,
                    tool_calls=toolbox.stats.calls,
                    denied_calls=toolbox.stats.denied,
                    files_written=sorted(toolbox.stats.files_written),
                    commands=toolbox.stats.commands,
                    model=resp.model,
                    transcript=str(transcript_path),
                    final_text=resp.text,
                )
            provider.append_exchange(messages, resp, results)
            if not resp.tool_calls:
                nudges += 1
                if nudges > 2:
                    raise AgentNoResult("agent stopped without calling submit() three times")
                provider.append_user(
                    messages,
                    "You have not finished: call the `submit` tool with your final "
                    "structured result (or keep working with the tools if work remains).",
                )
    raise AgentBudgetExceeded(f"agent exceeded max_turns={config.max_turns} without submitting")


def _summarise(inp: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k, v in inp.items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = f"<{len(v)} chars, {v.count(chr(10)) + 1} lines>"
        elif isinstance(v, dict | list):
            s = json.dumps(v, default=str)
            out[k] = s if len(s) <= 600 else s[:600] + "..."
        else:
            out[k] = v
    return out
