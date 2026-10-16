"""Per-run Final Engineering Summary, generated from artifacts, decisions, approvals and events.

Everything in it is traceable: each section is derived from stored artifacts (with hashes)
or the audit trail, never from free-form recollection.
"""

from __future__ import annotations

from typing import Any

from sdlc.domain.graph import DependencyGraph
from sdlc.observability.metrics import run_metrics
from sdlc.persistence.store import Store


def mermaid(graph: DependencyGraph, states: dict[str, Any] | None = None) -> str:
    lines = ["flowchart TD"]
    shape = {
        "agent": ("[", "]"),
        "check": ("{{", "}}"),
        "human": ("[/", "/]"),
        "planner": ("[[", "]]"),
        "join": ("((", "))"),
        "action": ("[(", ")]"),
    }
    for n in graph.nodes:
        left, right = shape.get(n.kind.value, ("[", "]"))
        label = n.id + (f" ✋{n.approval.value}" if n.approval else "")
        if states and n.id in states:
            label += f" · {states[n.id].status.value}"
        lines.append(f'    {_mid(n.id)}{left}"{label}"{right}')
    for n in graph.nodes:
        for d in n.deps:
            lines.append(f"    {_mid(d)} --> {_mid(n.id)}")
    return "\n".join(lines)


def _mid(node_id: str) -> str:
    return node_id.replace(":", "_").replace("-", "_")


def engineering_summary(store: Store, run_id: str) -> str:
    meta = store.get_run(run_id) or {}
    arts = {a.name: a for a in store.list_artifacts(run_id)}
    val = {k: a.value for k, a in arts.items()}
    states = store.load_node_states(run_id)
    graph = DependencyGraph.from_dict(meta.get("graph", []))
    m = run_metrics(store, run_id)
    spec, design = val.get("requirements_spec", {}), val.get("design", {})
    verification, security = val.get("verification_report", {}), val.get("security_review", {})
    impact = val.get("codebase_impact")
    out: list[str] = [
        f"# Engineering Summary — {meta.get('scenario')} ({run_id})",
        "",
        f"**Status:** {meta.get('status')}  ",
        f"**Requirement:** {meta.get('requirement', '').strip()[:600]}",
        "",
        "## 1. Requirement understanding",
        spec.get("problem_statement", "_not produced_"),
        "",
    ]
    brief = val.get("requirement", {})
    if brief.get("clarifications"):
        out.append("**Clarifications obtained from a human:**")
        out += [
            f"- {k}: {c['question']} → **{c['answer']}** ({c['by']})"
            for k, c in brief["clarifications"].items()
        ]
        out.append("")
    if brief.get("amendments"):
        out.append("**Amendments applied mid-run (triggered re-planning):**")
        out += [f"- {a['text']} ({a['author']})" for a in brief["amendments"]]
        out.append("")
    out += ["| ID | Requirement | Priority | Verified by tests |", "|---|---|---|---|"]
    trace = verification.get("traceability", {})
    for fr in spec.get("functional", []):
        out.append(
            f"| {fr['id']} | {fr['title']} | {fr.get('priority')} | {'✅' if trace.get(fr['id']) else '—'} |"
        )
    out += [""] + [
        f"- **{n['id']}** ({n['category']}): {n['requirement']} — _{n['measure']}_"
        for n in spec.get("non_functional", [])
    ]
    if impact:
        out += [
            "",
            "## 2. Codebase reasoning (brownfield)",
            impact.get("summary", ""),
            "",
            "**Impacted files:**",
        ]
        out += [f"- `{f['path']}` ({f['change']}): {f['reason']}" for f in impact.get("impacted_files", [])]
        if impact.get("data_flows"):
            out += ["", "**Data flows touched:**"] + [f"- {d}" for d in impact["data_flows"]]
        if impact.get("existing_defects"):
            out += ["", "**Existing defects found:**"] + [
                f"- {d['description']} @ {d['location']}" for d in impact["existing_defects"]
            ]
    out += [
        "",
        "## 3. Plan & decomposition",
        f"Waves (parallel groups): {val.get('plan', {}).get('waves')}",
        "",
    ]
    for wp in design.get("work_packages", []):
        st = states.get(f"impl:{wp['id']}")
        out.append(
            f"- **{wp['id']} {wp['title']}** → {', '.join(wp['files'])} "
            f"(deps: {wp['depends_on'] or '—'}; {st.status.value if st else 'n/a'}, "
            f"{st.attempts if st else 0} attempt(s))"
        )
    out += ["", "```mermaid", mermaid(graph, states), "```", "", "## 4. Architecture decisions"]
    out += [
        f"- **{d['id']} {d['title']}** — {d['decision']}. _Why:_ {d['rationale']}"
        + (f" _Alternatives:_ {', '.join(d['alternatives'])}" if d.get("alternatives") else "")
        for d in design.get("decisions", [])
    ]
    out += ["", "**API contract:**"] + [
        f"- `{e['method']} {e['path']}` — {e['summary']}" for e in design.get("api_contract", [])
    ]
    out += ["", "## 5. Validation"]
    for g in verification.get("gates", []):
        out.append(f"- {'✅' if g['passed'] else '❌'} **{g['name']}** — {g['detail'].splitlines()[0][:200]}")
    out += ["", "**Security review:**"] + (
        [
            f"- [{f['severity']}] {f['title']} @ {f['location']} → {f['recommendation']}"
            for f in security.get("findings", [])
        ]
        or ["- no findings"]
    )
    out += ["", "## 6. Risks & trade-offs"]
    out += [
        f"- {r['description']} — _mitigation:_ {r['mitigation']}"
        for r in spec.get("risks", []) + design.get("risks", [])
    ]
    out += ["", "## 7. Assumptions"] + [f"- {a}" for a in spec.get("assumptions", [])]
    out += [f"- {d.title} ({d.actor})" for d in store.list_decisions(run_id) if "default" in d.title]
    out += ["", "## 8. Human oversight"]
    for a in store.list_approvals(run_id):
        out.append(
            f"- `{a.kind.value}` on **{a.node_id}**: {a.status.value} by {a.decided_by or '—'}"
            + (f" — “{a.comment}”" if a.comment else "")
        )
    out += ["", "## 9. Reliability metrics", "| metric | value |", "|---|---|"]
    for k in (
        "e2e_latency_s",
        "attempt_success_rate",
        "first_pass_yield",
        "retries",
        "rollbacks",
        "mttr_s",
        "repair_loops",
        "replans",
        "reused_nodes",
        "safe_stops",
        "policy_denials",
        "approvals_requested",
        "approvals_rejected",
        "llm_cost_usd",
        "cache_hit_ratio",
    ):
        out.append(f"| {k} | {m.get(k)} |")
    out += [
        "",
        "## 10. Limitations",
        *[f"- {x}" for x in spec.get("out_of_scope", [])],
        "- Generated code is validated by automated gates and a security review agent; a human should still "
        "review the release diff before production deployment.",
        "",
    ]
    rel = val.get("release")
    if rel:
        out += [
            "## 11. Release",
            f"- tag `{rel['tag']}` @ `{rel['commit'][:12]}`",
            f"- bundle `{rel['bundle']}`",
            f"- sha256 `{rel['sha256']}`",
            "",
        ]
    out += [
        "## Artifact lineage",
        "| artifact | version | hash | producer | derived from |",
        "|---|---|---|---|---|",
    ]
    for a in sorted(arts.values(), key=lambda x: x.created_at):
        out.append(
            f"| {a.name} | v{a.version} | `{a.hash}` | {a.producer} | "
            f"{', '.join(f'{k}@{v[:8]}' for k, v in a.derived_from.items()) or '—'} |"
        )
    return "\n".join(out) + "\n"
