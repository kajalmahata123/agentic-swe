"""Snapshot -> restore round-trip: recorded runs stay fully inspectable on another machine."""

from __future__ import annotations

import subprocess

from sdlc.domain.models import RunStatus
from sdlc.engine.bootstrap import create_run, execute, load_context, open_store, runs_dir
from sdlc.engine.context import RunConfig
from sdlc.governance.audit import AuditLog
from sdlc.snapshot import restore, snapshot
from tests.conftest import HumanBot
from tests.fake_agents import build_provider


def test_snapshot_restore_round_trip(sdlc_home, tmp_path, monkeypatch):
    store = open_store()
    scenario = {"name": "greenfield", "mode": "greenfield", "requirement": "Build a URL shortener."}
    run_id = create_run(store, scenario, RunConfig(model="fake", approve_mode="auto"))
    ctx = load_context(store, run_id, provider=build_provider(inject_faults=False, ambiguous=False))
    ctx.approvals.poll_s = 0.05
    with HumanBot(store, ctx.policy):
        assert execute(ctx) == RunStatus.SUCCEEDED

    snap = tmp_path / "demo-state"
    assert snapshot(sdlc_home, snap) == [run_id]
    assert (snap / "runs" / run_id / "repo.bundle").exists()
    assert not (snap / "runs" / run_id / "repo").exists()  # no nested git repos in the snapshot

    new_home = tmp_path / "elsewhere"
    monkeypatch.setenv("SDLC_HOME", str(new_home))
    restore(snap, new_home)
    restored = open_store()
    assert AuditLog(restored).verify(run_id)[0]
    meta = restored.get_run(run_id)
    from sdlc.engine.bootstrap import workspace_path

    repo = workspace_path({**meta, "workspace": "/nonexistent/path/repo"})
    assert repo == runs_dir() / run_id / "repo" and (repo / "app" / "main.py").exists()
    st = restored.load_node_states(run_id)["impl:WP-2"]
    diff = subprocess.run(
        ["git", "diff", st.base_sha, st.merge_sha], cwd=repo, capture_output=True, text=True
    )
    assert "app/api.py" in diff.stdout  # per-node diffs still resolve
    assert "v0.1.0" in subprocess.run(["git", "tag"], cwd=repo, capture_output=True, text=True).stdout
