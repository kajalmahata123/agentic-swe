"""Oversight dashboard: live DAG, approvals inbox, audit trail, metrics, artifacts, workspace.

Reads the same SQLite state the engine writes; decisions written here (approvals, stops,
amendments) are picked up by the engine wherever it runs (CLI process or a run started from
this dashboard in a background thread).

Security: binds to localhost by default. If SDLC_DASHBOARD_TOKEN is set, every mutating
request must carry it in the `X-SDLC-Token` header.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from sdlc.domain.graph import DependencyGraph
from sdlc.domain.models import ApprovalStatus
from sdlc.engine.bootstrap import create_run, execute, load_context, load_scenario, open_store, runs_dir
from sdlc.engine.context import RunConfig
from sdlc.governance.approvals import ApprovalError, ApprovalService
from sdlc.governance.audit import AuditLog
from sdlc.governance.policy import Policy
from sdlc.llm.factory import default_models
from sdlc.observability.metrics import portfolio_metrics, run_metrics
from sdlc.observability.report import engineering_summary, mermaid

STATIC = Path(__file__).parent / "static"
SCENARIOS = Path(os.environ.get("SDLC_SCENARIOS", Path(__file__).resolve().parents[2] / "scenarios"))


class ApproveBody(BaseModel):
    comment: str = ""
    answers: dict[str, str] = Field(default_factory=dict)
    action: str | None = None
    operator: str = "dashboard"


class RejectBody(BaseModel):
    comment: str = Field(min_length=3)
    operator: str = "dashboard"


class StartBody(BaseModel):
    scenario: str
    approve_mode: str = "dashboard"
    max_parallel: int = 3


class TextBody(BaseModel):
    text: str = Field(min_length=3)
    operator: str = "dashboard"


def require_token(x_sdlc_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("SDLC_DASHBOARD_TOKEN")
    if expected and x_sdlc_token != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-SDLC-Token")


def create_app() -> FastAPI:
    app = FastAPI(title="Agentic SDLC — oversight dashboard")
    store = open_store()
    policy = Policy.load()
    approvals = ApprovalService(store, AuditLog(store), policy)
    threads: dict[str, threading.Thread] = {}

    def _run_or_404(run_id: str) -> dict[str, Any]:
        meta = store.get_run(run_id)
        if meta is None:
            raise HTTPException(status_code=404, detail="run not found")
        return meta

    def _start_thread(run_id: str, approve_mode: str | None) -> None:
        def target() -> None:
            try:
                ctx = load_context(store, run_id, approve_mode=approve_mode)
                execute(ctx)
                (runs_dir() / run_id / "ENGINEERING_SUMMARY.md").write_text(
                    engineering_summary(store, run_id)
                )
            except Exception as exc:  # surfaced via run.error / audit trail
                store.update_run(run_id, status="failed", error=f"engine thread crashed: {exc!r}")

        t = threading.Thread(target=target, name=f"run-{run_id}", daemon=True)
        threads[run_id] = t
        t.start()

    # ------------------------------------------------------------------ runs
    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        out = []
        for r in store.list_runs(100):
            pending = len(store.list_approvals(r["id"], ApprovalStatus.PENDING))
            out.append(
                {
                    **r,
                    "pending_approvals": pending,
                    "live": r["id"] in threads and threads[r["id"]].is_alive(),
                }
            )
        return out

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        meta = _run_or_404(run_id)
        graph = DependencyGraph.from_dict(meta["graph"])
        states = store.load_node_states(run_id)
        levels = graph.levels()
        nodes = []
        for spec in graph.nodes:
            st = states.get(spec.id)
            nodes.append(
                {
                    **spec.to_dict(),
                    "level": levels.get(spec.id, 0),
                    "status": st.status.value if st else "pending",
                    "attempts": st.attempts if st else 0,
                    "cost_usd": round(st.cost_usd, 4) if st else 0,
                    "summary": st.summary if st else None,
                    "error": st.error if st else None,
                    "started_at": st.started_at if st else None,
                    "finished_at": st.finished_at if st else None,
                    "base_sha": st.base_sha if st else None,
                    "merge_sha": st.merge_sha if st else None,
                }
            )
        return {
            "run": {
                k: meta[k]
                for k in (
                    "id",
                    "scenario",
                    "requirement",
                    "status",
                    "created_at",
                    "started_at",
                    "finished_at",
                    "error",
                    "stop_reason",
                    "workspace",
                    "config",
                )
            },
            "nodes": nodes,
            "approvals": [_approval(a) for a in store.list_approvals(run_id)],
            "metrics": run_metrics(store, run_id),
            "amendments": store.all_amendments(run_id),
            "mermaid": mermaid(graph, states),
            "live": run_id in threads and threads[run_id].is_alive(),
        }

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = 0, types: str | None = None, limit: int = Query(500, le=5000)):
        _run_or_404(run_id)
        return store.events(run_id, after_seq=after, types=types.split(",") if types else None, limit=limit)

    @app.get("/api/runs/{run_id}/audit/verify")
    def verify_audit(run_id: str) -> dict[str, Any]:
        ok, msg = AuditLog(store).verify(run_id)
        return {"ok": ok, "message": msg}

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(run_id)
        return [
            {
                "name": a.name,
                "version": a.version,
                "hash": a.hash,
                "producer": a.producer,
                "derived_from": a.derived_from,
                "created_at": a.created_at,
            }
            for a in store.list_artifacts(run_id, latest_only=False)
        ]

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    def artifact(run_id: str, name: str, version: int | None = None) -> dict[str, Any]:
        arts = [a for a in store.list_artifacts(run_id, latest_only=False) if a.name == name]
        if version:
            arts = [a for a in arts if a.version == version]
        if not arts:
            raise HTTPException(status_code=404, detail="artifact not found")
        a = arts[-1]
        return {
            "name": a.name,
            "version": a.version,
            "hash": a.hash,
            "producer": a.producer,
            "derived_from": a.derived_from,
            "value": a.value,
        }

    @app.get("/api/runs/{run_id}/decisions")
    def decisions(run_id: str) -> list[dict[str, Any]]:
        return [d.__dict__ for d in store.list_decisions(run_id)]

    @app.get("/api/runs/{run_id}/report", response_class=PlainTextResponse)
    def report(run_id: str) -> str:
        _run_or_404(run_id)
        return engineering_summary(store, run_id)

    @app.get("/api/runs/{run_id}/files")
    def files(run_id: str, path: str | None = None) -> dict[str, Any]:
        root = Path(_run_or_404(run_id)["workspace"]).resolve()
        if path is None:
            out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout
            return {"files": [f for f in out.splitlines() if f]}
        target = (root / path).resolve()
        if root not in target.parents or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return {"path": path, "content": target.read_text(errors="replace")[:200_000]}

    @app.get("/api/runs/{run_id}/nodes/{node_id}/diff", response_class=PlainTextResponse)
    def node_diff(run_id: str, node_id: str) -> str:
        meta = _run_or_404(run_id)
        st = store.load_node_states(run_id).get(node_id)
        if not st or not st.base_sha or not st.merge_sha:
            return ""
        out = subprocess.run(
            ["git", "diff", st.base_sha, st.merge_sha], cwd=meta["workspace"], capture_output=True, text=True
        ).stdout
        return out[:300_000]

    @app.get("/api/runs/{run_id}/nodes/{node_id}/transcript")
    def transcript(run_id: str, node_id: str, attempt: int | None = None) -> list[dict[str, Any]]:
        import json

        tdir = runs_dir() / run_id / "transcripts"
        safe = node_id.replace(":", "_")
        candidates = sorted(tdir.glob(f"{safe}-a*.jsonl")) if tdir.exists() else []
        if attempt:
            candidates = [p for p in candidates if p.stem.endswith(f"-a{attempt}")]
        if not candidates:
            return []
        return [json.loads(line) for line in candidates[-1].read_text().splitlines() if line.strip()]

    # ------------------------------------------------------------- approvals
    @app.get("/api/approvals")
    def list_approvals(status: str = "pending") -> list[dict[str, Any]]:
        st = ApprovalStatus(status) if status != "all" else None
        return [_approval(a) for a in store.list_approvals(None, st)]

    @app.post("/api/approvals/{approval_id}/approve", dependencies=[Depends(require_token)])
    def approve(approval_id: str, body: ApproveBody) -> dict[str, Any]:
        response: dict[str, Any] = {}
        if body.answers:
            response["answers"] = body.answers
        if body.action:
            response.update({"action": body.action, "guidance": body.comment})
        try:
            a = approvals.decide(approval_id, True, f"human:{body.operator}", body.comment, response or None)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _approval(a)

    @app.post("/api/approvals/{approval_id}/reject", dependencies=[Depends(require_token)])
    def reject(approval_id: str, body: RejectBody) -> dict[str, Any]:
        try:
            a = approvals.decide(approval_id, False, f"human:{body.operator}", body.comment)
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _approval(a)

    # --------------------------------------------------------- run control
    @app.get("/api/scenarios")
    def scenarios() -> list[dict[str, Any]]:
        out = []
        for p in sorted(SCENARIOS.glob("*.yaml")):
            s = load_scenario(p)
            out.append(
                {
                    "file": p.name,
                    "name": s["name"],
                    "mode": s["mode"],
                    "title": s.get("title", s["name"]),
                    "requirement": s["requirement"].strip(),
                    "seed": s.get("seed"),
                }
            )
        return out

    @app.post("/api/runs", dependencies=[Depends(require_token)])
    def start(body: StartBody) -> dict[str, Any]:
        path = (SCENARIOS / body.scenario).resolve()
        if path.parent != SCENARIOS.resolve() or not path.exists():
            raise HTTPException(status_code=404, detail="scenario not found")
        scenario = load_scenario(path)
        model, fallback = default_models()
        try:
            run_id = create_run(
                store,
                scenario,
                RunConfig(
                    model=model,
                    fallback_model=fallback,
                    approve_mode=body.approve_mode,
                    max_parallel=body.max_parallel,
                ),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        _start_thread(run_id, body.approve_mode)
        return {"run_id": run_id}

    @app.post("/api/runs/{run_id}/stop", dependencies=[Depends(require_token)])
    def stop(run_id: str, body: TextBody | None = None) -> dict[str, Any]:
        _run_or_404(run_id)
        store.request_stop(run_id, body.text if body else "operator requested safe stop (dashboard)")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/resume", dependencies=[Depends(require_token)])
    def resume(run_id: str) -> dict[str, Any]:
        _run_or_404(run_id)
        if run_id in threads and threads[run_id].is_alive():
            raise HTTPException(status_code=409, detail="run is already executing")
        _start_thread(run_id, None)
        return {"ok": True}

    @app.post("/api/runs/{run_id}/amend", dependencies=[Depends(require_token)])
    def amend(run_id: str, body: TextBody) -> dict[str, Any]:
        _run_or_404(run_id)
        return {"amendment_id": store.add_amendment(run_id, body.text, f"human:{body.operator}")}

    @app.get("/api/metrics")
    def metrics() -> dict[str, Any]:
        data = portfolio_metrics(store)
        data.pop("per_run", None)
        return data

    @app.get("/api/policy")
    def get_policy() -> dict[str, Any]:
        return policy.data

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


def _approval(a) -> dict[str, Any]:
    return {
        "id": a.id,
        "run_id": a.run_id,
        "node_id": a.node_id,
        "kind": a.kind.value,
        "status": a.status.value,
        "summary": a.summary,
        "payload": a.payload,
        "subject_hash": a.subject_hash,
        "requested_at": a.requested_at,
        "decided_at": a.decided_at,
        "decided_by": a.decided_by,
        "comment": a.comment,
        "response": a.response,
    }
