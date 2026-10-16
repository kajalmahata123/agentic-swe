"""Deterministic tool runners used by exit gates (never by the LLM)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sdlc.agents.tools import sandbox_env


@dataclass
class ProcResult:
    code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def run_py(root: Path, args: list[str], timeout: float = 600) -> ProcResult:
    try:
        proc = subprocess.run(
            [sys.executable, *args],
            cwd=root,
            capture_output=True,
            text=True,
            env=sandbox_env(root),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ProcResult(124, f"timed out after {timeout}s")
    return ProcResult(proc.returncode, (proc.stdout + "\n" + proc.stderr).strip())


def compile_check(root: Path, target: str = ".") -> ProcResult:
    return run_py(root, ["-m", "compileall", "-q", "-x", r"(\.venv|worktrees|\.git)", target])


def lint_check(root: Path) -> ProcResult:
    # Correctness-only rule set: syntax errors, undefined names, invalid comparisons.
    return run_py(
        root,
        ["-m", "ruff", "check", "--no-cache", "--select", "E9,F63,F7,F82", "--output-format", "concise", "."],
    )


@dataclass
class PytestResult:
    code: int
    passed: int
    failed: int
    errors: int
    skipped: int
    coverage: float | None
    output: str

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.errors

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["output"] = _tail(self.output, 8000)
        return d


def run_pytest(root: Path, cov_package: str = "app", timeout: float = 900) -> PytestResult:
    cov_file = root / "coverage.json"
    cov_file.unlink(missing_ok=True)
    res = run_py(
        root,
        [
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--maxfail=50",
            f"--cov={cov_package}",
            "--cov-report=json:coverage.json",
            "--cov-report=",
            "-o",
            "addopts=",
        ],
        timeout=timeout,
    )
    counts = {k: 0 for k in ("passed", "failed", "error", "skipped")}
    tail = res.output.splitlines()[-15:]
    for line in reversed(tail):
        found = re.findall(r"(\d+) (passed|failed|errors?|skipped)", line)
        if found:
            for n, kind in found:
                counts["error" if kind.startswith("error") else kind] = int(n)
            break
    coverage = None
    if cov_file.exists():
        try:
            coverage = round(json.loads(cov_file.read_text())["totals"]["percent_covered"], 2)
        except (KeyError, ValueError):
            coverage = None
        cov_file.unlink(missing_ok=True)
    return PytestResult(
        res.code, counts["passed"], counts["failed"], counts["error"], counts["skipped"], coverage, res.output
    )


def _tail(text: str, n: int) -> str:
    return text if len(text) <= n else "... [earlier output truncated]\n" + text[-n:]


# OpenAPI paths give prefixed, schema-visible routes; a recursive walk of the router tree adds
# routes hidden from the schema (e.g. a redirect endpoint with include_in_schema=False).
# Recent FastAPI versions wrap included routers lazily, hence `original_router`.
OPENAPI_DUMP = (
    "import json\n"
    "from app.main import app\n"
    "routes = {}\n"
    "for path, ops in app.openapi().get('paths', {}).items():\n"
    "    routes.setdefault(path, set()).update(m.upper() for m in ops if m.upper() != 'PARAMETERS')\n"
    "def walk(rs):\n"
    "    for r in rs:\n"
    "        inner = getattr(r, 'original_router', None) or getattr(r, 'router', None)\n"
    "        if inner is not None and inner is not app.router and hasattr(inner, 'routes'):\n"
    "            walk(inner.routes)\n"
    "        methods, path = getattr(r, 'methods', None), getattr(r, 'path', None)\n"
    "        if methods and path:\n"
    "            routes.setdefault(path, set()).update(m for m in methods if m not in ('HEAD', 'OPTIONS'))\n"
    "walk(app.routes)\n"
    "print('__OPENAPI__' + json.dumps({p: sorted(m) for p, m in routes.items()}))\n"
)


def dump_routes(root: Path) -> tuple[dict[str, list[str]] | None, str]:
    res = run_py(root, ["-c", OPENAPI_DUMP], timeout=120)
    marker = next((ln for ln in res.output.splitlines() if ln.startswith("__OPENAPI__")), None)
    if not res.ok or marker is None:
        return None, _tail(res.output, 4000)
    return json.loads(marker[len("__OPENAPI__") :]), ""


def normalise_path(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/") or "/")
