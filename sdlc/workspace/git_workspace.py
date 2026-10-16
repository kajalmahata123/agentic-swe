"""Git-backed workspace: the unit of rollback and change control.

* The run's product code lives in a git repo; `main` is the integrated state.
* Every mutating node attempt works in its *own* git worktree/branch off `main`, so
  parallel agents never see each other's half-written files.
* Exit gates inspect the attempt's diff; on success it merges into `main` (serialised by a
  lock), on failure the worktree is discarded — that *is* the rollback, and it is exact.
* Re-planning can reset `main` to any earlier node's base commit.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

_GIT_ID = [
    "-c",
    "user.name=sdlc-orchestrator",
    "-c",
    "user.email=orchestrator@sdlc.local",
    "-c",
    "commit.gpgsign=false",
]


class GitError(RuntimeError):
    pass


class MergeConflict(GitError):
    pass


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *_GIT_ID, *args], cwd=cwd, capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


@dataclass
class TaskWorkspace:
    node_id: str
    attempt: int
    path: Path
    branch: str
    base_sha: str


class GitWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self._merge_lock = threading.Lock()

    # ---------------------------------------------------------------- set up
    def init(self, seed_dir: Path | None = None) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        if (self.root / ".git").exists():
            return self.head()
        _git(self.root, "init", "-q", "-b", "main")
        if seed_dir:
            ignore = shutil.ignore_patterns(
                ".git",
                ".venv",
                "__pycache__",
                ".pytest_cache",
                "*.db",
                ".ruff_cache",
                "*.sqlite3",
                ".coverage",
                "htmlcov",
            )
            shutil.copytree(seed_dir, self.root, dirs_exist_ok=True, ignore=ignore)
        gi = self.root / ".gitignore"
        if not gi.exists():
            gi.write_text(
                "__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n*.db\n.coverage\ncoverage.json\n"
            )
        _git(self.root, "add", "-A")
        _git(
            self.root,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "baseline: seeded from existing codebase" if seed_dir else "baseline: empty repository",
        )
        return self.head()

    def head(self) -> str:
        return _git(self.root, "rev-parse", "HEAD")

    # --------------------------------------------------------- task worktrees
    def open_task(self, node_id: str, attempt: int) -> TaskWorkspace:
        safe = node_id.replace(":", "_").replace("/", "_")
        branch = f"task/{safe}-a{attempt}"
        path = self.root.parent / "worktrees" / f"{safe}-a{attempt}"
        if path.exists():
            self._remove_worktree(path, branch)
        with self._merge_lock:  # snapshot main consistently w.r.t. concurrent merges
            base = self.head()
            _git(self.root, "worktree", "add", "-q", "-b", branch, str(path), base)
        return TaskWorkspace(node_id, attempt, path, branch, base)

    def task_changes(self, task: TaskWorkspace) -> tuple[dict[str, str | None], int]:
        """Stage everything and return ({path: new content | None if deleted}, changed line count)."""
        _git(task.path, "add", "-A")
        numstat = _git(task.path, "diff", "--cached", "--numstat", task.base_sha)
        changes: dict[str, str | None] = {}
        lines = 0
        for row in numstat.splitlines():
            added, deleted, rel = row.split("\t", 2)
            lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
            fp = task.path / rel
            if fp.exists():
                try:
                    changes[rel] = fp.read_text()
                except UnicodeDecodeError:
                    changes[rel] = ""
            else:
                changes[rel] = None
        return changes, lines

    def task_diff(self, task: TaskWorkspace, max_chars: int = 60_000) -> str:
        _git(task.path, "add", "-A")
        diff = _git(task.path, "diff", "--cached", task.base_sha)
        return diff if len(diff) <= max_chars else diff[:max_chars] + "\n... [diff truncated]"

    def commit_task(self, task: TaskWorkspace, message: str) -> str | None:
        _git(task.path, "add", "-A")
        if not _git(task.path, "status", "--porcelain"):
            return None
        _git(task.path, "commit", "-q", "-m", message)
        return _git(task.path, "rev-parse", "HEAD")

    def merge_task(self, task: TaskWorkspace, message: str) -> tuple[str, str]:
        """Merge the task branch into main. Returns (main_before, main_after)."""
        with self._merge_lock:
            before = self.head()
            proc = subprocess.run(
                ["git", *_GIT_ID, "merge", "--no-ff", "-q", "-m", message, task.branch],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode != 0:
                _git(self.root, "merge", "--abort", check=False)
                raise MergeConflict(proc.stdout.strip() + "\n" + proc.stderr.strip())
            return before, self.head()

    def discard_task(self, task: TaskWorkspace) -> None:
        self._remove_worktree(task.path, task.branch)

    def _remove_worktree(self, path: Path, branch: str) -> None:
        _git(self.root, "worktree", "remove", "--force", str(path), check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(self.root, "worktree", "prune", check=False)
        _git(self.root, "branch", "-D", branch, check=False)

    # ---------------------------------------------------------- main history
    def reset_main(self, sha: str) -> None:
        with self._merge_lock:
            _git(self.root, "reset", "-q", "--hard", sha)
            _git(self.root, "clean", "-q", "-fd")

    def is_ancestor(self, sha: str, of: str | None = None) -> bool:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, of or "HEAD"],
            cwd=self.root,
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0

    def changed_files_between(self, a: str, b: str = "HEAD") -> list[str]:
        out = _git(self.root, "diff", "--name-only", a, b)
        return [line for line in out.splitlines() if line]

    def tag(self, name: str, message: str) -> None:
        _git(self.root, "tag", "-a", name, "-m", message)

    def log(self, n: int = 50) -> list[dict[str, str]]:
        out = _git(self.root, "log", f"-{n}", "--pretty=format:%H%x1f%s%x1f%ct")
        rows = []
        for line in out.splitlines():
            sha, subject, ts = line.split("\x1f")
            rows.append({"sha": sha, "subject": subject, "ts": ts})
        return rows

    def list_files(self) -> list[str]:
        return [f for f in _git(self.root, "ls-files").splitlines() if f]
