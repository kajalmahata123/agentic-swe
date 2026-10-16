"""The only capabilities an agent has. Every call passes the policy guard and is audited.

Agents never touch the filesystem or a shell directly: paths are resolved inside the task's
workspace, writes are confined to the task's write scope, commands run without a shell from
an allow-list, with a scrubbed environment (no API keys) and a timeout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from sdlc.governance.policy import Policy, glob_match
from sdlc.llm.base import ToolCall, ToolResult, ToolSpec

MAX_READ = 120_000
MAX_OUTPUT = 12_000
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "node_modules"}

AuditFn = Callable[..., Any]


@dataclass
class ToolStats:
    calls: int = 0
    denied: int = 0
    errors: int = 0
    files_written: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)


class SubmitAccepted(Exception):
    def __init__(self, value: dict[str, Any]) -> None:
        self.value = value


class Toolbox:
    def __init__(
        self,
        root: Path,
        policy: Policy,
        write_scope: list[str],
        audit: AuditFn,
        output_model: type[BaseModel],
        allow_commands: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.policy = policy
        self.write_scope = write_scope
        self.audit = audit
        self.output_model = output_model
        self.allow_commands = allow_commands
        self.stats = ToolStats()

    # ------------------------------------------------------------------ specs
    def specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                "list_files",
                "List files in the workspace matching a glob (default all). Returns relative paths.",
                {
                    "type": "object",
                    "properties": {"pattern": {"type": "string", "description": "glob, e.g. 'app/**/*.py'"}},
                    "required": [],
                },
            ),
            ToolSpec(
                "read_file",
                "Read a UTF-8 text file from the workspace.",
                {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            ),
            ToolSpec(
                "search",
                "Regex search across workspace text files. Returns path:line: text.",
                {
                    "type": "object",
                    "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}},
                    "required": ["pattern"],
                },
            ),
        ]
        if self.write_scope:
            specs += [
                ToolSpec(
                    "write_file",
                    "Create or overwrite a file (whole content). Only paths inside your "
                    f"write scope are allowed: {self.write_scope}",
                    {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                        "required": ["path", "content"],
                    },
                ),
                ToolSpec(
                    "edit_file",
                    "Replace exactly one occurrence of old_string with new_string in a file.",
                    {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                ),
            ]
        if self.allow_commands:
            allowed = ", ".join(" ".join(c) for c in self.policy.commands)
            specs.append(
                ToolSpec(
                    "run_command",
                    f"Run an allow-listed command in the workspace root (no shell). Allowed prefixes: "
                    f"{allowed}. Example: 'python -m pytest -q tests/test_links.py'.",
                    {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                )
            )
        specs.append(
            ToolSpec(
                "submit",
                "Finish the task by submitting your final structured result. Call exactly once, last.",
                {
                    "type": "object",
                    "properties": {"result": self.output_model.model_json_schema()},
                    "required": ["result"],
                },
            )
        )
        return specs

    # -------------------------------------------------------------- dispatch
    def execute(self, call: ToolCall) -> ToolResult:
        self.stats.calls += 1
        fn = getattr(self, f"_t_{call.name}", None)
        if fn is None:
            self.stats.errors += 1
            return ToolResult(call.id, f"unknown tool {call.name!r}", True)
        try:
            return ToolResult(call.id, fn(**call.input))
        except SubmitAccepted:
            raise
        except _Denied as d:
            self.stats.denied += 1
            return ToolResult(call.id, f"DENIED by policy [{d.rule}]: {d.reason}", True)
        except TypeError as exc:
            self.stats.errors += 1
            return ToolResult(call.id, f"invalid arguments for {call.name}: {exc}", True)
        except (OSError, ValueError) as exc:
            self.stats.errors += 1
            return ToolResult(call.id, f"error: {exc}", True)

    # ----------------------------------------------------------------- guard
    def _resolve(self, rel: str, write: bool) -> Path:
        rel = rel.strip()
        candidate = (self.root / rel).resolve()
        try:
            rel_norm = candidate.relative_to(self.root).as_posix()
        except ValueError:
            self.audit(
                "tool.guard",
                tool="write" if write else "read",
                path=rel,
                allowed=False,
                rule="SCOPE-001",
                reason="path escapes the workspace",
            )
            self._deny("SCOPE-001", f"path {rel!r} escapes the workspace")
        decision = self.policy.check_path(rel_norm, write, self.write_scope)
        if write or not decision.allowed:  # reads are high-volume: audit writes and denials only
            self.audit(
                "tool.guard",
                tool="write" if write else "read",
                path=rel_norm,
                allowed=decision.allowed,
                rule=decision.rule,
                reason=decision.reason,
            )
        if not decision.allowed:
            self._deny(decision.rule, decision.reason)
        return candidate

    def _deny(self, rule: str, reason: str) -> None:
        raise _Denied(rule, reason)

    # ----------------------------------------------------------------- tools
    def _t_list_files(self, pattern: str = "**/*") -> str:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for f in filenames:
                rel = (Path(dirpath) / f).relative_to(self.root).as_posix()
                if glob_match(rel, [pattern]) or pattern in ("**/*", "*", ""):
                    out.append(rel)
        out.sort()
        return "\n".join(out[:500]) or "(no files)"

    def _t_read_file(self, path: str) -> str:
        fp = self._resolve(path, write=False)
        if not fp.is_file():
            raise ValueError(f"no such file: {path}")
        text = fp.read_text(errors="replace")
        return text if len(text) <= MAX_READ else text[:MAX_READ] + "\n... [truncated]"

    def _t_search(self, pattern: str, glob: str = "**/*") -> str:
        rx = re.compile(pattern)
        hits = []
        for rel in self._t_list_files(glob).splitlines():
            fp = self.root / rel
            try:
                for i, line in enumerate(fp.read_text().splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n... [more matches]"
            except (UnicodeDecodeError, OSError):
                continue
        return "\n".join(hits) or "(no matches)"

    def _t_write_file(self, path: str, content: str) -> str:
        fp = self._resolve(path, write=True)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        rel = fp.relative_to(self.root).as_posix()
        self.stats.files_written.add(rel)
        return f"wrote {rel} ({len(content.splitlines())} lines)"

    def _t_edit_file(self, path: str, old_string: str, new_string: str) -> str:
        fp = self._resolve(path, write=True)
        if not fp.is_file():
            raise ValueError(f"no such file: {path}")
        text = fp.read_text()
        n = text.count(old_string)
        if n != 1:
            raise ValueError(f"old_string must occur exactly once in {path}; found {n} occurrences")
        fp.write_text(text.replace(old_string, new_string, 1))
        rel = fp.relative_to(self.root).as_posix()
        self.stats.files_written.add(rel)
        return f"edited {rel}"

    def _t_run_command(self, command: str) -> str:
        decision, argv = self.policy.check_command(command)
        self.audit(
            "tool.command",
            command=command[:300],
            allowed=decision.allowed,
            rule=decision.rule,
            reason=decision.reason,
        )
        if not decision.allowed:
            self._deny(decision.rule, decision.reason)
        if argv[0] == "python":
            argv[0] = sys.executable
        elif argv[0] in {"pytest", "ruff"}:
            argv = [sys.executable, "-m", *argv]
        self.stats.commands.append(command)
        env = sandbox_env(self.root)
        try:
            proc = subprocess.run(
                argv,
                cwd=self.root,
                capture_output=True,
                text=True,
                env=env,
                timeout=self.policy.command_timeout,
            )
        except subprocess.TimeoutExpired:
            return f"command timed out after {self.policy.command_timeout}s"
        out = (proc.stdout + ("\n" + proc.stderr if proc.stderr else "")).strip()
        if len(out) > MAX_OUTPUT:
            out = out[:2000] + "\n... [output truncated] ...\n" + out[-(MAX_OUTPUT - 2000) :]
        return f"exit code {proc.returncode}\n{out}"

    def _t_submit(self, result: Any) -> str:
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError as exc:
                raise ValueError(f"result must be a JSON object: {exc}") from exc
        try:
            value = self.output_model.model_validate(result)
        except ValidationError as exc:
            raise ValueError(f"result does not match the required schema; fix and resubmit:\n{exc}") from exc
        raise SubmitAccepted(value.model_dump(mode="json"))


class _Denied(Exception):
    def __init__(self, rule: str, reason: str) -> None:
        self.rule, self.reason = rule, reason


SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)", re.I)


def sandbox_env(root: Path) -> dict[str, str]:
    """Minimal environment for agent-run commands: no credentials, workspace on PYTHONPATH."""
    keep = {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}
    env = {k: v for k, v in os.environ.items() if k in keep and not SECRET_ENV.search(k)}
    env.update(
        {
            "PYTHONPATH": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "DATABASE_URL": "sqlite:///./test_agent.db",
        }
    )
    return env
