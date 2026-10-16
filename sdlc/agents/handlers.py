"""LLM agent node handlers. Each pairs an agent role with deterministic exit gates.

The agent proposes; the gates (and humans) dispose. A handler never trusts the agent's own
claims where it can check them: requirement traceability, file ownership, compile checks,
test runs and FR coverage are all verified mechanically here.
"""

from __future__ import annotations

import re
from typing import Any

from sdlc.agents.base import AgentHandler, as_json
from sdlc.agents.loop import AgentOutcome
from sdlc.agents.schemas import (
    Design,
    DocsReport,
    ImpactAnalysis,
    ImplementationReport,
    RequirementsSpec,
    SecurityReview,
    TestSuiteReport,
)
from sdlc.checks.runner import compile_check, run_pytest
from sdlc.domain.models import Decision
from sdlc.engine.context import GateResult, NodeContext, NodeResult, register
from sdlc.governance.policy import glob_match


def _must_frs(spec: dict[str, Any]) -> list[str]:
    return [f["id"] for f in spec.get("functional", []) if f.get("priority", "must") == "must"]


def _brief_text(brief: dict[str, Any]) -> str:
    parts = [f"## Requirement brief\n{brief.get('text', '')}"]
    if brief.get("amendments"):
        parts.append(
            "## Amendments (these override earlier text)\n"
            + "\n".join(f"- [{a['id']}] ({a['author']}): {a['text']}" for a in brief["amendments"])
        )
    if brief.get("clarifications"):
        parts.append(
            "## Clarification answers from the product owner\n"
            + "\n".join(
                f"- {qid}: {c['question']} -> ANSWER: {c['answer']}"
                for qid, c in brief["clarifications"].items()
            )
        )
    return "\n\n".join(parts)


# ============================================================== requirements
class RequirementsAgent(AgentHandler):
    name = "requirements"
    role = "requirements"
    output_model = RequirementsSpec
    allow_commands = False

    def build_prompt(self, ctx: NodeContext) -> str:
        brief = ctx.run.value("requirement") or {}
        prompt = _brief_text(brief)
        impact = ctx.input("codebase_impact")
        if impact:
            prompt += "\n\n## Impact analysis of the existing codebase\n" + as_json(impact)
        previous = ctx.run.value("requirements_spec")
        if previous:
            prompt += (
                "\n\n## Previous version of the specification (keep ids stable; revise per the "
                "answers/amendments above)\n" + as_json(previous)
            )
        return prompt + "\n\nProduce the RequirementsSpec and submit it."

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        gates = []
        ids = [f["id"] for f in output["functional"]] + [n["id"] for n in output["non_functional"]]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        gates.append(
            GateResult("unique_requirement_ids", not dupes, f"duplicates: {dupes}" if dupes else "ok")
        )
        answered = set((ctx.run.value("requirement") or {}).get("clarifications", {}))
        still_blocking = [q["id"] for q in output["ambiguities"] if q["id"] in answered and q["blocking"]]
        gates.append(
            GateResult(
                "answered_questions_resolved",
                not still_blocking,
                f"answered but still blocking: {still_blocking}" if still_blocking else "ok",
            )
        )
        blocking = [q["id"] for q in output["ambiguities"] if q["blocking"] and not q.get("resolved_by")]
        summary = (
            f"{len(output['functional'])} FRs, {len(output['non_functional'])} NFRs, "
            f"{len(output['ambiguities'])} ambiguities ({len(blocking)} blocking)"
        )
        decisions = [
            Decision(ctx.spec.id, "agent:requirements", f"Assumption: {a}", "stated by analyst")
            for a in output.get("assumptions", [])[:10]
        ]
        return NodeResult(
            outputs={"requirements_spec": output}, summary=summary, gates=gates, decisions=decisions
        )


# ================================================================ brownfield
class ImpactAnalyst(AgentHandler):
    name = "impact_analysis"
    role = "impact_analyst"
    output_model = ImpactAnalysis
    allow_commands = True

    def build_prompt(self, ctx: NodeContext) -> str:
        brief = ctx.run.value("requirement") or {}
        return (
            _brief_text(brief)
            + "\n\n## Deterministic codebase index\n"
            + as_json(ctx.input("codebase_index"))
            + "\n\nAnalyse the existing codebase for this change and submit an ImpactAnalysis."
        )

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        index = ctx.input("codebase_index") or {}
        known = set(index.get("files", []))
        bogus = [
            f["path"] for f in output["impacted_files"] if f["change"] != "add" and f["path"] not in known
        ]
        gates = [
            GateResult(
                "impacted_files_exist",
                not bogus,
                f"files claimed to exist but not in repo: {bogus}" if bogus else "ok",
            )
        ]
        return NodeResult(
            outputs={"codebase_impact": output},
            gates=gates,
            summary=f"{len(output['impacted_files'])} impacted files, "
            f"{len(output['existing_defects'])} existing defects found",
        )


# ==================================================================== design
class ArchitectAgent(AgentHandler):
    name = "design"
    role = "architect"
    output_model = Design
    allow_commands = False

    def build_prompt(self, ctx: NodeContext) -> str:
        prompt = "## Approved requirements specification\n" + as_json(ctx.input("requirements_spec"))
        if ctx.input("codebase_impact"):
            prompt += "\n\n## Impact analysis (existing codebase — design the change)\n" + as_json(
                ctx.input("codebase_impact")
            )
            prompt += "\n\nExisting files:\n" + "\n".join(
                (ctx.input("codebase_index") or {}).get("files", [])
            )
        return prompt + "\n\nProduce the Design and submit it."

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        spec = ctx.input("requirements_spec") or {}
        gates = design_gates(output, spec, ctx.run.policy)
        deps = output.get("dependencies", [])
        bad_deps = [
            d for d in deps if re.split(r"[<>=!~\[ ]", d)[0].lower() not in ctx.run.policy.deps_allowed
        ]
        extra = []
        if bad_deps:
            extra.append(
                (
                    "change",
                    f"design requires non-allow-listed dependencies {bad_deps}",
                    {"dependencies": bad_deps},
                )
            )
        decisions = [
            Decision(
                ctx.spec.id,
                "agent:architect",
                f"{d['id']}: {d['title']}",
                f"{d['decision']} — {d['rationale']}",
                d.get("alternatives", []),
                {"requirements_spec": ctx.inputs["requirements_spec"].hash},
            )
            for d in output.get("decisions", [])
        ]
        wps = output["work_packages"]
        summary = (
            f"{len(output['api_contract'])} endpoints, {len(output['data_model'])} entities, "
            f"{len(wps)} work packages, {len(output.get('decisions', []))} ADRs"
        )
        approval_payload = {
            "api_contract": [f"{e['method']} {e['path']} — {e['summary']}" for e in output["api_contract"]],
            "work_packages": [
                {"id": w["id"], "title": w["title"], "files": w["files"], "depends_on": w["depends_on"]}
                for w in wps
            ],
            "decisions": [f"{d['id']} {d['title']}: {d['decision']}" for d in output.get("decisions", [])],
            "risks": output.get("risks", []),
        }
        return NodeResult(
            outputs={"design": output},
            summary=summary,
            gates=gates,
            decisions=decisions,
            approval_summary=f"Approve architecture & plan: {summary}",
            approval_payload=approval_payload,
            extra_approvals=extra,
        )


def design_gates(design: dict[str, Any], spec: dict[str, Any], policy) -> list[GateResult]:
    gates: list[GateResult] = []
    wps = design["work_packages"]
    ids = [w["id"] for w in wps]
    gates.append(GateResult("unique_work_packages", len(ids) == len(set(ids)), f"ids: {ids}"))
    unknown = sorted({d for w in wps for d in w["depends_on"] if d not in ids})
    gates.append(
        GateResult("dependencies_resolve", not unknown, f"unknown WP refs: {unknown}" if unknown else "ok")
    )
    # cycle check
    graph = {w["id"]: set(w["depends_on"]) for w in wps}
    visiting, done, cyclic = set(), set(), []

    def visit(n: str) -> None:
        if n in done or n not in graph:
            return
        if n in visiting:
            cyclic.append(n)
            return
        visiting.add(n)
        for m in graph[n]:
            visit(m)
        visiting.discard(n)
        done.add(n)

    for n in graph:
        visit(n)
    gates.append(
        GateResult("work_packages_acyclic", not cyclic, f"cycle through {cyclic}" if cyclic else "ok")
    )
    owners: dict[str, list[str]] = {}
    for w in wps:
        for f in w["files"]:
            owners.setdefault(f, []).append(w["id"])
    overlaps = {f: o for f, o in owners.items() if len(o) > 1}
    gates.append(
        GateResult(
            "disjoint_file_ownership",
            not overlaps,
            f"files owned by several packages: {overlaps}" if overlaps else "ok",
        )
    )
    globs = [f for f in owners if any(ch in f for ch in "*?[")]
    gates.append(
        GateResult("explicit_file_paths", not globs, f"globs not allowed: {globs}" if globs else "ok")
    )
    test_owned = [f for f in owners if f.startswith("tests/")]
    gates.append(
        GateResult(
            "tests_owned_by_test_agent",
            not test_owned,
            f"WPs may not own tests: {test_owned}" if test_owned else "ok",
        )
    )
    forbidden = [
        f
        for f in owners
        if glob_match(f, policy.change.get("forbidden_paths", [])) or f.startswith("/") or ".." in f
    ]
    gates.append(
        GateResult("paths_allowed", not forbidden, f"forbidden paths: {forbidden}" if forbidden else "ok")
    )
    covered = {fr for w in wps for fr in w.get("fr_ids", [])}
    missing = [fr for fr in _must_frs(spec) if fr not in covered]
    gates.append(
        GateResult(
            "must_requirements_traced",
            not missing,
            f"must-FRs not assigned to any work package: {missing}" if missing else "ok",
        )
    )
    return gates


# ============================================================ implementation
class ImplementerAgent(AgentHandler):
    name = "implement"
    role = "implementer"
    output_model = ImplementationReport

    def build_prompt(self, ctx: NodeContext) -> str:
        design = ctx.input("design") or {}
        wp_id = ctx.spec.params["wp"]
        wp = next(w for w in design["work_packages"] if w["id"] == wp_id)
        frs = [
            f for f in (ctx.input("requirements_spec") or {}).get("functional", []) if f["id"] in wp["fr_ids"]
        ]
        return (
            f"## Your work package: {wp_id} — {wp['title']}\n{as_json(wp)}\n\n"
            f"## Requirements this package satisfies\n{as_json(frs)}\n\n"
            f"## Approved design (shared by all packages)\n{as_json(design)}\n\n"
            f"Packages this one depends on are already merged: {wp['depends_on'] or 'none'}. "
            f"You own exactly these files: {wp['files']}. Implement the package and submit."
        )

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        gates = []
        wp_files = ctx.spec.write_scope
        missing = [f for f in wp_files if not (ctx.workdir / f).exists()]
        gates.append(
            GateResult(
                "owned_files_created",
                not missing,
                f"package files not created: {missing}" if missing else "ok",
            )
        )
        comp = compile_check(ctx.workdir)
        gates.append(GateResult("compiles", comp.ok, comp.output[-3000:] if not comp.ok else "ok"))
        return NodeResult(
            outputs={f"impl_report:{ctx.spec.params['wp']}": output},
            gates=gates,
            summary=output["summary"][:500],
        )


class RepairAgent(ImplementerAgent):
    name = "repair"
    role = "repair"

    def build_prompt(self, ctx: NodeContext) -> str:
        report = ctx.run.value("verification_report") or {}
        security = ctx.run.value("security_review") or {}
        prompt = (
            f"## Gate failure that triggered this repair (from {ctx.spec.params.get('trigger')})\n"
            f"{ctx.spec.params.get('findings', '')}\n\n## Latest verification report\n{as_json(report)}\n"
        )
        high = [f for f in security.get("findings", []) if f["severity"] in ("high", "critical")]
        if high and str(ctx.spec.params.get("trigger", "")).startswith("security"):
            prompt += f"\n## Security findings to remediate\n{as_json(high)}\n"
        prompt += (
            f"\n## Approved design\n{as_json(ctx.input('design'))}\n\n"
            f"## Requirements\n{as_json(ctx.input('requirements_spec'))}\n\n"
            f"Your write scope: {ctx.spec.write_scope}. Fix the root causes and submit."
        )
        return prompt

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        comp = compile_check(ctx.workdir)
        gates = [
            GateResult("compiles", comp.ok, comp.output[-3000:] if not comp.ok else "ok"),
            GateResult(
                "changed_something",
                bool(outcome.files_written),
                "no files modified" if not outcome.files_written else f"{len(outcome.files_written)} files",
            ),
        ]
        return NodeResult(
            outputs={f"repair_report_{ctx.spec.params['cycle']}": output},
            gates=gates,
            summary=output["summary"][:500],
        )


# =================================================================== testing
FR_RX = re.compile(r"\b(FR-\d+)\b")


class TestAuthorAgent(AgentHandler):
    __test__ = False  # not a pytest test class
    name = "write_tests"
    role = "test_author"
    output_model = TestSuiteReport

    def build_prompt(self, ctx: NodeContext) -> str:
        return (
            f"## Requirements\n{as_json(ctx.input('requirements_spec'))}\n\n"
            f"## Design (API contract, data model)\n{as_json(ctx.input('design'))}\n\n"
            "The implementation is merged in this workspace. Write the test suite under tests/, run it, "
            "and submit a TestSuiteReport."
        )

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        spec = ctx.input("requirements_spec") or {}
        tests_dir = ctx.workdir / "tests"
        text = (
            "\n".join(p.read_text(errors="replace") for p in tests_dir.rglob("*.py"))
            if tests_dir.exists()
            else ""
        )
        referenced = set(FR_RX.findall(text))
        untested = [fr for fr in _must_frs(spec) if fr not in referenced]
        gates = [
            GateResult(
                "every_must_fr_has_tests",
                not untested,
                f"no test references: {untested}" if untested else "ok",
            )
        ]
        result = run_pytest(ctx.workdir)
        gates.append(GateResult("suite_runs", result.total > 0, f"collected {result.total} tests"))
        if result.failed or result.errors:
            reported = bool(output.get("defects_found"))
            gates.append(
                GateResult(
                    "failures_explained",
                    reported,
                    f"{result.failed} failed / {result.errors} errors with defects reported "
                    "-> repair loop will fix"
                    if reported
                    else f"{result.failed} failed / {result.errors} errors but no product defect "
                    f"reported; fix the tests or report the defect.\n{result.output[-3000:]}",
                    severity="block" if not reported else "warn",
                )
            )
        return NodeResult(
            outputs={"test_report": {**output, "pytest": result.to_dict()}},
            gates=gates,
            summary=f"{result.passed} passed, {result.failed} failed, coverage {result.coverage}%",
        )


# ================================================================== security
class SecurityReviewAgent(AgentHandler):
    name = "security_review"
    role = "security_reviewer"
    output_model = SecurityReview

    def build_prompt(self, ctx: NodeContext) -> str:
        return (
            f"## Design\n{as_json(ctx.input('design'))}\n\n"
            f"## Latest verification report (includes deterministic policy scan)\n"
            f"{as_json(ctx.input('verification_report'))}\n\n"
            "Review the code in this workspace (read-only) and submit a SecurityReview."
        )

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        high = [f for f in output["findings"] if f["severity"] in ("high", "critical")]
        limit = int(ctx.run.policy.release.get("max_open_high_findings", 0))
        listing = "; ".join(f"{f['id']} [{f['severity']}] {f['title']} @ {f['location']}" for f in high)
        result = NodeResult(
            outputs={"security_review": output},
            summary=f"{len(output['findings'])} findings, {len(high)} high/critical",
        )
        if len(high) <= limit:
            result.gates = [GateResult("no_open_high_findings", True, "ok")]
            return result
        # Blocking findings are a risk decision, not an automatic fix: a human either accepts
        # the risk (recorded waiver, honoured by the release gate) or rejects -> repair loop.
        result.gates = [GateResult("no_open_high_findings", True, f"needs risk decision: {listing}", "warn")]
        result.extra_approvals.append(
            (
                "policy_exception",
                f"Accept {len(high)} high/critical security finding(s) as a documented risk? Approve = waive "
                "(recorded in release notes); Reject with a comment = send to the repair loop",
                {"findings": high, "options": ["approve: accept risk", "reject: repair"]},
            )
        )
        return result


# ===================================================================== docs
class DocsAgent(AgentHandler):
    name = "docs"
    role = "docs_writer"
    output_model = DocsReport

    def build_prompt(self, ctx: NodeContext) -> str:
        return (
            f"## Requirements\n{as_json(ctx.input('requirements_spec'))}\n\n"
            f"## Design\n{as_json(ctx.input('design'))}\n\n"
            f"Release version: {ctx.run.config.version}. Read the code, write the docs, and submit."
        )

    def evaluate(self, ctx: NodeContext, output: dict[str, Any], outcome: AgentOutcome) -> NodeResult:
        gates = []
        required = ctx.run.policy.release.get("require_docs", ["README.md"]) + ["docs/API.md"]
        missing = [f for f in required if not (ctx.workdir / f).exists()]
        gates.append(
            GateResult("required_docs_exist", not missing, f"missing: {missing}" if missing else "ok")
        )
        corpus = "".join(
            (ctx.workdir / f).read_text(errors="replace") for f in required if (ctx.workdir / f).exists()
        )
        design = ctx.input("design") or {}
        undocumented = [
            f"{e['method']} {e['path']}" for e in design.get("api_contract", []) if e["path"] not in corpus
        ]
        gates.append(
            GateResult(
                "all_endpoints_documented",
                not undocumented,
                f"undocumented endpoints: {undocumented}" if undocumented else "ok",
            )
        )
        return NodeResult(outputs={"docs_report": output}, gates=gates, summary=output["summary"][:300])


for _h in (
    RequirementsAgent(),
    ImpactAnalyst(),
    ArchitectAgent(),
    ImplementerAgent(),
    RepairAgent(),
    TestAuthorAgent(),
    SecurityReviewAgent(),
    DocsAgent(),
):
    register(_h)
