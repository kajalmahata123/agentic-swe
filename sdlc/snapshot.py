"""Portable snapshots of orchestrator state, so recorded runs can be inspected anywhere.

`snapshot` turns a live state home (.sdlc) into a committable folder:
  state.db                      consistent copy (SQLite online backup, safe while runs are live)
  runs/<id>/repo.bundle         the run's full git history (all branches + tags) as one file
  runs/<id>/transcripts/        per-attempt agent transcripts
  runs/<id>/release/            release bundle, manifest, notes
  runs/<id>/ENGINEERING_SUMMARY.md
Worktrees (temporary) and working trees (recreated from the bundle) are not copied.

`restore` rebuilds a working state home from a snapshot: copies files and clones each bundle
into runs/<id>/repo. Nothing needs an API key: the dashboard and every read-only CLI command
(status, audit --verify, lineage, report, metrics, graph) work on the restored state.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

COPY_DIRS = ("transcripts", "release")
COPY_FILES = ("ENGINEERING_SUMMARY.md",)


def snapshot(home: Path, out: Path) -> list[str]:
    home, out = Path(home).resolve(), Path(out).resolve()
    if not (home / "state.db").exists():
        raise FileNotFoundError(f"no state.db in {home}")
    if out.exists():
        shutil.rmtree(out)
    (out / "runs").mkdir(parents=True)
    src = sqlite3.connect(home / "state.db")
    dst = sqlite3.connect(out / "state.db")
    with dst:
        src.backup(dst)  # consistent even while engines are writing (WAL)
    dst.execute("VACUUM")
    dst.close()
    src.close()
    done = []
    for run_dir in sorted((home / "runs").iterdir()):
        if not run_dir.is_dir():
            continue
        target = out / "runs" / run_dir.name
        target.mkdir()
        repo = run_dir / "repo"
        if (repo / ".git").exists():
            subprocess.run(
                ["git", "bundle", "create", str(target / "repo.bundle"), "--all"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
        for name in COPY_DIRS:
            if (run_dir / name).exists():
                shutil.copytree(run_dir / name, target / name)
        for name in COPY_FILES:
            if (run_dir / name).exists():
                shutil.copy2(run_dir / name, target / name)
        done.append(run_dir.name)
    return done


def restore(snapshot_dir: Path, home: Path, force: bool = False) -> list[str]:
    snapshot_dir, home = Path(snapshot_dir).resolve(), Path(home).resolve()
    if home.exists():
        if not force:
            raise FileExistsError(f"{home} exists; pass force=True (--force) to replace it")
        shutil.rmtree(home)
    (home / "runs").mkdir(parents=True)
    shutil.copy2(snapshot_dir / "state.db", home / "state.db")
    restored = []
    for snap_run in sorted((snapshot_dir / "runs").iterdir()):
        target = home / "runs" / snap_run.name
        shutil.copytree(snap_run, target, ignore=shutil.ignore_patterns("repo.bundle"))
        bundle = snap_run / "repo.bundle"
        if bundle.exists():
            subprocess.run(
                ["git", "clone", "-q", str(bundle), str(target / "repo")], check=True, capture_output=True
            )
            # recreate local branches so per-node diffs (base_sha..merge_sha) resolve
            subprocess.run(["git", "checkout", "-q", "main"], cwd=target / "repo", capture_output=True)
        restored.append(snap_run.name)
    return restored


# ---------------------------------------------------------------------- export
def export_runs(home: Path, out: Path, logs: dict[str, Path] | None = None) -> list[str]:
    """Write every run as a plain, browsable folder (no nested git repos, no binary db):

    <out>/<scenario>/
      README.md                 index: outcome, metrics, approvals, where to look
      code/                     generated source at the release tag (or HEAD)
      GIT_HISTORY.md            one commit + merge per node attempt, with file stats
      artifacts/<name>.v<N>.json  every artifact version (requirements, design, plan, reports...)
      approvals.json  decisions.json  metrics.json  audit_log.jsonl
      transcripts/  release/  ENGINEERING_SUMMARY.md  console.log
    """
    import json

    from sdlc.domain.graph import DependencyGraph
    from sdlc.observability.metrics import run_metrics
    from sdlc.persistence.store import Store

    home, out = Path(home).resolve(), Path(out).resolve()
    store = Store(home / "state.db")
    out.mkdir(parents=True, exist_ok=True)
    index_rows = []
    try:
        for row in sorted(store.list_runs(500), key=lambda r: r["created_at"]):
            meta = store.get_run(row["id"])  # full record (graph, config)
            run_id, scenario = meta["id"], meta["scenario"]
            target = out / scenario
            if target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True)
            run_dir = home / "runs" / run_id
            repo = run_dir / "repo"
            tags = _git(repo, "tag", "--sort=-creatordate").split() if (repo / ".git").exists() else []
            ref = tags[0] if tags else "HEAD"
            if (repo / ".git").exists():
                archive = subprocess.run(
                    ["git", "archive", ref], cwd=repo, capture_output=True, check=True
                ).stdout
                (target / "code").mkdir()
                subprocess.run(["tar", "-x", "-C", str(target / "code")], input=archive, check=True)
                log = _git(
                    repo, "log", "--reverse", "--date=iso", "--stat", "--format=%n### %s%n`%h` · %ad", ref
                )
                (target / "GIT_HISTORY.md").write_text(
                    f"# Git history of the generated code ({scenario}, `{run_id}`, ref `{ref}`)\n\n"
                    "Each node attempt commits on its own branch and is merged into `main` only after its "
                    "exit gates and approvals pass.\n" + log + "\n"
                )
            arts = target / "artifacts"
            arts.mkdir()
            for a in store.list_artifacts(run_id, latest_only=False):
                (arts / f"{a.name.replace(':', '_')}.v{a.version}.json").write_text(
                    json.dumps(
                        {
                            "name": a.name,
                            "version": a.version,
                            "hash": a.hash,
                            "producer": a.producer,
                            "derived_from": a.derived_from,
                            "value": a.value,
                        },
                        indent=2,
                        default=str,
                    )
                )
            approvals = store.list_approvals(run_id)
            (target / "approvals.json").write_text(
                json.dumps(
                    [{**a.__dict__, "kind": a.kind.value, "status": a.status.value} for a in approvals],
                    indent=2,
                    default=str,
                )
            )
            (target / "decisions.json").write_text(
                json.dumps([d.__dict__ for d in store.list_decisions(run_id)], indent=2, default=str)
            )
            metrics = run_metrics(store, run_id)
            (target / "metrics.json").write_text(json.dumps(metrics, indent=2))
            with (target / "audit_log.jsonl").open("w") as fh:
                for e in store.events(run_id):
                    fh.write(json.dumps(e, default=str) + "\n")
            for name in COPY_DIRS:
                if (run_dir / name).exists():
                    shutil.copytree(run_dir / name, target / name)
            for name in COPY_FILES:
                if (run_dir / name).exists():
                    shutil.copy2(run_dir / name, target / name)
            if logs and scenario in logs and logs[scenario].exists():
                shutil.copy2(logs[scenario], target / "console.log")
            graph = DependencyGraph.from_dict(meta["graph"])
            states = store.load_node_states(run_id)
            release = store.latest_artifact(run_id, "release") or None
            tag = release.value.get("tag") if release else "—"
            (target / "README.md").write_text(_run_readme(meta, graph, states, approvals, metrics, tag))
            index_rows.append(
                f"| [{scenario}]({scenario}/) | `{run_id}` | {meta['status']} | {tag} | "
                f"{metrics.get('attempts')} | {metrics.get('replans')} | "
                f"{metrics.get('repair_loops')} | ${metrics.get('llm_cost_usd')} |"
            )
    finally:
        store.close()
    (out / "README.md").write_text(
        "# Generated by the orchestrator: live runs\n\n"
        "Everything in this folder was **produced by the agents** during the live scenario runs. It's "
        "exported as plain files so it can be browsed on GitHub. For the interactive view (dashboard, "
        "per-node diffs, transcripts), run `make demo`.\n\n"
        "| Scenario | Run | Status | Release | Attempts | Re-plans | Repair loops | LLM cost |\n"
        "|---|---|---|---|---|---|---|---|\n" + "\n".join(index_rows) + "\n"
    )
    return [r.split("](")[0].lstrip("| [") for r in index_rows]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def _run_readme(meta, graph, states, approvals, metrics, tag) -> str:
    lines = [
        f"# {meta['scenario']}: `{meta['id']}`",
        "",
        f"**Status:** {meta['status']} · **Release:** {tag}",
        "",
        "## Requirement given to the agents",
        "",
        "> " + meta["requirement"].strip().replace("\n", "\n> "),
        "",
        "## Where to look",
        "",
        "| Path | What it is |",
        "|---|---|",
        "| [`code/`](code/) | the generated service at its release tag: app, tests, docs |",
        "| [`ENGINEERING_SUMMARY.md`](ENGINEERING_SUMMARY.md) | final engineering summary of the run |",
        "| [`artifacts/`](artifacts/) | every artifact version: requirements spec, impact analysis, design "
        "(API, ADRs, work packages), plan, test/verification/security reports, release readiness |",
        "| [`transcripts/`](transcripts/) | every agent attempt: turns, tool calls, results, cost (JSONL) |",
        "| [`GIT_HISTORY.md`](GIT_HISTORY.md) | commit + merge per node attempt |",
        "| [`approvals.json`](approvals.json) · [`decisions.json`](decisions.json) | decisions |",
        "| [`audit_log.jsonl`](audit_log.jsonl) | the full hash-chained event log |",
        "| [`metrics.json`](metrics.json) | reliability metrics |",
        "| [`release/`](release/) | release bundle, manifest (sha256, approvals, SBOM), release notes |",
        "| [`console.log`](console.log) | the live CLI output of the run |",
        "",
        "## Nodes",
        "",
        "| Node | Stage | Kind | Status | Attempts | Cost |",
        "|---|---|---|---|---|---|",
    ]
    for nid in graph.topological_order():
        spec, st = graph.get(nid), states.get(nid)
        lines.append(
            f"| {nid} | {spec.stage.value} | {spec.kind.value} | {st.status.value if st else '—'} | "
            f"{st.attempts if st else 0} | ${(st.cost_usd if st else 0):.2f} |"
        )
    lines += [
        "",
        "## Human & policy checkpoints",
        "",
        "| Kind | Node | Decision | By | Comment |",
        "|---|---|---|---|---|",
    ]
    for a in approvals:
        comment = (a.comment or "").replace("|", "\\|").replace("\n", " ")[:120]
        lines.append(
            f"| {a.kind.value} | {a.node_id} | {a.status.value} | {a.decided_by or '—'} | {comment} |"
        )
    lines += [
        "",
        "## Metrics",
        "",
        "```json",
        __import__("json").dumps({k: v for k, v in metrics.items() if k != "stage_latency_s"}, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)
