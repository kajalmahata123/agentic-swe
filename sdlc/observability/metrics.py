"""Reliability metrics, derived purely from the audit trail (single source of truth).

Because metrics are recomputed from immutable events, they are reproducible and cannot drift
from what actually happened.
"""

from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any

from sdlc.persistence.store import Store


def run_metrics(store: Store, run_id: str) -> dict[str, Any]:
    events = store.events(run_id)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        by_type[e["type"]].append(e)

    stage_of = {e["node_id"]: e["data"].get("stage") for e in by_type["node.started"]}
    attempts = by_type["node.attempt_started"]
    failed_attempts = by_type["node.attempt_failed"]
    succeeded = by_type["node.succeeded"]
    retries = [e for e in attempts if (e["attempt"] or 1) > 1]
    first_pass = [e for e in succeeded if (e["attempt"] or 1) == 1]
    recoveries = [e["data"]["recovery_s"] for e in by_type["node.recovered"]]
    failure_kinds: dict[str, int] = defaultdict(int)
    for e in failed_attempts:
        failure_kinds[e["data"].get("kind", "error")] += 1

    stage_latency: dict[str, float] = defaultdict(float)
    for e in succeeded:
        stage_latency[stage_of.get(e["node_id"]) or "unknown"] += e["data"].get("duration_s", 0.0)

    turns = by_type["agent.turn"]
    cost = sum(e["data"].get("cost_usd", 0.0) for e in turns)
    in_tok = sum(e["data"].get("input_tokens", 0) for e in turns)
    cache_tok = sum(e["data"].get("cache_read", 0) for e in turns)
    cache_write = sum(e["data"].get("cache_write", 0) for e in turns)
    denied = [
        e for e in by_type["tool.guard"] + by_type["tool.command"] if not e["data"].get("allowed", True)
    ]
    decided = by_type["approval.decided"]

    started = by_type["run.started"][0]["ts"] if by_type["run.started"] else None
    ended = (
        by_type["run.finished"][-1]["ts"]
        if by_type["run.finished"]
        else (events[-1]["ts"] if events else None)
    )
    total_attempts = len(attempts)
    return {
        "events": len(events),
        "e2e_latency_s": round(ended - started, 2) if started and ended else None,
        "nodes_succeeded": len({e["node_id"] for e in succeeded}),
        "nodes_failed": len(by_type["node.failed"]),
        "attempts": total_attempts,
        "attempt_success_rate": round(len(succeeded) / total_attempts, 3) if total_attempts else None,
        "first_pass_yield": round(len(first_pass) / len(succeeded), 3) if succeeded else None,
        "retries": len(retries),
        "failed_attempts": len(failed_attempts),
        "failure_kinds": dict(failure_kinds),
        "rollbacks": len(by_type["node.rolled_back"]) + len(by_type["replan.workspace_reset"]),
        "mttr_s": round(mean(recoveries), 2) if recoveries else None,
        "recoveries": len(recoveries),
        "repair_loops": len(by_type["replan.repair_loop"]),
        "replans": len(by_type["replan.invalidated"]),
        "reused_nodes": len(by_type["node.reused"]),
        "safe_stops": len(by_type["run.stopped"]),
        "approvals_requested": len(by_type["approval.requested"]),
        "approvals_rejected": sum(1 for e in decided if e["data"].get("decision") == "rejected"),
        "approval_wait_s_avg": round(mean(e["data"].get("wait_s", 0) for e in decided), 2)
        if decided
        else None,
        "policy_denials": len(denied),
        "agent_turns": len(turns),
        "llm_cost_usd": round(cost, 4),
        "input_tokens": in_tok,
        "output_tokens": sum(e["data"].get("output_tokens", 0) for e in turns),
        # share of prompt tokens served from cache (reads / all prompt tokens incl. cache writes)
        "cache_hit_ratio": round(cache_tok / (cache_tok + cache_write + in_tok), 3)
        if (cache_tok + cache_write + in_tok)
        else None,
        "stage_latency_s": {k: round(v, 2) for k, v in stage_latency.items()},
    }


def portfolio_metrics(store: Store) -> dict[str, Any]:
    runs = store.list_runs(limit=500)
    per_run = {r["id"]: run_metrics(store, r["id"]) for r in runs}
    finished = [r for r in runs if r["status"] in ("succeeded", "failed")]
    ok = [r for r in finished if r["status"] == "succeeded"]
    mttrs = [m["mttr_s"] for m in per_run.values() if m["mttr_s"] is not None]
    lat = [m["e2e_latency_s"] for m in per_run.values() if m["e2e_latency_s"]]
    return {
        "runs": len(runs),
        "run_success_rate": round(len(ok) / len(finished), 3) if finished else None,
        "total_retries": sum(m["retries"] for m in per_run.values()),
        "total_rollbacks": sum(m["rollbacks"] for m in per_run.values()),
        "mttr_s_avg": round(mean(mttrs), 2) if mttrs else None,
        "e2e_latency_s_avg": round(mean(lat), 2) if lat else None,
        "llm_cost_usd": round(sum(m["llm_cost_usd"] for m in per_run.values()), 4),
        "per_run": per_run,
    }
