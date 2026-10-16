"""Deterministic codebase index (brownfield): modules, layers, routes, ORM models, imports.

Grounds the impact-analysis agent in facts instead of guesses, and lets the gate verify that
files the agent claims are impacted actually exist.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
_SKIP = {".git", ".venv", "__pycache__", ".pytest_cache", "worktrees"}


def index_codebase(root: Path) -> dict[str, Any]:
    files: list[str] = []
    modules: dict[str, Any] = {}
    routes: list[dict[str, str]] = []
    models: list[dict[str, Any]] = []
    imports: dict[str, list[str]] = {}
    loc = 0
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP for part in path.relative_to(root).parts) or not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        files.append(rel)
        if path.suffix != ".py":
            continue
        source = path.read_text(errors="replace")
        loc += source.count("\n") + 1
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            modules[rel] = {"error": f"syntax error: {exc}"}
            continue
        info: dict[str, Any] = {"classes": [], "functions": [], "layer": _layer(rel)}
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                deps.add(node.module)
            elif isinstance(node, ast.Import):
                deps.update(a.name for a in node.names)
        imports[rel] = sorted(d for d in deps if d.split(".")[0] in {"app", "tests"})
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                info["classes"].append(node.name)
                table = _tablename(node)
                if table:
                    models.append(
                        {"class": node.name, "table": table, "file": rel, "columns": _columns(node)}
                    )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                info["functions"].append(node.name)
                for dec in node.decorator_list:
                    route = _route(dec)
                    if route:
                        routes.append(
                            {"method": route[0], "path": route[1], "handler": node.name, "file": rel}
                        )
        modules[rel] = info
    return {
        "files": files,
        "python_loc": loc,
        "modules": modules,
        "routes": routes,
        "orm_models": models,
        "internal_imports": imports,
        "tests": [f for f in files if f.startswith("tests/")],
    }


def _layer(rel: str) -> str:
    for layer in ("api", "services", "repositories", "models", "schemas", "core"):
        if f"/{layer}/" in f"/{rel}":
            return layer
    return "tests" if rel.startswith("tests/") else "other"


def _tablename(node: ast.ClassDef) -> str | None:
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "__tablename__" for t in stmt.targets
        ):
            if isinstance(stmt.value, ast.Constant):
                return str(stmt.value.value)
    return None


def _columns(node: ast.ClassDef) -> list[str]:
    cols = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            cols.append(f"{stmt.target.id}: {ast.unparse(stmt.annotation)}")
    return cols


def _route(dec: ast.expr) -> tuple[str, str] | None:
    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in HTTP_METHODS:
        if dec.args and isinstance(dec.args[0], ast.Constant):
            return dec.func.attr.upper(), str(dec.args[0].value)
    return None
