"""The orchestrator: a stateful, governed DAG executor.

Scheduling
    Event loop over the dependency graph. A node launches when its entry gate passes
    (deps done, *all* ancestors settled, inputs present, budget left). Independent nodes run
    in parallel up to `max_parallel`; JOIN nodes are synchronisation barriers. Nodes waiting
    on a human do not block unrelated branches.

Per-node lifecycle (bounded)
    attempt -> handler -> exit gate (handler gates + diff policy scan) -> human approvals
    -> merge into main -> store artifacts (+lineage) -> succeeded.
    Any failure discards the attempt's worktree (exact rollback) and feeds the reason back
    to the next attempt. Transient errors back off; later attempts escalate to the fallback
    model; repairable checks spawn repair loops instead of blind retries; exhausted nodes
    follow their failure policy (escalate to a human / skip / safe-stop).

Run-level controls
    Operator stop, budget exhaustion, circuit breaker (consecutive failures) and fatal provider
    errors all trigger a *safe stop*: in-flight attempts are cancelled and rolled back, state
    is persisted, and `resume` continues exactly where it left off. Amendments submitted while
    the run is live are applied between scheduling ticks and re-plan the graph.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

from sdlc.domain.models import (
    DONE_OK,
    ApprovalKind,
    ApprovalStatus,
    Decision,
    FailurePolicy,
    NodeKind,
    NodeSpec,
    NodeState,
    NodeStatus,
    RunStatus,
    content_hash,
    now,
)
from sdlc.engine.context import HANDLERS, GateResult, NodeContext, NodeResult, RunContext
from sdlc.engine.gates import diff_gates, entry_gate, outside_scope
from sdlc.engine.replan import Replanner
from sdlc.governance.approvals import StopRequested
from sdlc.governance.policy import glob_match
from sdlc.llm.base import FatalLLMError, LLMError
from sdlc.workspace.git_workspace import MergeConflict, TaskWorkspace


class RunBudgetExceeded(Exception):
    pass


class Orchestrator:
    def __init__(self, run: RunContext, tick_s: float = 0.25) -> None:
        self.run = run
        self.tick_s = tick_s
        self.tasks: dict[str, asyncio.Task] = {}
        self.cancels: dict[str, threading.Event] = {}
        self.replanner = Replanner(run, self.cancel_node)
        self.consecutive_failures = 0
        self.first_failure_at: dict[str, float] = {}

    # ================================================================ run loop
    async def execute(self) -> RunStatus:
        run = self.run
        for st in run.states.values():  # resume: in-flight work from a crash/stop restarts cleanly
            if st.status in (NodeStatus.RUNNING, NodeStatus.AWAITING_APPROVAL):
                st.status = NodeStatus.PENDING
                run.store.save_node_state(run.run_id, st)
        run.store.clear_stop(run.run_id)
        meta = run.store.get_run(run.run_id) or {}
        run.store.update_run(
            run.run_id,
            status=RunStatus.RUNNING,
            started_at=meta.get("started_at") or now(),
            finished_at=None,
            error=None,
        )
        run.audit.record(
            run.run_id,
            "run.started",
            None,
            resumed=bool(meta.get("started_at")),
            nodes=len(run.graph),
            config=run.config.to_dict(),
        )
        try:
            while True:
                self._apply_amendments()
                reason = run.stop_reason()
                if reason:
                    await self._safe_stop(reason)
                    return RunStatus.STOPPED
                self._launch_ready()
                if not self.tasks:
                    outcome = self._settle()
                    if outcome:
                        return outcome
                    await asyncio.sleep(self.tick_s)
                    continue
                done, _ = await asyncio.wait(
                    list(self.tasks.values()), timeout=self.tick_s, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    nid = next((k for k, v in self.tasks.items() if v is task), None)
                    if nid:
                        self.tasks.pop(nid, None)
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if isinstance(exc, StopRequested):
                        continue  # the loop will see the stop flag
                    if exc:
                        run.audit.record(run.run_id, "engine.error", nid, error=repr(exc))
                        run.store.request_stop(run.run_id, f"engine error in {nid}: {exc!r}")
        except Exception as exc:
            run.audit.record(run.run_id, "engine.crash", None, error=repr(exc))
            run.store.update_run(run.run_id, status=RunStatus.FAILED, finished_at=now(), error=repr(exc))
            raise

    def _launch_ready(self) -> None:
        run = self.run
        running = sum(1 for nid in self.tasks if run.states[nid].status == NodeStatus.RUNNING)
        for nid in run.graph.topological_order():
            if nid in self.tasks:
                continue
            st = run.states[nid]
            if st.status != NodeStatus.PENDING:
                continue
            spec = run.graph.get(nid)
            gates = entry_gate(run, spec)
            if not all(g.passed for g in gates):
                budget = next(g for g in gates if g.name == "budget_available")
                if not budget.passed:
                    run.store.request_stop(run.run_id, f"budget exhausted: {budget.detail}")
                    return
                continue
            if st.reusable and self._current_input_hashes(spec) == st.input_hashes and st.output_hashes:
                st.status, st.reusable, st.finished_at = NodeStatus.SUCCEEDED, False, now()
                st.summary = "reused: inputs unchanged after re-plan"
                run.store.save_node_state(run.run_id, st)
                run.audit.record(run.run_id, "node.reused", nid, input_hashes=st.input_hashes)
                continue
            if spec.kind not in (NodeKind.JOIN,) and running >= run.config.max_parallel:
                continue
            run.audit.record(run.run_id, "gate.entry", nid, results=[g.to_dict() for g in gates])
            self.tasks[nid] = asyncio.create_task(self._run_node(spec), name=nid)
            running += 1

    def _current_input_hashes(self, spec: NodeSpec) -> dict[str, str]:
        return {n: a.hash for n in spec.inputs if (a := self.run.artifact(n)) is not None}

    def _settle(self) -> RunStatus | None:
        """No tasks running and nothing launchable: decide whether the run is finished."""
        run = self.run
        statuses = {nid: st.status for nid, st in run.states.items()}
        if all(s in DONE_OK for s in statuses.values()):
            return self._finish(RunStatus.SUCCEEDED, None)
        if run.store.pending_amendments(run.run_id):
            return None
        failed = sorted(n for n, s in statuses.items() if s == NodeStatus.FAILED)
        blocked = sorted(n for n, s in statuses.items() if s == NodeStatus.PENDING)
        if failed or blocked:
            return self._finish(RunStatus.FAILED, f"failed: {failed}; blocked: {blocked}")
        return None

    def _finish(self, status: RunStatus, error: str | None) -> RunStatus:
        from sdlc.observability.metrics import run_metrics

        run = self.run
        metrics = run_metrics(run.store, run.run_id)
        run.store.update_run(run.run_id, status=status, finished_at=now(), error=error)
        run.audit.record(run.run_id, "run.finished", None, status=status.value, error=error, metrics=metrics)
        return status

    # ============================================================ node attempt
    async def _run_node(self, spec: NodeSpec) -> None:
        run = self.run
        st = run.states[spec.id]
        handler = HANDLERS[spec.handler]
        st.status, st.started_at, st.error, st.finished_at = NodeStatus.RUNNING, now(), None, None
        inputs = {n: a for n in spec.inputs if (a := run.artifact(n)) is not None}
        st.input_hashes = {n: a.hash for n, a in inputs.items()}
        run.store.save_node_state(run.run_id, st)
        run.audit.record(
            run.run_id,
            "node.started",
            spec.id,
            inputs=st.input_hashes,
            kind=spec.kind.value,
            stage=spec.stage.value,
        )
        feedback: list[str] = []
        attempt_budget = st.attempts + spec.retry.max_attempts
        escalations = 0
        while True:
            if run.states.get(spec.id) is not st:
                return  # invalidated by a re-plan while we were between attempts
            if st.attempts >= attempt_budget:
                action = await self._exhausted(spec, st, feedback, escalations)
                escalations += 1
                if action == "retry":
                    attempt_budget = st.attempts + spec.retry.max_attempts
                    continue
                return
            st.attempts += 1
            attempt = st.attempts
            run.store.save_node_state(run.run_id, st)
            model = run.config.fallback_model if attempt >= spec.retry.escalate_model_at else run.config.model
            task_ws = run.workspace.open_task(spec.id, attempt) if spec.mutates_workspace else None
            cancel = threading.Event()
            self.cancels[spec.id] = cancel
            ctx = NodeContext(
                run=run,
                spec=spec,
                state=st,
                attempt=attempt,
                model=model,
                inputs=inputs,
                feedback=list(feedback),
                task_ws=task_ws,
                cancel=cancel,
                charge=lambda amount, _st=st: self._charge(_st, amount),
                escalated=attempt >= spec.retry.escalate_model_at,
            )
            run.audit.record(
                run.run_id,
                "node.attempt_started",
                spec.id,
                attempt,
                model=model,
                workspace=str(task_ws.path) if task_ws else None,
            )
            t0 = time.time()
            try:
                result = await asyncio.wait_for(handler.run(ctx), timeout=spec.timeout_s)
            except asyncio.CancelledError:
                cancel.set()
                self._discard(task_ws)
                raise
            except StopRequested:
                self._discard(task_ws)
                self._park(st)
                raise
            except RunBudgetExceeded as exc:
                self._discard(task_ws)
                self._park(st)
                run.store.request_stop(run.run_id, str(exc))
                return
            except FatalLLMError as exc:
                self._discard(task_ws)
                self._attempt_failed(spec, st, attempt, "fatal", str(exc), time.time() - t0)
                self._park(st)
                run.store.request_stop(run.run_id, f"LLM provider unusable: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 - every handler failure is a failed attempt
                self._discard(task_ws)
                transient = isinstance(exc, TimeoutError) or (isinstance(exc, LLMError) and exc.transient)
                self._attempt_failed(
                    spec,
                    st,
                    attempt,
                    "transient" if transient else "error",
                    f"{type(exc).__name__}: {exc}",
                    time.time() - t0,
                )
                feedback.append(f"Attempt {attempt} failed with {type(exc).__name__}: {exc}")
                if self._circuit_open():
                    return
                await asyncio.sleep(spec.retry.backoff_s * (2 ** (attempt - 1)) if transient else 0)
                continue
            finally:
                self.cancels.pop(spec.id, None)

            accepted = await self._accept(spec, st, attempt, result, task_ws, feedback, time.time() - t0)
            if accepted:
                return
            if run.states.get(spec.id) is not st or st.status in DONE_OK:
                return  # superseded by a repair loop or invalidated
            if self._circuit_open():
                return

    async def _accept(
        self,
        spec: NodeSpec,
        st: NodeState,
        attempt: int,
        result: NodeResult,
        task_ws: TaskWorkspace | None,
        feedback: list[str],
        duration: float,
    ) -> bool:
        """Exit gate -> approvals -> merge -> artifacts. Returns True when the node succeeded."""
        run = self.run
        gates: list[GateResult] = list(result.gates)
        approvals: list[tuple[ApprovalKind, str, dict[str, Any]]] = []
        diff_text = ""
        if task_ws is not None:
            changes, lines = run.workspace.task_changes(task_ws)
            scan = run.policy.scan_changes(changes, lines, resolve=_reader(task_ws.path))
            gates += diff_gates(spec, scan, outside_scope(list(changes), spec.write_scope))
            diff_text = run.workspace.task_diff(task_ws)
            result.metrics["diff"] = {"files": sorted(changes), "changed_lines": lines}
            for f in scan.needs_approval:
                kind = (
                    ApprovalKind.CHANGE
                    if f.rule.startswith(("CHG", "DEP"))
                    else ApprovalKind.POLICY_EXCEPTION
                )
                approvals.append((kind, f"{f.rule}: {f.title} ({f.path})", {"finding": f.to_dict()}))
            guarded = [p for p in changes if glob_match(p, spec.params.get("approval_paths", []))]
            if guarded:
                approvals.append(
                    (
                        ApprovalKind.CHANGE,
                        f"{spec.id} modified guarded paths {guarded}",
                        {"files": guarded, "reason": "repairs that edit tests need human review"},
                    )
                )
        for g in gates:
            run.audit.record(run.run_id, "gate.result", spec.id, attempt, **g.to_dict())
        failed = [g for g in gates if not g.passed and g.severity == "block"]
        if failed:
            self._discard(task_ws)
            detail = "; ".join(f"[{g.name}] {g.detail}" for g in failed)
            self._attempt_failed(spec, st, attempt, "gate", detail, duration)
            if self._try_repair_loop(spec, st, attempt, result, detail):
                return False
            feedback.append(f"Attempt {attempt} was rejected by the exit gate: {detail}")
            return False

        if spec.approval:
            approvals.insert(
                0, (spec.approval, result.approval_summary or spec.title or spec.id, result.approval_payload)
            )
        for kind, summary, payload in result.extra_approvals:
            approvals.append((ApprovalKind(kind), summary, payload))
        subject = content_hash({"outputs": result.outputs, "diff": content_hash(diff_text)})
        for kind, summary, payload in approvals:
            full = {
                **payload,
                "outputs_preview": _preview(result.outputs),
                "diff": diff_text[:20000] if diff_text else None,
                "gates": [g.to_dict() for g in gates],
                "attempt": attempt,
                "metrics": result.metrics,
            }
            st.status = NodeStatus.AWAITING_APPROVAL
            run.store.save_node_state(run.run_id, st)
            approval = run.approvals.request(run.run_id, spec.id, kind, summary, full, subject)
            try:
                approval = await run.approvals.wait(approval, run.stop_reason)
            except StopRequested:
                self._discard(task_ws)
                self._park(st)
                raise
            if approval.status == ApprovalStatus.SUPERSEDED:
                self._discard(task_ws)
                return False
            run.decide(
                Decision(
                    node_id=spec.id,
                    actor=approval.decided_by or "unknown",
                    title=f"{kind.value} {approval.status.value}",
                    rationale=approval.comment or "",
                    based_on={"subject": subject},
                )
            )
            if approval.status == ApprovalStatus.REJECTED:
                self._discard(task_ws)
                st.status = NodeStatus.RUNNING
                self._attempt_failed(spec, st, attempt, "rejected", approval.comment or "", duration)
                # A rejected risk waiver on a repairable check means "fix it", not "re-review it".
                if kind == ApprovalKind.POLICY_EXCEPTION and self._try_repair_loop(
                    spec,
                    st,
                    attempt,
                    result,
                    f"human declined to accept the risk: {approval.comment}. {summary}",
                ):
                    return False
                feedback.append(
                    f"A human reviewer REJECTED attempt {attempt} ({kind.value}): {approval.comment}. "
                    "Address this feedback specifically."
                )
                return False
        st.status = NodeStatus.RUNNING

        if task_ws is not None:
            run.workspace.commit_task(task_ws, f"{spec.id}: {spec.title or spec.handler} (attempt {attempt})")
            try:
                st.base_sha, st.merge_sha = run.workspace.merge_task(
                    task_ws, f"merge {spec.id} [{spec.stage.value}] attempt {attempt}"
                )
            except MergeConflict as exc:
                self._discard(task_ws)
                self._attempt_failed(spec, st, attempt, "merge_conflict", str(exc)[:2000], duration)
                feedback.append(
                    f"Your changes conflicted with work merged by another task: {str(exc)[:1500]}. "
                    "Re-read the current files and integrate rather than overwrite."
                )
                return False
            self._discard(task_ws)

        for name, value in result.outputs.items():
            art = run.put(name, value, spec.id, derived_from=st.input_hashes)
            st.output_hashes[name] = art.hash
        for d in result.decisions:
            run.decide(d)
        if spec.kind == NodeKind.PLANNER:
            self._apply_plan(spec, result.new_nodes)
        st.status, st.finished_at, st.summary = NodeStatus.SUCCEEDED, now(), result.summary[:2000]
        run.store.save_node_state(run.run_id, st)
        recovered_from = self.first_failure_at.pop(spec.id, None)
        run.audit.record(
            run.run_id,
            "node.succeeded",
            spec.id,
            attempt,
            duration_s=round(duration, 3),
            total_s=round(st.finished_at - (st.started_at or st.finished_at), 3),
            cost_usd=round(st.cost_usd, 4),
            outputs=st.output_hashes,
            summary=result.summary[:500],
            metrics=result.metrics,
        )
        if recovered_from is not None:
            run.audit.record(
                run.run_id, "node.recovered", spec.id, attempt, recovery_s=round(now() - recovered_from, 3)
            )
        self.consecutive_failures = 0
        self.replanner.on_outputs_changed(spec.id, dict(st.output_hashes))
        if result.invalidate:
            self.replanner.invalidate(set(result.invalidate), f"requested by {spec.id}")
        return True

    def _try_repair_loop(
        self, spec: NodeSpec, st: NodeState, attempt: int, result: NodeResult, detail: str
    ) -> bool:
        """Supersede a failed repairable check with repair-k -> verify-k (-> re-run). False if not allowed."""
        run = self.run
        cycles = self.replanner.repair_cycles()
        if not spec.params.get("repairable") or cycles >= int(run.policy.budgets.get("max_repair_cycles", 2)):
            return False
        self.replanner.insert_repair_loop(spec.id, cycles + 1, detail)
        for name, value in result.outputs.items():  # keep the failing report: repair reads it
            art = run.put(name, value, spec.id, derived_from=st.input_hashes)
            st.output_hashes[name] = art.hash
        st.status, st.finished_at = NodeStatus.SKIPPED, now()
        st.summary = f"superseded by repair loop {cycles + 1}"
        spec.params["superseded"] = True
        run.persist_graph()
        run.store.save_node_state(run.run_id, st)
        run.audit.record(run.run_id, "node.superseded", spec.id, attempt, by=f"repair-{cycles + 1}")
        return True

    # ============================================================== failures
    def _attempt_failed(
        self, spec: NodeSpec, st: NodeState, attempt: int, kind: str, error: str, duration: float
    ) -> None:
        self.consecutive_failures += 1
        self.first_failure_at.setdefault(spec.id, now())
        st.error = error[:4000]
        self.run.store.save_node_state(self.run.run_id, st)
        self.run.audit.record(
            self.run.run_id,
            "node.attempt_failed",
            spec.id,
            attempt,
            kind=kind,
            error=error[:4000],
            duration_s=round(duration, 3),
            rolled_back=spec.mutates_workspace,
        )
        if spec.mutates_workspace:
            self.run.audit.record(
                self.run.run_id,
                "node.rolled_back",
                spec.id,
                attempt,
                reason=kind,
                detail="attempt worktree discarded; main untouched",
            )

    def _circuit_open(self) -> bool:
        limit = int(self.run.policy.budgets.get("max_consecutive_failures", 5))
        if self.consecutive_failures >= limit:
            self.run.store.request_stop(
                self.run.run_id, f"circuit breaker: {self.consecutive_failures} consecutive failed attempts"
            )
            return True
        return False

    async def _exhausted(self, spec: NodeSpec, st: NodeState, feedback: list[str], escalations: int) -> str:
        run = self.run
        run.audit.record(
            run.run_id, "node.exhausted", spec.id, attempts=st.attempts, policy=spec.on_failure.value
        )
        if spec.on_failure == FailurePolicy.SKIP:
            st.status, st.finished_at = NodeStatus.SKIPPED, now()
            st.summary = f"optional node skipped after {st.attempts} attempts: {st.error}"
            run.store.save_node_state(run.run_id, st)
            run.audit.record(run.run_id, "node.skipped", spec.id, reason=st.summary)
            return "skip"
        if spec.on_failure == FailurePolicy.SAFE_STOP or (
            run.config.approve_mode == "auto" and escalations >= 1
        ):
            self._fail(st, f"exhausted {st.attempts} attempts: {st.error}")
            run.store.request_stop(run.run_id, f"{spec.id} failed after {st.attempts} attempts")
            return "stop"
        st.status = NodeStatus.AWAITING_APPROVAL
        run.store.save_node_state(run.run_id, st)
        approval = run.approvals.request(
            run.run_id,
            spec.id,
            ApprovalKind.ESCALATION,
            f"{spec.id} failed {st.attempts} attempts — retry with guidance, skip, or abort?",
            {
                "last_error": st.error,
                "feedback": feedback[-4:],
                "attempts": st.attempts,
                "options": ["retry", "skip", "abort"],
            },
            content_hash({"node": spec.id, "attempts": st.attempts, "error": st.error}),
        )
        approval = await run.approvals.wait(approval, run.stop_reason)
        action = (approval.response or {}).get(
            "action", "retry" if approval.status == ApprovalStatus.APPROVED else "abort"
        )
        if approval.status == ApprovalStatus.REJECTED:
            action = "abort"
        guidance = (approval.response or {}).get("guidance") or approval.comment
        if action == "retry":
            st.status = NodeStatus.RUNNING
            run.store.save_node_state(run.run_id, st)
            if guidance:
                feedback.append(f"Guidance from a human operator: {guidance}")
            return "retry"
        if action == "skip":
            st.status, st.finished_at = NodeStatus.SKIPPED, now()
            st.summary = f"skipped by {approval.decided_by}: {guidance}"
            run.store.save_node_state(run.run_id, st)
            run.audit.record(run.run_id, "node.skipped", spec.id, reason=st.summary)
            return "skip"
        self._fail(st, f"aborted by {approval.decided_by}: {guidance}")
        run.store.request_stop(run.run_id, f"aborted by {approval.decided_by} at {spec.id}")
        return "abort"

    def _fail(self, st: NodeState, reason: str) -> None:
        st.status, st.finished_at, st.summary = NodeStatus.FAILED, now(), reason[:2000]
        self.run.store.save_node_state(self.run.run_id, st)
        self.run.audit.record(self.run.run_id, "node.failed", st.node_id, reason=reason[:2000])

    def _park(self, st: NodeState) -> None:
        """Return an interrupted node to PENDING so a resume re-runs it from scratch."""
        if self.run.states.get(st.node_id) is st and st.status not in DONE_OK:
            st.status = NodeStatus.PENDING
            self.run.store.save_node_state(self.run.run_id, st)

    def _discard(self, task_ws: TaskWorkspace | None) -> None:
        if task_ws is not None:
            self.run.workspace.discard_task(task_ws)

    # ================================================================ budget
    def _charge(self, st: NodeState, amount: float) -> None:
        run = self.run
        with run.cost_lock:
            st.cost_usd += amount
            total = run.total_cost()
        limit = float(run.policy.budgets.get("max_run_cost_usd", 0) or 0)
        if limit and total > limit:
            raise RunBudgetExceeded(f"run budget exceeded: ${total:.2f} > ${limit:.2f}")

    # ========================================================= graph changes
    def _apply_plan(self, planner: NodeSpec, new_nodes: list[NodeSpec]) -> None:
        run = self.run
        for old in run.graph.generated_by(planner.id):
            run.graph.remove(old)
            run.states.pop(old, None)
            run.store.delete_node_state(run.run_id, old)
        for node in new_nodes:
            node.created_by = planner.id
            run.graph.add(node)
            run.states[node.id] = NodeState(node_id=node.id)
            run.store.save_node_state(run.run_id, run.states[node.id])
        for node in new_nodes:
            for target in node.params.get("feeds", []):
                if target in run.graph:
                    run.graph.add_dependency(target, node.id)
        run.persist_graph()
        run.audit.record(
            run.run_id,
            "plan.expanded",
            planner.id,
            added=[n.id for n in new_nodes],
            edges={n.id: n.deps for n in new_nodes},
        )

    def cancel_node(self, node_id: str) -> None:
        ev = self.cancels.get(node_id)
        if ev:
            ev.set()
        task = self.tasks.pop(node_id, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _apply_amendments(self) -> None:
        run = self.run
        for amd in run.store.pending_amendments(run.run_id):
            brief = dict(run.value("requirement") or {})
            brief.setdefault("amendments", [])
            brief["amendments"] = [
                *brief["amendments"],
                {"id": amd["id"], "text": amd["text"], "author": amd["author"]},
            ]
            producer = run.graph.producer_of("requirement") or "intake"
            run.put("requirement", brief, producer)
            run.store.mark_amendment_applied(amd["id"])
            run.audit.record(
                run.run_id,
                "amendment.applied",
                None,
                amendment_id=amd["id"],
                text=amd["text"],
                author=amd["author"],
            )
            consumers = {
                c for c in run.graph.consumers_of("requirement") if run.states[c].status != NodeStatus.PENDING
            }
            if consumers:
                self.replanner.invalidate(
                    consumers, f"requirement amended by {amd['author']}: {amd['text'][:120]}"
                )

    async def _safe_stop(self, reason: str) -> None:
        run = self.run
        for nid in list(self.tasks):
            ev = self.cancels.get(nid)
            if ev:
                ev.set()
            self.tasks[nid].cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        in_flight = []
        for st in run.states.values():
            if st.status in (NodeStatus.RUNNING, NodeStatus.AWAITING_APPROVAL):
                st.status = NodeStatus.PENDING
                run.store.save_node_state(run.run_id, st)
                in_flight.append(st.node_id)
        self.tasks.clear()
        run.store.update_run(run.run_id, status=RunStatus.STOPPED, finished_at=now(), error=reason)
        run.audit.record(
            run.run_id, "run.stopped", None, reason=reason, rolled_back=in_flight, resumable=True
        )


def _reader(root):
    """File resolver confined to a workspace, used by import-aware policy rules."""

    def read(rel: str) -> str | None:
        fp = (root / rel).resolve()
        if root.resolve() not in fp.parents or not fp.is_file():
            return None
        return fp.read_text(errors="replace")

    return read


def _preview(outputs: dict[str, Any], limit: int = 30000) -> dict[str, Any]:
    import json

    out = {}
    for k, v in outputs.items():
        s = json.dumps(v, default=str)
        out[k] = v if len(s) <= limit else {"truncated": s[:limit]}
    return out
