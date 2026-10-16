"""Entry and exit gates applied to every node, on top of each handler's own checks.

Entry gate  — the node may start: dependencies done, no unfinished ancestor, required input
              artifacts present, run not stopping, budget left.
Exit gate   — the node's work may be accepted: handler gates pass, and for workspace-mutating
              nodes the diff passes policy (scope, secrets, unsafe code, deps, size). Policy
              findings of severity `approve` become human policy-exception checkpoints.
"""

from __future__ import annotations

from sdlc.domain.models import DONE_OK, NodeSpec
from sdlc.engine.context import GateResult, RunContext
from sdlc.governance.policy import DiffScan, glob_match


def entry_gate(run: RunContext, spec: NodeSpec) -> list[GateResult]:
    results: list[GateResult] = []
    undone = [d for d in spec.deps if run.states[d].status not in DONE_OK]
    results.append(GateResult("dependencies_done", not undone, f"waiting on {undone}" if undone else "ok"))
    ancestors = _ancestors(run, spec.id)
    unfinished = sorted(a for a in ancestors if run.states[a].status not in DONE_OK)
    results.append(
        GateResult(
            "ancestors_settled", not unfinished, f"upstream not settled: {unfinished}" if unfinished else "ok"
        )
    )
    missing = []
    for name in spec.inputs:
        producer = run.graph.producer_of(name)
        if producer and producer in ancestors and run.artifact(name) is None:
            missing.append(name)
    results.append(
        GateResult("inputs_present", not missing, f"missing artifacts {missing}" if missing else "ok")
    )
    budget = float(run.policy.budgets.get("max_run_cost_usd", 0) or 0)
    spent = run.total_cost()
    over = bool(budget) and spent >= budget
    results.append(GateResult("budget_available", not over, f"spent ${spent:.2f} of ${budget:.2f}"))
    return results


def _ancestors(run: RunContext, node_id: str) -> set[str]:
    seen: set[str] = set()
    stack = list(run.graph.get(node_id).deps)
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in run.graph:
            continue
        seen.add(nid)
        stack.extend(run.graph.get(nid).deps)
    return seen


def ancestors(run: RunContext, node_id: str) -> set[str]:
    return _ancestors(run, node_id)


def diff_gates(spec: NodeSpec, scan: DiffScan, files_outside_scope: list[str]) -> list[GateResult]:
    gates = [
        GateResult(
            "write_scope",
            not files_outside_scope,
            f"files outside write scope: {files_outside_scope}" if files_outside_scope else "ok",
        ),
    ]
    blocking = scan.blocking
    gates.append(
        GateResult(
            "policy_scan",
            not blocking,
            "; ".join(f"{f.rule} {f.title} at {f.path}:{f.line} `{f.snippet}`" for f in blocking)
            or f"{len(scan.findings)} findings, none blocking",
        )
    )
    if scan.warnings:
        gates.append(
            GateResult(
                "policy_warnings",
                True,
                "; ".join(f"{f.rule} {f.title} at {f.path}:{f.line}" for f in scan.warnings),
                severity="warn",
            )
        )
    return gates


def outside_scope(files: list[str], scope: list[str]) -> list[str]:
    return [f for f in files if not glob_match(f, scope)]
