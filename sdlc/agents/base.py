"""Shared machinery for LLM-backed node handlers."""

from __future__ import annotations

import asyncio
import json
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sdlc.agents.loop import AgentConfig, AgentOutcome, run_agent
from sdlc.agents.tools import Toolbox
from sdlc.engine.context import Handler, NodeContext, NodeResult

PROMPTS = Path(__file__).parent / "prompts"


@cache
def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


def as_json(value: Any, limit: int = 60_000) -> str:
    s = json.dumps(value, indent=1, default=str)
    return s if len(s) <= limit else s[:limit] + "\n... [truncated]"


class AgentHandler(Handler):
    role: str = ""
    output_model: type[BaseModel]
    allow_commands: bool = True
    effort: str | None = None

    def build_prompt(self, ctx: NodeContext) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        raise NotImplementedError  # pragma: no cover - interface

    async def run(self, ctx: NodeContext) -> NodeResult:
        system = load_prompt("common") + "\n\n" + load_prompt(self.role)
        toolbox = Toolbox(
            root=ctx.workdir,
            policy=ctx.run.policy,
            write_scope=ctx.spec.write_scope,
            audit=lambda t, **d: ctx.emit(t, **d),
            output_model=self.output_model,
            allow_commands=self.allow_commands,
        )
        prompt = self.build_prompt(ctx)
        if ctx.feedback:
            prompt += "\n\n## Feedback from previous attempts (address every point)\n" + "\n".join(
                f"- {f}" for f in ctx.feedback[-6:]
            )
        config = AgentConfig(
            role=self.role,
            system=system,
            model=ctx.model,
            effort="xhigh" if ctx.escalated else (self.effort or ctx.run.config.effort),
            max_turns=int(ctx.run.policy.budgets.get("max_agent_turns", 60)),
        )
        transcript = ctx.run.run_dir / "transcripts" / f"{ctx.spec.id.replace(':', '_')}-a{ctx.attempt}.jsonl"
        outcome = await asyncio.to_thread(
            run_agent,
            ctx.run.provider,
            config,
            prompt,
            toolbox,
            transcript,
            lambda t, **d: ctx.emit(t, **d),
            ctx.charge,
            ctx.cancel,
        )
        result = self.evaluate(ctx, outcome.result, outcome)
        result.cost_usd = outcome.cost_usd
        result.metrics.update(
            {
                "agent_turns": outcome.turns,
                "tool_calls": outcome.tool_calls,
                "denied_calls": outcome.denied_calls,
                "model": outcome.model,
                "cost_usd": round(outcome.cost_usd, 4),
                "transcript": outcome.transcript,
                "files_written": outcome.files_written,
            }
        )
        return result
