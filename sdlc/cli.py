"""`sdlc` command-line interface: run scenarios, oversee them, inspect the audit trail."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sdlc.domain.graph import DependencyGraph
from sdlc.domain.models import ApprovalStatus, RunStatus
from sdlc.engine.bootstrap import create_run, execute, load_context, load_scenario, open_store, runs_dir
from sdlc.engine.context import RunConfig
from sdlc.governance.approvals import ApprovalError, ApprovalService
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.llm.factory import default_models, load_dotenv
from sdlc.observability.metrics import portfolio_metrics, run_metrics
from sdlc.observability.report import engineering_summary, mermaid

COLORS = {
    "ok": "\033[32m",
    "fail": "\033[31m",
    "warn": "\033[33m",
    "info": "\033[36m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "end": "\033[0m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    COLORS = dict.fromkeys(COLORS, "")


def c(kind: str, text: str) -> str:
    return f"{COLORS[kind]}{text}{COLORS['end']}"


# ------------------------------------------------------------------ live view
def live_printer(event: dict[str, Any]) -> None:
    t, n, d, a = event["type"], event["node_id"] or "-", event["data"], event["attempt"]
    line = None
    if t == "node.attempt_started":
        line = c("info", f"▶ {n}") + c("dim", f"  attempt {a} · {d.get('model')}")
    elif t == "node.succeeded":
        line = (
            c("ok", f"✔ {n}")
            + f"  {d.get('summary', '')[:110]}"
            + c("dim", f"  ({d.get('duration_s')}s, ${d.get('cost_usd', 0):.3f})")
        )
    elif t == "node.attempt_failed":
        line = c("fail", f"✘ {n}") + f"  attempt {a} [{d.get('kind')}] {d.get('error', '')[:160]}"
    elif t == "node.rolled_back":
        line = c("warn", f"↺ {n}") + "  rolled back (worktree discarded)"
    elif t == "approval.requested":
        line = c("bold", f"✋ APPROVAL NEEDED [{d.get('kind')}] {n}: {d.get('summary')}") + c(
            "dim",
            f"\n   approve: sdlc approve {d.get('approval_id')}   reject: sdlc reject {d.get('approval_id')} "
            f"-m 'why'   or use the dashboard",
        )
    elif t == "approval.decided":
        line = c(
            "ok" if d.get("decision") == "approved" else "warn",
            f"✋ {d.get('kind')} {d.get('decision')} by {d.get('actor')}",
        ) + (f"  “{d.get('comment')}”" if d.get("comment") else "")
    elif t == "plan.expanded":
        line = c("info", f"⎇ plan expanded: {', '.join(d.get('added', []))}")
    elif t == "replan.invalidated":
        line = c("warn", f"⟲ re-plan: {d.get('nodes')} — {d.get('reason', '')[:120]}")
    elif t == "replan.repair_loop":
        line = c("warn", f"⟲ repair loop inserted after {n}: {d.get('inserted')}")
    elif t == "replan.workspace_reset":
        line = c("warn", f"↺ main reset to {d.get('to_sha', '')[:10]} ({d.get('reason', '')[:80]})")
    elif (
        t == "tool.guard" and not d.get("allowed", True) or t == "tool.command" and not d.get("allowed", True)
    ):
        line = c("warn", f"⛔ policy denied {n}: {d.get('rule')} {d.get('reason', '')[:120]}")
    elif t == "node.reused":
        line = c("dim", f"≡ {n} reused (inputs unchanged)")
    elif t == "amendment.applied":
        line = c("warn", f"✎ amendment by {d.get('author')}: {d.get('text', '')[:120]}")
    elif t in ("run.stopped", "run.finished"):
        line = c(
            "bold",
            f"■ run {t.split('.')[1]}: {d.get('status', '')} {d.get('reason') or d.get('error') or ''}",
        )
    if line:
        print(line, flush=True)


def _schedule_amendments(store, run_id: str, scenario: dict[str, Any], audit: AuditLog) -> None:
    pending = [a for a in scenario.get("amendments", []) or [] if a.get("after_node")]
    fired: set[int] = set()

    def on_event(event: dict[str, Any]) -> None:
        if event["type"] != "node.succeeded":
            return
        for i, amd in enumerate(pending):
            if i not in fired and event["node_id"] == amd["after_node"]:
                fired.add(i)
                store.add_amendment(run_id, amd["text"], amd.get("author", "product-owner"))

    audit.subscribe(on_event)


# ------------------------------------------------------------------- commands
def cmd_run(args: argparse.Namespace) -> int:
    scenario = load_scenario(args.scenario)
    store = open_store()
    model, fallback = default_models()
    config = RunConfig(
        model=args.model or model,
        fallback_model=args.fallback_model or fallback,
        effort=args.effort,
        max_parallel=args.max_parallel,
        approve_mode=args.approve,
        operator=args.operator,
    )
    run_id = create_run(store, scenario, config)
    print(
        c(
            "bold",
            f"run {run_id} · scenario {scenario['name']} ({scenario['mode']}) · approvals={args.approve}",
        )
    )
    print(c("dim", f"workspace: {runs_dir() / run_id / 'repo'}"))
    return _execute(store, run_id, args.approve, scenario)


def cmd_resume(args: argparse.Namespace) -> int:
    store = open_store()
    return _execute(store, args.run_id, args.approve, None)


def _execute(store, run_id: str, approve: str | None, scenario: dict[str, Any] | None) -> int:
    audit = AuditLog(store)
    audit.subscribe(live_printer)
    if scenario:
        _schedule_amendments(store, run_id, scenario, audit)
    ctx = load_context(store, run_id, approve_mode=approve, audit=audit)
    try:
        status = execute(ctx)
    except KeyboardInterrupt:
        print(c("warn", "\ninterrupted — run state is persisted; resume with: sdlc resume " + run_id))
        return 130
    m = run_metrics(store, run_id)
    print(c("bold", f"\nrun {run_id}: {status.value}"))
    print(
        json.dumps(
            {
                k: m[k]
                for k in (
                    "e2e_latency_s",
                    "attempt_success_rate",
                    "retries",
                    "rollbacks",
                    "mttr_s",
                    "repair_loops",
                    "replans",
                    "approvals_requested",
                    "llm_cost_usd",
                )
            }
        )
    )
    report = runs_dir() / run_id / "ENGINEERING_SUMMARY.md"
    report.write_text(engineering_summary(store, run_id))
    print(c("dim", f"engineering summary: {report}"))
    return 0 if status == RunStatus.SUCCEEDED else (3 if status == RunStatus.STOPPED else 1)


def cmd_runs(args: argparse.Namespace) -> int:
    for r in open_store().list_runs(args.limit):
        print(f"{r['id']}  {r['status']:<10} {r['scenario']:<14} {(r['error'] or '')[:80]}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = open_store()
    run_id = args.run_id or store.list_runs(1)[0]["id"]
    meta = store.get_run(run_id)
    states = store.load_node_states(run_id)
    graph = DependencyGraph.from_dict(meta["graph"])
    print(
        c("bold", f"{run_id} · {meta['scenario']} · {meta['status']}")
        + (f"  ({meta['error']})" if meta["error"] else "")
    )
    for nid in graph.topological_order():
        st, spec = states[nid], graph.get(nid)
        color = {"succeeded": "ok", "failed": "fail", "running": "info", "awaiting_approval": "warn"}.get(
            st.status.value, "dim"
        )
        print(
            f"  {c(color, f'{st.status.value:<18}')} {nid:<22} {spec.stage.value:<15} "
            f"a{st.attempts} ${st.cost_usd:.3f}  {(st.summary or st.error or '')[:70]}"
        )
    pending = store.list_approvals(run_id, ApprovalStatus.PENDING)
    for a in pending:
        print(c("warn", f"  ✋ pending {a.id} [{a.kind.value}] {a.node_id}: {a.summary}"))
    return 0


def cmd_approvals(args: argparse.Namespace) -> int:
    for a in open_store().list_approvals(args.run_id, ApprovalStatus.PENDING):
        print(f"{a.id}  {a.run_id}  [{a.kind.value}] {a.node_id}: {a.summary}")
        if args.verbose:
            print(json.dumps(a.payload, indent=2, default=str)[:4000])
    return 0


def _service() -> ApprovalService:
    store = open_store()
    return ApprovalService(store, AuditLog(store), Policy.load())


def cmd_approve(args: argparse.Namespace) -> int:
    answers = dict(a.split("=", 1) for a in args.answer or [])
    response: dict[str, Any] = {}
    if answers:
        response["answers"] = answers
    if args.action:
        response["action"] = args.action
        response["guidance"] = args.comment or ""
    try:
        a = _service().decide(
            args.approval_id, True, f"human:{args.operator}", args.comment or "", response or None
        )
    except ApprovalError as exc:
        print(c("fail", str(exc)))
        return 2
    print(c("ok", f"approved {a.id} ({a.kind.value} on {a.node_id})"))
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    try:
        a = _service().decide(args.approval_id, False, f"human:{args.operator}", args.comment)
    except ApprovalError as exc:
        print(c("fail", str(exc)))
        return 2
    print(c("warn", f"rejected {a.id} — the agent will retry with your feedback"))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    open_store().request_stop(args.run_id, args.reason)
    print(
        c("warn", f"safe-stop requested for {args.run_id}: in-flight work will be rolled back; resume later")
    )
    return 0


def cmd_amend(args: argparse.Namespace) -> int:
    aid = open_store().add_amendment(args.run_id, args.text, f"human:{args.operator}")
    print(
        c(
            "ok",
            f"amendment {aid} queued; a live run re-plans on its next tick (or `sdlc resume {args.run_id}`)",
        )
    )
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    store = open_store()
    data = run_metrics(store, args.run_id) if args.run_id else portfolio_metrics(store)
    if not args.run_id:
        data = {k: v for k, v in data.items() if k != "per_run"}
    print(json.dumps(data, indent=2))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    store = open_store()
    if args.verify:
        ok, msg = AuditLog(store).verify(args.run_id)
        print(c("ok" if ok else "fail", msg))
        return 0 if ok else 1
    events = store.events(args.run_id, types=args.type or None)
    for e in events[-args.tail :] if args.tail else events:
        print(
            f"{e['seq']:5} {e['type']:<24} {str(e['node_id']):<20} {json.dumps(e['data'], default=str)[:200]}"
        )
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    store = open_store()

    def show(name: str, h: str | None, depth: int) -> None:
        art = store.artifact_by_hash(args.run_id, name, h) if h else store.latest_artifact(args.run_id, name)
        if art is None:
            print("  " * depth + f"{name} (missing)")
            return
        print("  " * depth + f"{name} v{art.version} #{art.hash} ← {art.producer}")
        for dep, dep_hash in art.derived_from.items():
            show(dep, dep_hash, depth + 1)

    show(args.artifact, None, 0)
    for d in store.list_decisions(args.run_id):
        if args.artifact in d.based_on:
            print(f"  decision: {d.title} ({d.actor})")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    text = engineering_summary(open_store(), args.run_id)
    if args.out:
        Path(args.out).write_text(text)
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    store = open_store()
    meta = store.get_run(args.run_id)
    print(mermaid(DependencyGraph.from_dict(meta["graph"]), store.load_node_states(args.run_id)))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from sdlc.engine.bootstrap import home
    from sdlc.snapshot import snapshot

    runs = snapshot(Path(args.home) if args.home else home(), Path(args.out))
    print(c("ok", f"snapshot of {len(runs)} runs written to {args.out}: {', '.join(runs)}"))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from sdlc.snapshot import restore

    try:
        runs = restore(Path(args.source), Path(args.home), force=args.force)
    except FileExistsError as exc:
        print(c("fail", str(exc)))
        return 2
    print(c("ok", f"restored {len(runs)} runs into {args.home}"))
    print(
        c(
            "dim",
            f"inspect: SDLC_HOME={args.home} sdlc runs | status <run> | audit <run> --verify | dashboard",
        )
    )
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    import uvicorn

    from sdlc.dashboard.app import create_app

    print(c("bold", f"dashboard: http://{args.host}:{args.port}"))
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


# ---------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdlc", description="Governed agentic SDLC orchestrator")
    p.add_argument("--operator", default=os.environ.get("SDLC_OPERATOR", os.environ.get("USER", "operator")))
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="start a scenario run")
    r.add_argument("scenario")
    r.add_argument(
        "--approve",
        choices=["dashboard", "console", "auto"],
        default="console",
        help="how human checkpoints are answered (release always needs a human)",
    )
    r.add_argument("--model")
    r.add_argument("--fallback-model")
    r.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--max-parallel", type=int, default=3)
    r.set_defaults(fn=cmd_run)

    rs = sub.add_parser("resume", help="resume a stopped/failed run (re-plans pending amendments)")
    rs.add_argument("run_id")
    rs.add_argument("--approve", choices=["dashboard", "console", "auto"])
    rs.set_defaults(fn=cmd_resume)

    sub.add_parser("runs", help="list runs").set_defaults(fn=cmd_runs, limit=30)
    st = sub.add_parser("status", help="node-level status of a run")
    st.add_argument("run_id", nargs="?")
    st.set_defaults(fn=cmd_status)

    ap = sub.add_parser("approvals", help="list pending approvals")
    ap.add_argument("--run-id")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.set_defaults(fn=cmd_approvals)

    a = sub.add_parser("approve", help="approve a checkpoint")
    a.add_argument("approval_id")
    a.add_argument("-m", "--comment")
    a.add_argument("--answer", action="append", help="clarification answer, e.g. --answer Q-1='30 days'")
    a.add_argument("--action", choices=["retry", "skip", "abort"], help="for escalations")
    a.set_defaults(fn=cmd_approve)

    rj = sub.add_parser("reject", help="reject a checkpoint with feedback for the agent")
    rj.add_argument("approval_id")
    rj.add_argument("-m", "--comment", required=True)
    rj.set_defaults(fn=cmd_reject)

    s = sub.add_parser("stop", help="safe-stop a run (rolls back in-flight work; resumable)")
    s.add_argument("run_id")
    s.add_argument("--reason", default="operator requested safe stop")
    s.set_defaults(fn=cmd_stop)

    am = sub.add_parser("amend", help="change the requirement of a run; triggers re-planning")
    am.add_argument("run_id")
    am.add_argument("text")
    am.set_defaults(fn=cmd_amend)

    me = sub.add_parser("metrics", help="reliability metrics for a run or all runs")
    me.add_argument("run_id", nargs="?")
    me.set_defaults(fn=cmd_metrics)

    au = sub.add_parser("audit", help="show or verify the hash-chained audit trail")
    au.add_argument("run_id")
    au.add_argument("--verify", action="store_true")
    au.add_argument("--type", action="append")
    au.add_argument("--tail", type=int, default=0)
    au.set_defaults(fn=cmd_audit)

    li = sub.add_parser("lineage", help="artifact lineage tree")
    li.add_argument("run_id")
    li.add_argument("artifact")
    li.set_defaults(fn=cmd_lineage)

    rp = sub.add_parser("report", help="final engineering summary for a run")
    rp.add_argument("run_id")
    rp.add_argument("-o", "--out")
    rp.set_defaults(fn=cmd_report)

    g = sub.add_parser("graph", help="mermaid diagram of a run's dependency graph")
    g.add_argument("run_id")
    g.set_defaults(fn=cmd_graph)

    sn = sub.add_parser("snapshot", help="write a portable, committable snapshot of all runs")
    sn.add_argument("--out", default="demo-state")
    sn.add_argument("--home", help="state home to snapshot (default: $SDLC_HOME or .sdlc)")
    sn.set_defaults(fn=cmd_snapshot)

    rs2 = sub.add_parser("restore", help="rebuild a state home from a snapshot (no API key needed)")
    rs2.add_argument("--from", dest="source", default="demo-state")
    rs2.add_argument("--home", default=".sdlc-demo")
    rs2.add_argument("--force", action="store_true")
    rs2.set_defaults(fn=cmd_restore)

    d = sub.add_parser("dashboard", help="web dashboard for oversight and approvals")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8765)
    d.set_defaults(fn=cmd_dashboard)
    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
