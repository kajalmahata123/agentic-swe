"""Policy guardrails: security, compliance and change control.

The same policy object is consulted at two points:
  1. *Pre-action* — every agent tool call (path scope, forbidden paths, command allow-list).
  2. *Post-action* — the node's diff is scanned before it may merge (code rules,
     protected paths, dependency allow-list, change-size budget).
"""

from __future__ import annotations

import ast
import fnmatch
import io
import re
import shlex
import tokenize
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policies" / "default.yaml"

BLOCK, APPROVE, WARN = "block", "approve", "warn"
_SHELL_META = re.compile(r"[;&|`$<>\n]")


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""
    rule: str = ""


@dataclass
class Finding:
    rule: str
    title: str
    severity: str
    path: str
    line: int | None = None
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class DiffScan:
    findings: list[Finding] = field(default_factory=list)
    changed_lines: int = 0
    files: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCK]

    @property
    def needs_approval(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == APPROVE]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]


def glob_match(path: str, patterns: list[str]) -> bool:
    p = PurePosixPath(path).as_posix()
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p, pat.rstrip("/*")) for pat in patterns)


class Policy:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.budgets = data.get("budgets", {})
        self.autonomy = data.get("autonomy", {})
        self.change = data.get("change_control", {})
        self.deps_allowed = {d.lower() for d in data.get("dependencies", {}).get("allowed", [])}
        self.commands = [list(c) for c in data.get("commands", {}).get("allowed", [])]
        self.command_timeout = float(data.get("commands", {}).get("timeout_s", 300))
        self.release = data.get("release", {})
        self.rules = []
        for r in data.get("code_rules", []):
            self.rules.append(
                {
                    **r,
                    "_re": re.compile(r["pattern"]),
                    "_co": re.compile(r["must_cooccur"]) if r.get("must_cooccur") else None,
                }
            )

    @classmethod
    def load(cls, path: str | Path | None = None) -> Policy:
        with open(path or DEFAULT_POLICY) as fh:
            return cls(yaml.safe_load(fh))

    # ------------------------------------------------------------- pre-action
    def check_path(self, rel_path: str, write: bool, write_scope: list[str]) -> PolicyDecision:
        p = PurePosixPath(rel_path)
        if p.is_absolute() or ".." in p.parts:
            return PolicyDecision(False, f"path {rel_path!r} escapes the workspace", "SCOPE-001")
        if glob_match(rel_path, self.change.get("forbidden_paths", [])):
            return PolicyDecision(False, f"path {rel_path!r} is forbidden by policy", "SCOPE-002")
        if write:
            if not write_scope:
                return PolicyDecision(False, "this agent is read-only", "SCOPE-003")
            if not glob_match(rel_path, write_scope):
                return PolicyDecision(
                    False,
                    f"path {rel_path!r} is outside this task's write scope {write_scope}; "
                    "another task owns it — report the needed change instead",
                    "SCOPE-004",
                )
        return PolicyDecision(True)

    def check_command(self, command: str) -> tuple[PolicyDecision, list[str]]:
        if _SHELL_META.search(command):
            return PolicyDecision(False, "shell metacharacters are not allowed", "CMD-001"), []
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return PolicyDecision(False, f"unparseable command: {exc}", "CMD-002"), []
        for prefix in self.commands:
            if argv[: len(prefix)] == prefix:
                return PolicyDecision(True), argv
        allowed = [" ".join(p) for p in self.commands]
        return PolicyDecision(False, f"command not in allow-list {allowed}", "CMD-003"), []

    def is_auto_approvable(self, kind: str) -> bool:
        return kind in self.autonomy.get("auto_approvable", []) and kind not in self.autonomy.get(
            "never_auto", []
        )

    # ------------------------------------------------------------ post-action
    def scan_changes(
        self,
        changes: dict[str, str | None],
        changed_lines: int,
        resolve: Callable[[str], str | None] | None = None,
    ) -> DiffScan:
        """`changes` maps relative path -> new file content (None when deleted).

        `resolve(path)` reads other workspace files so co-occurrence rules can follow the value
        into the modules a file imports (e.g. a router forwarding an IP to a hashing service).
        """
        scan = DiffScan(changed_lines=changed_lines, files=sorted(changes))
        for path, content in sorted(changes.items()):
            if glob_match(path, self.change.get("forbidden_paths", [])):
                scan.findings.append(Finding("SCOPE-002", "forbidden path modified", BLOCK, path))
            if glob_match(path, self.change.get("protected_paths", [])):
                scan.findings.append(
                    Finding("CHG-001", "protected path modified (change control)", APPROVE, path)
                )
            if content is None:
                continue
            if path.endswith("requirements.txt"):
                scan.findings.extend(self._scan_requirements(path, content))
            if path.endswith(".py"):
                scan.findings.extend(self._scan_code(path, content, resolve))
        limit = self.change.get("max_changed_lines_without_approval")
        if limit and changed_lines > limit:
            scan.findings.append(
                Finding("CHG-002", f"change size {changed_lines} lines exceeds budget {limit}", APPROVE, "*")
            )
        return scan

    def _scan_requirements(self, path: str, content: str) -> list[Finding]:
        out = []
        for i, line in enumerate(content.splitlines(), 1):
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower().replace("_", "-")
            if name and name not in self.deps_allowed:
                out.append(
                    Finding("DEP-001", f"dependency {name!r} not on allow-list", APPROVE, path, i, line)
                )
        return out

    @staticmethod
    def _cooccurs(
        rx: re.Pattern, content: str, resolve: Callable[[str], str | None] | None, depth: int = 2
    ) -> bool:
        """True if `rx` matches the file or a first-party module it imports (up to `depth` hops)."""
        if rx.search(content):
            return True
        if resolve is None or depth == 0:
            return False
        for mod in _first_party_imports(content):
            base = mod.replace(".", "/")
            for candidate in (base + ".py", base + "/__init__.py"):
                src = resolve(candidate)
                if src is not None and Policy._cooccurs(rx, src, resolve, depth - 1):
                    return True
        return False

    def _scan_code(
        self, path: str, content: str, resolve: Callable[[str], str | None] | None = None
    ) -> list[Finding]:
        out = []
        is_test = path.startswith("tests/") or "/tests/" in path
        lines = content.splitlines()
        code_lines = _strip_strings_and_comments(content).splitlines()
        for rule in self.rules:
            if rule.get("paths") and not glob_match(path, rule["paths"]):
                continue
            if is_test and rule["id"] in {"SEC-003", "SEC-007"}:
                continue  # fixtures legitimately contain fake tokens / debug flags
            if rule["_co"] is not None:
                if rule["_re"].search(content) and not self._cooccurs(rule["_co"], content, resolve):
                    ln = next((i for i, x in enumerate(lines, 1) if rule["_re"].search(x)), None)
                    out.append(
                        Finding(
                            rule["id"],
                            rule["title"],
                            rule["severity"],
                            path,
                            ln,
                            lines[ln - 1].strip() if ln else "",
                        )
                    )
                continue
            scan_lines = code_lines if rule.get("code_only") else lines
            for i, line in enumerate(scan_lines, 1):
                if line.lstrip().startswith("#"):
                    continue
                if rule["_re"].search(line):
                    out.append(
                        Finding(
                            rule["id"], rule["title"], rule["severity"], path, i, lines[i - 1].strip()[:160]
                        )
                    )
        return out


def _first_party_imports(content: str, package: str = "app") -> list[str]:
    """Modules of `package` imported by `content` (`from app.x import y` yields app.x and app.x.y)."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == package:
            mods.append(node.module)
            mods.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names if a.name.split(".")[0] == package)
    return mods


def _strip_strings_and_comments(content: str) -> str:
    """Blank out string literals and comments, preserving line numbers (falls back to raw text)."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(content).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return content
    lines = content.splitlines(keepends=True)
    out = [list(line) for line in lines]
    for tok in tokens:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT):
            continue
        (sr, sc), (er, ec) = tok.start, tok.end
        for r in range(sr, er + 1):
            row = out[r - 1]
            start = sc if r == sr else 0
            end = ec if r == er else len(row)
            for c in range(start, min(end, len(row))):
                if row[c] != "\n":
                    row[c] = " "
    return "".join("".join(row) for row in out)
