"""End-to-end: re-planning on requirement change, safe-stop/resume, brownfield, budget stop."""

from __future__ import annotations

import threading
import time

from sdlc.domain.models import ApprovalStatus, NodeStatus, RunStatus
from sdlc.engine.bootstrap import create_run, execute, load_context, open_store, runs_dir
from sdlc.engine.context import RunConfig
from tests.conftest import HumanBot
from tests.fake_agents import build_provider

GREEN = {
    "name": "greenfield",
    "mode": "greenfield",
    "requirement": "Build a URL shortener.",
    "version": "1.0.0",
}


def _ctx(store, scenario, provider, approve_mode="auto"):
    run_id = create_run(
        store, scenario, RunConfig(model="fake", fallback_model="fake", approve_mode=approve_mode)
    )
    ctx = load_context(store, run_id, provider=provider)
    ctx.approvals.poll_s = 0.05
    return run_id, ctx


def test_amendment_mid_run_replans_and_rolls_back_workspace(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(store, GREEN, build_provider())
    fired = threading.Event()

    def amend_after_verify(event):
        if event["type"] == "node.succeeded" and event["node_id"] == "verify-2" and not fired.is_set():
            fired.set()
            store.add_amendment(run_id, "Links must expire automatically when the campaign ends.", "human:po")

    ctx.audit.subscribe(amend_after_verify)
    with HumanBot(store, ctx.policy):
        status = execute(ctx)
    assert status == RunStatus.SUCCEEDED, store.get_run(run_id)["error"]
    assert fired.is_set()

    types = [e["type"] for e in store.events(run_id)]
    assert "amendment.applied" in types and "replan.workspace_reset" in types
    states = store.load_node_states(run_id)
    # the amendment added FR-4 -> design added WP-5 -> planner regenerated the implementation graph
    assert states["impl:WP-5"].status == NodeStatus.SUCCEEDED
    repo = runs_dir() / run_id / "repo"
    assert (repo / "app" / "expiry.py").exists() and (repo / "tests" / "test_expiry.py").exists()
    spec = ctx.value("requirements_spec")
    assert any(f["id"] == "FR-4" for f in spec["functional"])
    # the amendment landed before release: exactly one release, cut from the re-planned code
    release = ctx.value("release")
    assert release["tag"] == "v1.0.0"
    assert release["commit"] == ctx.workspace.head()
    # design changed, so it needed a fresh approval bound to the new content
    design_approvals = [a for a in store.list_approvals(run_id) if a.kind.value == "design"]
    assert len({a.subject_hash for a in design_approvals}) == 2


def test_safe_stop_then_resume(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(
        store,
        {**GREEN, "name": "stoppable"},
        build_provider(inject_faults=False, ambiguous=False),
        approve_mode="dashboard",
    )

    def stop_at_design():
        while True:
            pending = store.list_approvals(run_id, ApprovalStatus.PENDING)
            if any(a.kind.value == "design" for a in pending):
                store.request_stop(run_id, "operator: pausing for architecture review board")
                return
            time.sleep(0.05)

    t = threading.Thread(target=stop_at_design, daemon=True)
    t.start()
    assert execute(ctx) == RunStatus.STOPPED
    states = store.load_node_states(run_id)
    assert states["design"].status == NodeStatus.PENDING  # parked, resumable
    assert store.get_run(run_id)["status"] == "stopped"

    ctx2 = load_context(store, run_id, provider=build_provider(inject_faults=False, ambiguous=False))
    ctx2.approvals.poll_s = 0.05
    with HumanBot(store, ctx2.policy):
        assert execute(ctx2) == RunStatus.SUCCEEDED
    types = [e["type"] for e in store.events(run_id)]
    assert types.count("run.started") == 2 and "run.stopped" in types


def test_brownfield_on_greenfield_output(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(store, GREEN, build_provider(inject_faults=False, ambiguous=False))
    with HumanBot(store, ctx.policy):
        assert execute(ctx) == RunStatus.SUCCEEDED

    brown = {
        "name": "brownfield",
        "mode": "brownfield",
        "seed": "latest:greenfield",
        "version": "1.1.0",
        "requirement": "Add link expiry to the existing service.",
    }
    bid, bctx = _ctx(store, brown, build_provider(inject_faults=False, ambiguous=False))
    with HumanBot(store, bctx.policy):
        assert execute(bctx) == RunStatus.SUCCEEDED, store.get_run(bid)["error"]
    index = bctx.value("codebase_index")
    assert "app/api.py" in index["files"] and index["baseline_tests"]["passed"] >= 4
    assert bctx.value("codebase_impact")["impacted_files"][0]["path"] == "app/api.py"
    assert bctx.value("release")["tag"] == "v1.1.0"


def test_budget_exhaustion_safe_stops(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(store, {**GREEN, "name": "budget"}, build_provider())
    ctx.policy.budgets["max_run_cost_usd"] = 0.05  # fake turns cost $0.01 each
    with HumanBot(store, ctx.policy):
        status = execute(ctx)
    assert status == RunStatus.STOPPED
    assert "budget" in store.get_run(run_id)["error"]


def test_high_security_finding_waived_by_human(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(
        store,
        {**GREEN, "name": "waiver"},
        build_provider(inject_faults=False, ambiguous=False, security_high=True),
    )
    with HumanBot(store, ctx.policy) as bot:
        assert execute(ctx) == RunStatus.SUCCEEDED, store.get_run(run_id)["error"]
    assert ("policy_exception", "approved") in bot.decided
    states = store.load_node_states(run_id)
    assert not [n for n in states if n.startswith("security_review-")]  # risk accepted: no security repair
    gate = {g["name"]: g for g in ctx.value("release_readiness")["checklist"]}
    assert gate["security_clear"]["passed"] and "risk accepted" in gate["security_clear"]["detail"]


def test_high_security_finding_rejected_goes_to_repair(sdlc_home):
    store = open_store()
    run_id, ctx = _ctx(
        store,
        {**GREEN, "name": "sec-repair"},
        build_provider(inject_faults=False, ambiguous=False, security_high=True),
    )
    with HumanBot(store, ctx.policy, reject_first={"policy_exception"}) as bot:
        assert execute(ctx) == RunStatus.SUCCEEDED, store.get_run(run_id)["error"]
    assert ("policy_exception", "rejected") in bot.decided
    states = store.load_node_states(run_id)
    assert states["security_review"].status == NodeStatus.SKIPPED  # superseded
    reruns = [n for n in states if n.startswith("security_review-")]
    assert reruns
    assert all(states[n].status == NodeStatus.SUCCEEDED for n in reruns)
    triggers = [
        n["params"].get("trigger") for n in store.get_run(run_id)["graph"] if n["handler"] == "repair"
    ]
    assert "security_review" in triggers  # the rejected waiver triggered a repair loop
