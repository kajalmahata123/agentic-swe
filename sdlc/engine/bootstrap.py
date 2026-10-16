"""Create, load and execute runs. Shared by the CLI and the dashboard."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import yaml

import sdlc.agents.handlers  # noqa: F401  (registers agent handlers)
import sdlc.pipelines.steps  # noqa: F401  (registers deterministic handlers)
from sdlc.domain.graph import DependencyGraph
from sdlc.domain.models import NodeState, RunStatus
from sdlc.engine.context import RunConfig, RunContext
from sdlc.engine.executor import Orchestrator
from sdlc.governance.approvals import ApprovalService
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.llm.base import LLMProvider
from sdlc.llm.factory import build_provider
from sdlc.persistence.store import Store
from sdlc.pipelines.templates import TEMPLATES
from sdlc.workspace.git_workspace import GitWorkspace


def home() -> Path:
    """State directory; read from the environment on every call so tests can isolate it."""
    return Path(os.environ.get("SDLC_HOME", ".sdlc")).resolve()


def runs_dir() -> Path:
    return home() / "runs"


def open_store() -> Store:
    return Store(home() / "state.db")


def workspace_path(meta: dict[str, Any]) -> Path:
    """A run's repo. Falls back to <home>/runs/<id>/repo so restored snapshots work on any machine."""
    recorded = Path(meta.get("workspace") or "")
    if meta.get("workspace") and recorded.exists():
        return recorded
    return runs_dir() / meta["id"] / "repo"


def load_scenario(path: str | Path) -> dict[str, Any]:
    with open(path) as fh:
        data = yaml.safe_load(fh)
    for key in ("name", "mode", "requirement"):
        if key not in data:
            raise ValueError(f"scenario {path} is missing {key!r}")
    if data["mode"] not in TEMPLATES:
        raise ValueError(f"unknown mode {data['mode']!r}; expected one of {sorted(TEMPLATES)}")
    return data


def resolve_seed(store: Store, seed: str | None) -> Path | None:
    """Brownfield seed: a path, `run:<id>` (that run's repo) or `latest:<scenario>`."""
    if not seed:
        return None
    if seed.startswith("run:"):
        path = runs_dir() / seed[4:] / "repo"
    elif seed.startswith("latest:"):
        wanted = seed.split(":", 1)[1]
        match = next(
            (r for r in store.list_runs(500) if r["scenario"] == wanted and r["status"] == "succeeded"), None
        )
        if not match:
            raise ValueError(
                f"no succeeded run of scenario {wanted!r} to use as brownfield seed — run it first"
            )
        path = runs_dir() / match["id"] / "repo"
    else:
        path = Path(seed).expanduser()
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"seed codebase {path} does not exist")
    return path


def create_run(store: Store, scenario: dict[str, Any], config: RunConfig) -> str:
    seed = resolve_seed(store, scenario.get("seed"))
    config.seed_dir = str(seed) if seed else None
    config.version = str(scenario.get("version", config.version))
    run_id = store.create_run(scenario["name"], scenario["requirement"].strip(), config.to_dict())
    run_dir = runs_dir() / run_id
    workspace = GitWorkspace(run_dir / "repo")
    base = workspace.init(seed)
    graph = DependencyGraph(TEMPLATES[scenario["mode"]]())
    store.update_run(run_id, graph=graph.to_dict(), workspace=str(workspace.root))
    for spec in graph.nodes:
        store.save_node_state(run_id, NodeState(node_id=spec.id))
    AuditLog(store).record(
        run_id,
        "run.created",
        None,
        scenario=scenario["name"],
        mode=scenario["mode"],
        seed=str(seed) if seed else None,
        base_commit=base,
        nodes=[n.id for n in graph.nodes],
    )
    for amendment in scenario.get("amendments", []) or []:
        if amendment.get("at_start"):
            store.add_amendment(run_id, amendment["text"], amendment.get("author", "scenario"))
    return run_id


def load_context(
    store: Store,
    run_id: str,
    provider: LLMProvider | None = None,
    approve_mode: str | None = None,
    policy_path: str | None = None,
    audit: AuditLog | None = None,
) -> RunContext:
    meta = store.get_run(run_id)
    if meta is None:
        raise ValueError(f"unknown run {run_id}")
    config = RunConfig(**{k: v for k, v in meta["config"].items() if k in RunConfig.__dataclass_fields__})
    if approve_mode:
        config.approve_mode = approve_mode
        store.update_run(run_id, config=config.to_dict())
    policy = Policy.load(policy_path)
    audit = audit or AuditLog(store)
    states = store.load_node_states(run_id)
    graph = DependencyGraph.from_dict(meta["graph"])
    for spec in graph.nodes:
        states.setdefault(spec.id, NodeState(node_id=spec.id))
    return RunContext(
        run_id=run_id,
        run_dir=runs_dir() / run_id,
        store=store,
        audit=audit,
        policy=policy,
        approvals=ApprovalService(store, audit, policy, mode=config.approve_mode, operator=config.operator),
        provider=provider or build_provider(),
        workspace=GitWorkspace(workspace_path(meta)),
        graph=graph,
        states=states,
        config=config,
        scenario=meta["scenario"],
    )


def execute(ctx: RunContext) -> RunStatus:
    return asyncio.run(Orchestrator(ctx).execute())
