"""Deterministic (non-LLM) node handlers: intake, human clarification, codebase scan,
planner, integration barrier, verification, release gate and publish."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sdlc.checks.codebase import index_codebase
from sdlc.checks.runner import compile_check, dump_routes, lint_check, normalise_path, run_py, run_pytest
from sdlc.domain.models import (
    ApprovalKind,
    ApprovalStatus,
    Decision,
    NodeKind,
    NodeSpec,
    NodeStatus,
    RetryPolicy,
    Risk,
    Stage,
    content_hash,
)
from sdlc.engine.context import GateResult, Handler, NodeContext, NodeResult, register


# ===================================================================== intake
class Intake(Handler):
    name = "intake"

    async def run(self, ctx: NodeContext) -> NodeResult:
        brief = ctx.run.value("requirement")
        if brief is None:
            meta = ctx.run.store.get_run(ctx.run.run_id) or {}
            brief = {
                "text": meta.get("requirement", ""),
                "scenario": ctx.run.scenario,
                "amendments": [],
                "clarifications": {},
                "clarification_rounds": 0,
            }
        gates = [
            GateResult("requirement_not_empty", bool(brief.get("text", "").strip()), "empty requirement")
        ]
        return NodeResult(
            outputs={"requirement": brief},
            gates=gates,
            summary=f"requirement captured ({len(brief['text'])} chars)",
        )


# ============================================================ clarification
class Clarify(Handler):
    """Human checkpoint: blocking ambiguities are put to the product owner.

    Answers are folded into the requirement brief and the requirements node is invalidated,
    so the specification is regenerated from the answers (a non-linear loop, bounded to two
    rounds before proceeding on the analyst's defaults)."""

    name = "clarify"

    async def run(self, ctx: NodeContext) -> NodeResult:
        spec = ctx.input("requirements_spec") or {}
        brief = dict(ctx.run.value("requirement") or {})
        answered = brief.get("clarifications", {}) or {}
        ambiguities = spec.get("ambiguities", [])
        blocking = [
            q for q in ambiguities if q["blocking"] and not q.get("resolved_by") and q["id"] not in answered
        ]
        defaults = [
            q
            for q in ambiguities
            if q not in blocking and not q.get("resolved_by") and q["id"] not in answered
        ]
        decisions = [
            Decision(
                ctx.spec.id,
                "agent:requirements",
                f"{q['id']} assumed default: {q['default']}",
                q["why"],
                q.get("options", []),
            )
            for q in defaults
        ]
        log = {
            "asked": [],
            "defaults_assumed": [{"id": q["id"], "default": q["default"]} for q in defaults],
            "answered_before": answered,
        }
        if not blocking:
            return NodeResult(
                outputs={"clarification_log": log},
                decisions=decisions,
                summary=f"no blocking ambiguities; {len(defaults)} defaults assumed",
            )
        rounds = int(brief.get("clarification_rounds", 0))
        if rounds >= 2:
            decisions += [
                Decision(
                    ctx.spec.id,
                    "policy:engine",
                    f"{q['id']} proceeding on default after 2 rounds",
                    q["default"],
                )
                for q in blocking
            ]
            return NodeResult(
                outputs={"clarification_log": log},
                decisions=decisions,
                gates=[GateResult("clarification_rounds", True, "proceeded on defaults", "warn")],
                summary="clarification round limit reached; proceeding on defaults",
            )
        questions = [
            {
                "id": q["id"],
                "question": q["question"],
                "why": q["why"],
                "options": q.get("options", []),
                "default": q["default"],
            }
            for q in blocking
        ]
        ctx.state.status = NodeStatus.AWAITING_APPROVAL
        ctx.run.store.save_node_state(ctx.run.run_id, ctx.state)
        approval = ctx.run.approvals.request(
            ctx.run.run_id,
            ctx.spec.id,
            ApprovalKind.CLARIFICATION,
            f"{len(questions)} blocking question(s) must be answered before design",
            {"questions": questions, "requirement": brief.get("text")},
            content_hash(questions),
        )
        approval = await ctx.run.approvals.wait(approval, ctx.run.stop_reason)
        ctx.state.status = NodeStatus.RUNNING
        ctx.run.store.save_node_state(ctx.run.run_id, ctx.state)
        provided = (
            (approval.response or {}).get("answers", {}) if approval.status == ApprovalStatus.APPROVED else {}
        )
        actor = approval.decided_by or "human"
        new = {}
        for q in questions:
            answer = provided.get(q["id"]) or q["default"]
            new[q["id"]] = {"question": q["question"], "answer": answer, "by": actor}
            decisions.append(
                Decision(ctx.spec.id, actor, f"{q['id']} answered: {answer}", q["why"], q.get("options", []))
            )
        brief["clarifications"] = {**answered, **new}
        brief["clarification_rounds"] = rounds + 1
        ctx.run.put("requirement", brief, ctx.spec.id)
        log["asked"] = [{"id": k, **v} for k, v in new.items()]
        return NodeResult(
            outputs={"clarification_log": log},
            decisions=decisions,
            invalidate=[
                n
                for n in ctx.run.graph.consumers_of("requirement")
                if n != ctx.spec.id and ctx.run.graph.get(n).handler == "requirements"
            ],
            summary=f"{len(new)} answers recorded; requirements will be regenerated",
        )


# ============================================================ codebase scan
class CodebaseScan(Handler):
    name = "codebase_scan"

    async def run(self, ctx: NodeContext) -> NodeResult:
        root = ctx.workdir
        index = await asyncio.to_thread(index_codebase, root)
        baseline = None
        if index["tests"]:
            res = await asyncio.to_thread(run_pytest, root)
            baseline = {
                "passed": res.passed,
                "failed": res.failed,
                "errors": res.errors,
                "coverage": res.coverage,
            }
        index["baseline_tests"] = baseline
        gates = [GateResult("codebase_present", bool(index["files"]), f"{len(index['files'])} files")]
        if baseline and (baseline["failed"] or baseline["errors"]):
            gates.append(
                GateResult(
                    "baseline_green",
                    True,
                    f"pre-existing failures: {baseline['failed']} failed / {baseline['errors']} "
                    "errors (recorded so they are not blamed on this change)",
                    "warn",
                )
            )
        return NodeResult(
            outputs={"codebase_index": index},
            gates=gates,
            summary=f"{len(index['files'])} files, {index['python_loc']} py LOC, "
            f"{len(index['routes'])} routes, {len(index['orm_models'])} ORM models",
        )


# ================================================================== planner
class Planner(Handler):
    """Expands the approved design into implementation nodes (runtime graph expansion)."""

    name = "plan"

    async def run(self, ctx: NodeContext) -> NodeResult:
        design = ctx.input("design") or {}
        wps = design.get("work_packages", [])
        nodes = []
        for wp in wps:
            nodes.append(
                NodeSpec(
                    id=f"impl:{wp['id']}",
                    stage=Stage.IMPLEMENTATION,
                    kind=NodeKind.AGENT,
                    handler="implement",
                    title=wp["title"],
                    deps=[ctx.spec.id] + [f"impl:{d}" for d in wp["depends_on"]],
                    inputs=["requirements_spec", "design"],
                    outputs=[f"impl_report:{wp['id']}"],
                    write_scope=list(wp["files"]),
                    risk=Risk(wp.get("risk", "low")),
                    approval=ApprovalKind.CHANGE if wp.get("risk") == "high" else None,
                    retry=RetryPolicy(max_attempts=3, escalate_model_at=3),
                    params={"wp": wp["id"], "feeds": ctx.spec.params.get("feeds", [])},
                )
            )
        waves = _waves({w["id"]: w["depends_on"] for w in wps})
        plan = {
            "work_packages": [w["id"] for w in wps],
            "waves": waves,
            "max_parallelism": max((len(w) for w in waves), default=0),
        }
        decision = Decision(
            ctx.spec.id,
            "agent:planner",
            f"Execution plan: {len(wps)} packages in {len(waves)} waves",
            f"waves {waves}; packages in the same wave own disjoint files and run in parallel",
            based_on={"design": ctx.inputs["design"].hash},
        )
        return NodeResult(
            outputs={"plan": plan},
            new_nodes=nodes,
            decisions=[decision],
            summary=f"{len(nodes)} implementation nodes, waves: {waves}",
        )


def _waves(deps: dict[str, list[str]]) -> list[list[str]]:
    level: dict[str, int] = {}

    def depth(n: str, seen: frozenset = frozenset()) -> int:
        if n in level:
            return level[n]
        if n in seen:
            return 0
        level[n] = 1 + max((depth(d, seen | {n}) for d in deps.get(n, []) if d in deps), default=-1)
        return level[n]

    for n in deps:
        depth(n)
    waves: dict[int, list[str]] = {}
    for n, lv in level.items():
        waves.setdefault(lv, []).append(n)
    return [sorted(waves[k]) for k in sorted(waves)]


# ==================================================================== join
class Integrate(Handler):
    """Synchronisation barrier for the parallel implementation branch + integration smoke check."""

    name = "integrate"

    async def run(self, ctx: NodeContext) -> NodeResult:
        root = ctx.workdir
        comp = await asyncio.to_thread(compile_check, root)
        smoke = await asyncio.to_thread(run_py, root, ["-c", "import app.main"], 120)
        merged = [n for n in ctx.spec.deps if n.startswith("impl:")]
        gates = [
            GateResult("compiles", comp.ok, comp.output[-2000:] or "ok"),
            GateResult("app_imports", smoke.ok, smoke.output[-2000:] or "ok", severity="warn"),
        ]
        return NodeResult(
            outputs={
                "integration": {"merged": merged, "imports": smoke.ok, "head": ctx.run.workspace.head()}
            },
            gates=gates,
            summary=f"joined {len(merged)} packages; app imports: {smoke.ok}",
        )


# ================================================================== verify
class Verify(Handler):
    """Deterministic quality gate: compile, lint, tests, coverage, API contract, FR traceability,
    full-tree policy scan. Repairable: failure spawns a repair loop rather than a blind retry."""

    name = "verify"

    async def run(self, ctx: NodeContext) -> NodeResult:
        return await asyncio.to_thread(self._verify, ctx)

    def _verify(self, ctx: NodeContext) -> NodeResult:
        root = ctx.workdir
        policy = ctx.run.policy
        design = ctx.run.value("design") or {}
        spec = ctx.run.value("requirements_spec") or {}
        gates: list[GateResult] = []

        comp = compile_check(root)
        gates.append(GateResult("compile", comp.ok, comp.output[-2000:] or "ok"))
        lint = lint_check(root)
        gates.append(GateResult("lint_correctness", lint.ok, lint.output[-3000:] or "ok"))

        tests = run_pytest(root)
        gates.append(
            GateResult(
                "tests_pass",
                tests.total > 0 and tests.failed == 0 and tests.errors == 0,
                f"{tests.passed} passed, {tests.failed} failed, {tests.errors} errors"
                + (f"\n{tests.output[-6000:]}" if (tests.failed or tests.errors) else ""),
            )
        )
        min_cov = float(policy.release.get("min_coverage", 80))
        cov_ok = tests.coverage is not None and tests.coverage >= min_cov
        gates.append(GateResult("coverage", cov_ok, f"{tests.coverage}% (minimum {min_cov}%)"))

        routes, err = dump_routes(root)
        contract: dict[str, Any] = {"missing": [], "undocumented": []}
        if routes is None:
            gates.append(GateResult("api_contract", False, f"could not import app.main:app\n{err}"))
        else:
            have = {(m, normalise_path(p)) for p, ms in routes.items() for m in ms}
            want = {(e["method"], normalise_path(e["path"])) for e in design.get("api_contract", [])}
            contract["missing"] = sorted(f"{m} {p}" for m, p in want - have)
            ignore = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
            contract["undocumented"] = sorted(f"{m} {p}" for m, p in have - want if p not in ignore)
            gates.append(
                GateResult(
                    "api_contract",
                    not contract["missing"],
                    f"endpoints in approved contract but not implemented: {contract['missing']}"
                    if contract["missing"]
                    else f"{len(want)} contract endpoints implemented",
                )
            )
            if contract["undocumented"]:
                gates.append(
                    GateResult(
                        "api_contract_extra",
                        True,
                        f"implemented but not in contract: {contract['undocumented']}",
                        "warn",
                    )
                )

        test_text = (
            "\n".join(p.read_text(errors="replace") for p in (root / "tests").rglob("*.py"))
            if (root / "tests").exists()
            else ""
        )
        must = [f["id"] for f in spec.get("functional", []) if f.get("priority", "must") == "must"]
        matrix = {fr: (fr in test_text) for fr in must}
        untraced = [fr for fr, ok in matrix.items() if not ok]
        gates.append(
            GateResult(
                "requirements_traceability",
                not untraced,
                f"must-FRs with no test: {untraced}" if untraced else f"{len(must)} must-FRs traced",
            )
        )

        files = {}
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if (
                p.is_file()
                and (p.suffix == ".py" or p.name == "requirements.txt")
                and not any(x in rel for x in (".git/", "__pycache__"))
            ):
                files[rel] = p.read_text(errors="replace")
        scan = policy.scan_changes(files, 0, resolve=files.get)
        gates.append(
            GateResult(
                "policy_scan_full_tree",
                not scan.blocking,
                "; ".join(f"{f.rule} {f.path}:{f.line}" for f in scan.blocking)
                or f"{len(scan.findings)} non-blocking findings",
            )
        )

        report = {
            "head": ctx.run.workspace.head(),
            "gates": [g.to_dict() for g in gates],
            "passed": all(g.passed for g in gates if g.severity == "block"),
            "tests": tests.to_dict(),
            "coverage": tests.coverage,
            "contract": contract,
            "traceability": matrix,
            "policy_findings": [f.to_dict() for f in scan.findings],
        }
        return NodeResult(
            outputs={"verification_report": report},
            gates=gates,
            summary=f"tests {tests.passed}/{tests.total} passing, coverage {tests.coverage}%, "
            f"contract missing {len(contract['missing'])}, untraced {len(untraced)}",
        )


# ============================================================ release gate
class ReleaseGate(Handler):
    """Release-readiness checklist + release notes; a human must approve the release."""

    name = "release_gate"

    async def run(self, ctx: NodeContext) -> NodeResult:
        run = ctx.run
        verification = run.value("verification_report") or {}
        security = run.value("security_review") or {}
        design_art = run.artifact("design")
        approvals = run.store.list_approvals(run.run_id)
        design_approvals = [
            a for a in approvals if a.kind == ApprovalKind.DESIGN and a.status == ApprovalStatus.APPROVED
        ]
        design_ok = bool(design_approvals)
        design_by = design_approvals[-1].decided_by if design_approvals else "nobody"
        high = [f for f in security.get("findings", []) if f["severity"] in ("high", "critical")]
        waived = [
            a
            for a in approvals
            if a.kind == ApprovalKind.POLICY_EXCEPTION and a.status == ApprovalStatus.APPROVED
        ]
        docs = run.value("docs_report")
        sec_art = run.artifact("security_review")
        # A waiver only covers the review it was given on: it must come from the node that
        # produced the *current* security review artifact.
        security_waived = bool(sec_art) and any(a.node_id == sec_art.producer for a in waived)
        min_cov = float(run.policy.release.get("min_coverage", 80))
        checklist = [
            GateResult(
                "verification_passed", bool(verification.get("passed")), f"head {verification.get('head')}"
            ),
            GateResult(
                "coverage_threshold",
                (verification.get("coverage") or 0) >= min_cov,
                f"{verification.get('coverage')}% >= {min_cov}%",
            ),
            GateResult("design_approved", design_ok, f"architecture approved by {design_by}"),
            GateResult(
                "security_clear",
                len(high) <= int(run.policy.release.get("max_open_high_findings", 0)) or security_waived,
                f"{len(high)} high/critical findings"
                + (" (risk accepted by a human)" if security_waived else ""),
            ),
            GateResult("documentation", docs is not None, "docs agent output present"),
            GateResult(
                "workspace_head_verified",
                verification.get("head") == run.workspace.head(),
                "nothing merged after the last verification",
            ),
        ]
        notes = release_notes(run, verification, security, waived)
        readiness = {
            "version": run.config.version,
            "checklist": [g.to_dict() for g in checklist],
            "release_notes": notes,
            "head": run.workspace.head(),
            "design_hash": design_art.hash if design_art else None,
        }
        return NodeResult(
            outputs={"release_readiness": readiness},
            gates=checklist,
            approval_summary=f"Release v{run.config.version} — all readiness checks passed",
            approval_payload={"checklist": readiness["checklist"], "release_notes": notes},
            summary="ready for release" if all(g.passed for g in checklist) else "not ready",
        )


def _high(security: dict) -> int:
    return sum(1 for f in security.get("findings", []) if f["severity"] in ("high", "critical"))


def release_notes(run, verification: dict, security: dict, waived: list) -> str:
    spec = run.value("requirements_spec") or {}
    design = run.value("design") or {}
    lines = [
        f"# Release v{run.config.version} — {spec.get('title', 'service')}",
        "",
        spec.get("problem_statement", ""),
        "",
        "## Delivered requirements",
    ]
    for fr in spec.get("functional", []):
        traced = (verification.get("traceability") or {}).get(fr["id"])
        lines.append(f"- **{fr['id']}** {fr['title']} — {'verified by tests' if traced else 'not traced'}")
    lines += ["", "## API"] + [
        f"- `{e['method']} {e['path']}` — {e['summary']}" for e in design.get("api_contract", [])
    ]
    tests = verification.get("tests") or {}
    lines += [
        "",
        "## Quality",
        f"- Tests: {tests.get('passed')} passed, {tests.get('failed')} failed",
        f"- Coverage: {verification.get('coverage')}%",
        f"- Security findings: {len(security.get('findings', []))} ({_high(security)} high/critical)",
    ]
    if waived:
        lines += ["", "## Accepted policy exceptions"] + [
            f"- {a.summary} (by {a.decided_by}: {a.comment})" for a in waived
        ]
    lines += ["", "## Architecture decisions"] + [
        f"- {d['id']} {d['title']}: {d['decision']}" for d in design.get("decisions", [])
    ]
    lines += ["", "## Known risks"] + [
        f"- {r['description']} — mitigation: {r['mitigation']}" for r in design.get("risks", [])
    ]
    return "\n".join(lines) + "\n"


# ================================================================= publish
class Publish(Handler):
    """Tag the approved commit and produce an immutable release bundle with a manifest."""

    name = "publish"

    async def run(self, ctx: NodeContext) -> NodeResult:
        run = ctx.run
        readiness = ctx.input("release_readiness") or {}
        head = run.workspace.head()
        gates = [
            GateResult(
                "approved_commit_unchanged",
                readiness.get("head") == head,
                f"approved {readiness.get('head')} vs head {head}",
            )
        ]
        if not gates[0].passed:
            return NodeResult(gates=gates, summary="head moved after release approval")
        tags = subprocess.run(
            ["git", "tag", "-l"], cwd=run.workspace.root, capture_output=True, text=True
        ).stdout
        tag = f"v{run.config.version}"
        n = 1
        while tag in tags.split():
            n += 1
            tag = f"v{run.config.version}-r{n}"
        out_dir = run.run_dir / "release" / tag
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            run.workspace.tag(tag, f"release {tag} approved via orchestrator run {run.run_id}")
            bundle = out_dir / f"service-{tag}.tar.gz"
            subprocess.run(
                ["git", "archive", "--format=tar.gz", "-o", str(bundle), tag],
                cwd=run.workspace.root,
                check=True,
                capture_output=True,
            )
            digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
            (out_dir / "RELEASE_NOTES.md").write_text(readiness.get("release_notes", ""))
            reqs = run.workspace.root / "requirements.txt"
            approvals = [
                {
                    "kind": a.kind.value,
                    "status": a.status.value,
                    "by": a.decided_by,
                    "node": a.node_id,
                    "comment": a.comment,
                }
                for a in run.store.list_approvals(run.run_id)
            ]
            manifest = {
                "run_id": run.run_id,
                "tag": tag,
                "commit": head,
                "bundle": bundle.name,
                "sha256": digest,
                "sbom": reqs.read_text().split() if reqs.exists() else [],
                "approvals": approvals,
                "checklist": readiness.get("checklist", []),
            }
            (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        except Exception:
            subprocess.run(["git", "tag", "-d", tag], cwd=run.workspace.root, capture_output=True)
            shutil.rmtree(out_dir, ignore_errors=True)
            raise
        return NodeResult(
            outputs={
                "release": {
                    "tag": tag,
                    "commit": head,
                    "bundle": str(bundle),
                    "sha256": digest,
                    "dir": str(out_dir),
                }
            },
            gates=gates,
            summary=f"released {tag} @ {head[:10]} -> {Path(bundle).name}",
        )


for _h in (Intake(), Clarify(), CodebaseScan(), Planner(), Integrate(), Verify(), ReleaseGate(), Publish()):
    register(_h)
