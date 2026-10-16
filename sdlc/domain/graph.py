"""Explicit dependency graph (DAG) of SDLC nodes.

The graph is mutable at runtime: planner nodes add work packages, and re-planning
removes nodes that a stale planner generated. Every mutation re-validates acyclicity so
the executor can never be handed a graph it cannot finish.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from sdlc.domain.models import NodeSpec


class GraphError(ValueError):
    pass


class DependencyGraph:
    def __init__(self, nodes: Iterable[NodeSpec] = ()) -> None:
        self._nodes: dict[str, NodeSpec] = {}
        for n in nodes:
            self._nodes[n.id] = n
        self.validate()

    # ------------------------------------------------------------------ queries
    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    def get(self, node_id: str) -> NodeSpec:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise GraphError(f"unknown node {node_id!r}") from exc

    @property
    def nodes(self) -> list[NodeSpec]:
        return list(self._nodes.values())

    def dependents(self, node_id: str) -> list[str]:
        return [n.id for n in self._nodes.values() if node_id in n.deps]

    def descendants(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        queue = deque(self.dependents(node_id))
        while queue:
            nid = queue.popleft()
            if nid in seen:
                continue
            seen.add(nid)
            queue.extend(self.dependents(nid))
        return seen

    def consumers_of(self, artifact: str) -> list[str]:
        return [n.id for n in self._nodes.values() if artifact in n.inputs]

    def producer_of(self, artifact: str) -> str | None:
        for n in self._nodes.values():
            if artifact in n.outputs:
                return n.id
        return None

    def topological_order(self) -> list[str]:
        indeg = {nid: 0 for nid in self._nodes}
        for n in self._nodes.values():
            for d in n.deps:
                indeg[n.id] += 1 if d in self._nodes else 0
        queue = deque(sorted(nid for nid, k in indeg.items() if k == 0))
        order: list[str] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for child in sorted(self.dependents(nid)):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)
        if len(order) != len(self._nodes):
            cyclic = sorted(set(self._nodes) - set(order))
            raise GraphError(f"dependency cycle among: {cyclic}")
        return order

    def levels(self) -> dict[str, int]:
        """Longest-path depth of each node — used for layered rendering."""
        level: dict[str, int] = {}
        for nid in self.topological_order():
            deps = [d for d in self._nodes[nid].deps if d in self._nodes]
            level[nid] = 1 + max((level[d] for d in deps), default=-1)
        return level

    # ---------------------------------------------------------------- mutations
    def add(self, node: NodeSpec) -> None:
        if node.id in self._nodes:
            raise GraphError(f"duplicate node {node.id!r}")
        self._nodes[node.id] = node
        try:
            self.validate()
        except GraphError:
            del self._nodes[node.id]
            raise

    def add_dependency(self, node_id: str, dep: str) -> None:
        node = self.get(node_id)
        if dep in node.deps:
            return
        node.deps.append(dep)
        try:
            self.validate()
        except GraphError:
            node.deps.remove(dep)
            raise

    def remove(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        for n in self._nodes.values():
            if node_id in n.deps:
                n.deps.remove(node_id)

    def generated_by(self, planner_id: str) -> list[str]:
        return [n.id for n in self._nodes.values() if n.created_by == planner_id]

    # --------------------------------------------------------------- validation
    def validate(self) -> None:
        for n in self._nodes.values():
            missing = [d for d in n.deps if d not in self._nodes]
            if missing:
                raise GraphError(f"node {n.id!r} depends on unknown {missing}")
            if n.id in n.deps:
                raise GraphError(f"node {n.id!r} depends on itself")
        self.topological_order()

    def to_dict(self) -> list[dict]:
        return [n.to_dict() for n in self._nodes.values()]

    @classmethod
    def from_dict(cls, data: list[dict]) -> DependencyGraph:
        return cls(NodeSpec.from_dict(d) for d in data)
