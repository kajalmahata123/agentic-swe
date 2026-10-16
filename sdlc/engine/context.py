"""Run-scoped context shared by the executor and every node handler.

Cross-stage context = the artifact store (versioned, hashed, with lineage) + the decision
log. Handlers never talk to each other directly; they read input artifacts and write output
artifacts, which is what makes dependency tracking and re-planning possible.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sdlc.domain.graph import DependencyGraph
from sdlc.domain.models import Artifact, Decision, NodeSpec, NodeState
from sdlc.governance.approvals import ApprovalService
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.llm.base import LLMProvider
from sdlc.persistence.store import Store
from sdlc.workspace.git_workspace import GitWorkspace, TaskWorkspace


@dataclass
class RunConfig:
    model: str = "claude-opus-5-5"
    fallback_model: str = "claude-opus-5-5"
    effort: str = "high"
    max_parallel: int = 3
    approve_mode: str = "dashboard"
    operator: str = "operator"
    seed_dir: str | None = None
    version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    store: Store
    audit: AuditLog
    policy: Policy
    approvals: ApprovalService
    provider: LLMProvider
    workspace: GitWorkspace
    graph: DependencyGraph
    states: dict[str, NodeState]
    config: RunConfig
    scenario: str
    cost_lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------------------------------------------------------------- artifacts
    def artifact(self, name: str) -> Artifact | None:
        return self.store.latest_artifact(self.run_id, name)

    def value(self, name: str, default: Any = None) -> Any:
        art = self.artifact(name)
        return art.value if art else default

    def put(
        self, name: str, value: Any, producer: str, derived_from: dict[str, str] | None = None
    ) -> Artifact:
        art = self.store.put_artifact(
            self.run_id, Artifact(name=name, value=value, producer=producer, derived_from=derived_from or {})
        )
        self.audit.record(
            self.run_id,
            "artifact.stored",
            producer,
            name=name,
            version=art.version,
            hash=art.hash,
            derived_from=art.derived_from,
        )
        return art

    def decide(self, d: Decision) -> None:
        self.store.add_decision(self.run_id, d)
        self.audit.record(
            self.run_id,
            "decision.recorded",
            d.node_id,
            actor=d.actor,
            title=d.title,
            rationale=d.rationale,
            alternatives=d.alternatives,
            based_on=d.based_on,
        )

    def stop_reason(self) -> str | None:
        return self.store.stop_requested(self.run_id)

    def total_cost(self) -> float:
        return sum(s.cost_usd for s in self.states.values())

    def persist_graph(self) -> None:
        self.store.update_run(self.run_id, graph=self.graph.to_dict())


@dataclass
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    severity: str = "block"  # block | warn

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class NodeResult:
    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    decisions: list[Decision] = field(default_factory=list)
    gates: list[GateResult] = field(default_factory=list)
    cost_usd: float = 0.0
    approval_summary: str = ""  # shown to the human when the node needs sign-off
    approval_payload: dict[str, Any] = field(default_factory=dict)
    new_nodes: list[NodeSpec] = field(default_factory=list)  # planner output
    extra_approvals: list[tuple[str, str, dict[str, Any]]] = field(
        default_factory=list
    )  # (kind, summary, payload)
    metrics: dict[str, Any] = field(default_factory=dict)
    # Nodes to invalidate after this node succeeds (e.g. clarification answers change the brief).
    invalidate: list[str] = field(default_factory=list)

    @property
    def failed_gates(self) -> list[GateResult]:
        return [g for g in self.gates if not g.passed and g.severity == "block"]


@dataclass
class NodeContext:
    run: RunContext
    spec: NodeSpec
    state: NodeState
    attempt: int
    model: str
    inputs: dict[str, Artifact]
    feedback: list[str]
    task_ws: TaskWorkspace | None
    cancel: threading.Event
    charge: Callable[[float], None]  # report incremental LLM cost; raises when budget is blown
    escalated: bool = False  # later attempts: fallback model + maximum effort

    @property
    def workdir(self) -> Path:
        return self.task_ws.path if self.task_ws else self.run.workspace.root

    def input(self, name: str, default: Any = None) -> Any:
        art = self.inputs.get(name)
        return art.value if art else default

    def emit(self, type_: str, **data: Any) -> None:
        self.run.audit.record(self.run.run_id, type_, self.spec.id, self.attempt, **data)


class Handler:
    """Base class for node handlers. Subclasses implement `run` (async)."""

    name: str = ""

    async def run(self, ctx: NodeContext) -> NodeResult:  # pragma: no cover - interface
        raise NotImplementedError


HANDLERS: dict[str, Handler] = {}


def register(handler: Handler) -> Handler:
    HANDLERS[handler.name] = handler
    return handler
