"""Core domain types shared by the engine, governance layer, persistence and dashboard.

Everything here is plain data. Behaviour lives in the engine/governance packages so the
types can be serialised to SQLite and JSON without dragging logic along.
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def content_hash(value: Any) -> str:
    """Stable hash of any JSON-serialisable value (sorted keys => order independent)."""
    blob = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


class Stage(str, enum.Enum):
    """SDLC lifecycle stages; every node belongs to exactly one."""

    REQUIREMENTS = "requirements"
    DESIGN = "design"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    RELEASE = "release"


class NodeKind(str, enum.Enum):
    AGENT = "agent"  # LLM agent does the work
    CHECK = "check"  # deterministic validation
    HUMAN = "human"  # a human decision is the work (clarification)
    PLANNER = "planner"  # expands the graph at runtime
    JOIN = "join"  # synchronisation barrier
    ACTION = "action"  # deterministic side effect (merge, tag, package)


class NodeStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    STALE = "stale"
    CANCELLED = "cancelled"


DONE_OK = {NodeStatus.SUCCEEDED, NodeStatus.SKIPPED}
TERMINAL = DONE_OK | {NodeStatus.FAILED, NodeStatus.CANCELLED}


class RunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STOPPED = "stopped"  # safe-stop: resumable


class Risk(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FailurePolicy(str, enum.Enum):
    ESCALATE = "escalate"  # ask a human: retry / skip / abort
    SKIP = "skip"  # optional node, degrade gracefully
    SAFE_STOP = "safe_stop"  # halt the whole run, keep state resumable


class ApprovalKind(str, enum.Enum):
    CLARIFICATION = "clarification"
    DESIGN = "design"
    CHANGE = "change"  # diff/change-control approval
    POLICY_EXCEPTION = "policy_exception"
    ESCALATION = "escalation"
    RELEASE = "release"


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"  # inputs changed (re-plan) before a decision was made


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    backoff_s: float = 1.0
    # From this attempt on, the agent is escalated to the fallback (stronger) model.
    escalate_model_at: int = 3


@dataclass
class NodeSpec:
    """Static description of one unit of work in the dependency graph."""

    id: str
    stage: Stage
    kind: NodeKind
    handler: str
    title: str = ""
    deps: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)  # artifact names consumed
    outputs: list[str] = field(default_factory=list)  # artifact names produced
    write_scope: list[str] = field(default_factory=list)  # glob patterns; empty => read-only
    risk: Risk = Risk.LOW
    approval: ApprovalKind | None = None  # human sign-off required on this node's output
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    on_failure: FailurePolicy = FailurePolicy.ESCALATE
    timeout_s: float = 1800.0
    params: dict[str, Any] = field(default_factory=dict)
    created_by: str | None = None  # planner node id when generated dynamically

    @property
    def mutates_workspace(self) -> bool:
        return bool(self.write_scope)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stage"] = self.stage.value
        d["kind"] = self.kind.value
        d["risk"] = self.risk.value
        d["approval"] = self.approval.value if self.approval else None
        d["on_failure"] = self.on_failure.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NodeSpec:
        d = dict(d)
        d["stage"] = Stage(d["stage"])
        d["kind"] = NodeKind(d["kind"])
        d["risk"] = Risk(d.get("risk", "low"))
        d["approval"] = ApprovalKind(d["approval"]) if d.get("approval") else None
        d["on_failure"] = FailurePolicy(d.get("on_failure", "escalate"))
        d["retry"] = RetryPolicy(**d.get("retry", {}))
        return cls(**d)


@dataclass
class NodeState:
    """Mutable runtime state of a node within a run."""

    node_id: str
    status: NodeStatus = NodeStatus.PENDING
    attempts: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)
    output_hashes: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    cost_usd: float = 0.0
    base_sha: str | None = None  # main HEAD before this node merged
    merge_sha: str | None = None  # main HEAD after this node merged
    summary: str | None = None
    # Set when a read-only node is invalidated: if its inputs come back byte-identical
    # the previous result is reused instead of re-running the agent (memoised re-plan).
    reusable: bool = False


@dataclass
class Artifact:
    """Versioned, content-addressed output of a node. Lineage via `derived_from`."""

    name: str
    value: Any
    producer: str
    version: int = 1
    hash: str = ""
    derived_from: dict[str, str] = field(default_factory=dict)  # input name -> hash
    created_at: float = field(default_factory=now)

    def __post_init__(self) -> None:
        if not self.hash:
            self.hash = content_hash(self.value)


@dataclass
class Decision:
    """A recorded decision (ADR-lite) with who made it and what it was based on."""

    node_id: str
    actor: str  # "agent:architect", "human:alice", "policy:engine"
    title: str
    rationale: str
    alternatives: list[str] = field(default_factory=list)
    based_on: dict[str, str] = field(default_factory=dict)  # artifact name -> hash
    ts: float = field(default_factory=now)


@dataclass
class Approval:
    id: str
    run_id: str
    node_id: str
    kind: ApprovalKind
    summary: str
    payload: dict[str, Any]
    subject_hash: str  # approval is only valid for exactly this content
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: float = field(default_factory=now)
    decided_at: float | None = None
    decided_by: str | None = None
    comment: str | None = None
    # For clarifications/escalations the human returns structured data (answers, choice).
    response: dict[str, Any] = field(default_factory=dict)
