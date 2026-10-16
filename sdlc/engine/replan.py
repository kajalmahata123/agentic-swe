"""Dynamic re-planning.

Three triggers change the plan while a run is live:

1. **Upstream output changed** — when a node re-runs and its output hash differs from what
   a downstream consumer last used, that consumer is invalidated (and transitively, only as
   far as outputs actually change). Unchanged outputs stop the cascade: work is reused.
2. **Explicit invalidation** — a clarification answer or a human amendment changes the
   requirement brief; the nodes that consumed it are invalidated.
3. **Repair loops** — a failed verification/security gate inserts `repair-k -> verify-k`
   (and a re-run of the failed check) and rewires dependents onto the new tail.

Invalidating a node that merged code rolls `main` back to that node's base commit; every
other node whose merge is no longer in history is invalidated too, so the workspace and the
plan can never disagree. Pending approvals on invalidated nodes are superseded.
"""

from __future__ import annotations

import copy
from collections.abc import Callable

from sdlc.domain.models import NodeKind, NodeSpec, NodeState, NodeStatus, RetryPolicy, Stage
from sdlc.engine.context import RunContext
from sdlc.engine.gates import ancestors

REPAIR_CREATOR = "engine:repair-loop"


class Replanner:
    def __init__(self, run: RunContext, cancel_node: Callable[[str], None]) -> None:
        self.run = run
        self.cancel_node = cancel_node

    # ----------------------------------------------------------- invalidation
    def on_outputs_changed(self, producer: str, changed: dict[str, str]) -> set[str]:
        """`changed` maps artifact name -> new hash. Invalidate consumers that used another hash."""
        upstream = ancestors(self.run, producer) | {producer}
        victims: set[str] = set()
        for name, new_hash in changed.items():
            for cid in self.run.graph.consumers_of(name):
                if cid in upstream or self.run.graph.get(cid).params.get("superseded"):
                    continue  # never invalidate the producer's own ancestors (would loop)
                st = self.run.states[cid]
                used = st.input_hashes.get(name)
                if st.status != NodeStatus.PENDING and used is not None and used != new_hash:
                    victims.add(cid)
        if victims:
            self.invalidate(victims, f"upstream output changed: {sorted(changed)} from {producer}")
        return victims

    def invalidate(self, node_ids: set[str], reason: str) -> set[str]:
        run = self.run
        affected: set[str] = set()
        queue = [n for n in node_ids if n in run.graph]
        while queue:
            nid = queue.pop()
            if nid in affected or nid not in run.graph:
                continue
            affected.add(nid)
            spec = run.graph.get(nid)
            if spec.kind == NodeKind.PLANNER:
                for child in run.graph.generated_by(nid):
                    queue.append(child)
        # Workspace: roll main back past the earliest merged victim.
        merged = [
            run.states[n]
            for n in affected
            if run.states[n].merge_sha and run.workspace.is_ancestor(run.states[n].merge_sha)
        ]
        if merged:
            history = [c["sha"] for c in run.workspace.log(500)]
            earliest = max(merged, key=lambda s: history.index(s.merge_sha) if s.merge_sha in history else -1)
            target = earliest.base_sha
            run.workspace.reset_main(target)
            run.audit.record(
                run.run_id, "replan.workspace_reset", earliest.node_id, to_sha=target, reason=reason
            )
            # Anything else merged after that point is gone from history too.
            for nid, st in run.states.items():
                if nid not in affected and st.merge_sha and not run.workspace.is_ancestor(st.merge_sha):
                    affected.add(nid)
        removed: list[str] = []
        for nid in sorted(affected):
            spec = run.graph.get(nid)
            if spec.created_by and spec.created_by in affected and spec.created_by != REPAIR_CREATOR:
                self.cancel_node(nid)
                run.graph.remove(nid)
                run.states.pop(nid, None)
                run.store.delete_node_state(run.run_id, nid)
                removed.append(nid)
                continue
            st = run.states[nid]
            if st.status == NodeStatus.RUNNING or st.status == NodeStatus.AWAITING_APPROVAL:
                self.cancel_node(nid)
            reusable = st.status == NodeStatus.SUCCEEDED and not spec.mutates_workspace
            run.states[nid] = NodeState(
                node_id=nid,
                status=NodeStatus.PENDING,
                input_hashes=st.input_hashes,
                output_hashes=st.output_hashes if reusable else {},
                cost_usd=st.cost_usd,
                summary=f"invalidated: {reason}",
                reusable=reusable,
            )
            run.store.save_node_state(run.run_id, run.states[nid])
        run.approvals.supersede(run.run_id, affected, reason)
        run.persist_graph()
        run.audit.record(
            run.run_id,
            "replan.invalidated",
            None,
            nodes=sorted(affected - set(removed)),
            removed=removed,
            reason=reason,
        )
        return affected

    # ------------------------------------------------------------ repair loop
    def insert_repair_loop(self, failed_id: str, cycle: int, findings: str) -> list[str]:
        run = self.run
        failed = run.graph.get(failed_id)
        verify_base = self._base_verify_spec()
        repair_scope = self._repair_scope()
        repair = NodeSpec(
            id=f"repair-{cycle}",
            stage=Stage.IMPLEMENTATION,
            kind=NodeKind.AGENT,
            handler="repair",
            title=f"Repair defects found by {failed_id}",
            # Inherit the failed check's upstream so a later re-plan cannot run the repair early.
            deps=list(failed.deps),
            inputs=["requirements_spec", "design", "verification_report", "security_review", "test_report"],
            outputs=[f"repair_report_{cycle}"],
            write_scope=repair_scope,
            retry=RetryPolicy(max_attempts=2, escalate_model_at=2),
            created_by=REPAIR_CREATOR,
            params={
                "trigger": failed_id,
                "cycle": cycle,
                "findings": findings,
                "approval_paths": ["tests/**"],
            },
        )
        chain: list[NodeSpec] = [repair]
        verify_k = copy.deepcopy(verify_base)
        verify_k.id, verify_k.deps, verify_k.created_by = f"verify-{cycle + 1}", [repair.id], REPAIR_CREATOR
        verify_k.title = f"Re-verify after repair-{cycle}"
        chain.append(verify_k)
        if failed.handler != verify_base.handler:
            rerun = copy.deepcopy(failed)
            base_id = failed.params.get("base_id", failed.id)
            rerun.id = f"{base_id}-{cycle + 1}"
            rerun.deps, rerun.created_by = [verify_k.id], REPAIR_CREATOR
            rerun.params = {**failed.params, "base_id": base_id}
            chain.append(rerun)
        dependents = run.graph.dependents(failed_id)
        for spec in chain:
            run.graph.add(spec)
            run.states[spec.id] = NodeState(node_id=spec.id)
            run.store.save_node_state(run.run_id, run.states[spec.id])
        tail = chain[-1].id
        for dep_id in dependents:
            node = run.graph.get(dep_id)
            node.deps = [tail if d == failed_id else d for d in node.deps]
        # The failed check is now an orphan leaf: it no longer gates anything.
        run.graph.validate()
        run.persist_graph()
        run.audit.record(
            run.run_id,
            "replan.repair_loop",
            failed_id,
            cycle=cycle,
            inserted=[s.id for s in chain],
            rewired=dependents,
        )
        return [s.id for s in chain]

    def _base_verify_spec(self) -> NodeSpec:
        for spec in self.run.graph.nodes:
            if spec.handler == "verify" and not spec.created_by:
                return spec
        raise RuntimeError("graph has no base verify node")

    def _repair_scope(self) -> list[str]:
        design = self.run.value("design") or {}
        files = sorted({f for wp in design.get("work_packages", []) for f in wp.get("files", [])})
        # Docs are in scope so a repair that changes behaviour (e.g. adds auth) also documents it.
        return files + ["tests/**", "requirements.txt", "README.md", "docs/**", "CHANGELOG.md"]

    def repair_cycles(self) -> int:
        return sum(1 for s in self.run.graph.nodes if s.handler == "repair")
