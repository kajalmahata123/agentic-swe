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
