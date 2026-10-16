"""End-to-end: the real engine drives scripted agents through the full greenfield lifecycle."""

from __future__ import annotations

import json

from sdlc.domain.models import NodeStatus, RunStatus
from sdlc.engine.bootstrap import create_run, execute, load_context, open_store
from sdlc.engine.context import RunConfig
from sdlc.governance.audit import AuditLog
from sdlc.observability.metrics import run_metrics
from tests.conftest import HumanBot
from tests.fake_agents import build_provider

SCENARIO = {
    "name": "greenfield-test",
    "mode": "greenfield",
    "requirement": "Build a URL shortener.",
    "version": "0.1.0",
}


def _run(provider, approve_mode="auto", reject_first=None):
    store = open_store()
    run_id = create_run(
        store,
        SCENARIO,
        RunConfig(model="fake", fallback_model="fake-strong", approve_mode=approve_mode, max_parallel=3),
    )
    ctx = load_context(store, run_id, provider=provider)
    ctx.approvals.poll_s = 0.05
    with HumanBot(store, ctx.policy, reject_first) as bot:
        status = execute(ctx)
    return store, run_id, ctx, status, bot


def test_greenfield_full_lifecycle(sdlc_home):
    store, run_id, ctx, status, bot = _run(build_provider())
    assert status == RunStatus.SUCCEEDED, store.get_run(run_id)["error"]

    states = store.load_node_states(run_id)
    # runtime graph expansion: planner created one node per work package
    assert {f"impl:WP-{i}" for i in range(1, 5)} <= set(states)
    # repair loop was inserted after verify caught the product defect, and the original was superseded
    assert states["verify"].status == NodeStatus.SKIPPED
    assert states["repair-1"].status == NodeStatus.SUCCEEDED
    assert states["verify-2"].status == NodeStatus.SUCCEEDED
    # WP-2's broken first attempt was rolled back and retried
    assert states["impl:WP-2"].attempts == 2
    # clarification loop: requirements ran twice (answers folded in)
    req_events = store.events(run_id, types=["node.succeeded"])
    assert sum(1 for e in req_events if e["node_id"] == "requirements") == 2

    m = run_metrics(store, run_id)
    assert m["retries"] >= 1 and m["rollbacks"] >= 1 and m["repair_loops"] == 1
    assert m["mttr_s"] is not None and m["replans"] >= 1
    assert m["approvals_requested"] >= 3  # clarification, design, requirements.txt change, release...

    # release was human-approved (never auto) and produced a bundle + manifest
    assert ("release", "approved") in bot.decided
    release = ctx.value("release")
    manifest = json.loads(open(f"{release['dir']}/manifest.json").read())
    assert manifest["commit"] == release["commit"] and manifest["sha256"] == release["sha256"]

    # audit trail is intact and tamper-evident
    ok, msg = AuditLog(store).verify(run_id)
    assert ok, msg
    store._conn.execute("UPDATE events SET data='{}' WHERE run_id=? AND seq=3", (run_id,))
    store._conn.commit()
    ok, _ = AuditLog(store).verify(run_id)
    assert not ok


def test_design_rejection_feeds_back_to_architect(sdlc_home):
    store, run_id, ctx, status, bot = _run(
        build_provider(inject_faults=False, ambiguous=False),
        approve_mode="dashboard",
        reject_first={"design"},
    )
    assert status == RunStatus.SUCCEEDED, store.get_run(run_id)["error"]
    assert ("design", "rejected") in bot.decided
    failed = store.events(run_id, types=["node.attempt_failed"])
    assert any(e["node_id"] == "design" and e["data"]["kind"] == "rejected" for e in failed)
    assert store.load_node_states(run_id)["design"].attempts == 2
