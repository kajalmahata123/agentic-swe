"""Tests for configuration/persistence (FR-12), OpenAPI (FR-13) and static
architecture/security rules (NFR-2, NFR-6, NFR-9), plus the performance budget (NFR-4).

Static checks walk the AST of the product code rather than grepping text.
"""

from __future__ import annotations

import ast
import re
import secrets
import time
from pathlib import Path

import pytest
from app.core.config import Settings
from app.main import create_app
from app.models import Base, ClickEvent, IdempotencyKey, Link
from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import AUTH_HEADERS, TEST_API_KEY, create_link
from sqlalchemy import inspect
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import sqltypes

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _py_files(sub: str = "") -> list[Path]:
    return sorted((APP_DIR / sub).rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
            for alias in node.names:
                mods.add(f"{node.module}.{alias.name}")
    return mods


# --------------------------------------------------------------------------- FR-12


def test_tables_created_at_startup_with_temp_sqlite(tmp_path):
    """Covers: FR-12"""
    db_file = tmp_path / "startup.db"
    app = create_app(Settings(database_url=f"sqlite:///{db_file}", api_key=TEST_API_KEY))
    with TestClient(app, headers=AUTH_HEADERS) as client:
        names = set(inspect(app.state.engine).get_table_names())
        assert {"links", "click_events", "idempotency_keys"} <= names
        assert client.get("/readyz").status_code == 200
        assert client.post("/api/links", json={"url": "https://e.com"}).status_code == 201
    assert db_file.exists()


def test_indexes_and_unique_constraints_exist(client):
    """Covers: FR-12, NFR-4, NFR-5"""
    insp = inspect(client.app.state.engine)
    link_uniques = [u["column_names"] for u in insp.get_unique_constraints("links")]
    link_unique_idx = [i["column_names"] for i in insp.get_indexes("links") if i["unique"]]
    assert ["code"] in link_uniques + link_unique_idx
    idem_uniques = [u["column_names"] for u in insp.get_unique_constraints("idempotency_keys")]
    idem_unique_idx = [
        i["column_names"] for i in insp.get_indexes("idempotency_keys") if i["unique"]
    ]
    assert ["key"] in idem_uniques + idem_unique_idx
    click_idx = [i["column_names"] for i in insp.get_indexes("click_events")]
    assert ["link_id", "created_at"] in click_idx


def test_short_url_uses_configured_base_url(make_client):
    """Covers: FR-12, FR-1"""
    client = make_client(base_url="https://go.example.org/")
    body = create_link(client)
    assert body["short_url"] == f"https://go.example.org/{body['code']}"
    detail = client.get(f"/api/links/{body['code']}").json()
    assert detail["short_url"] == body["short_url"]


def test_settings_defaults_and_env(monkeypatch):
    """Covers: FR-12"""
    for var in ("DATABASE_URL", "BASE_URL", "SHORT_CODE_LENGTH", "IP_HASH_SALT", "LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.database_url == "sqlite:///./shortener.db"
    assert s.base_url == "http://localhost:8000"
    assert s.short_code_length == 7
    assert s.code_max_attempts == 5
    assert s.ip_hash_salt is None
    assert s.log_level == "INFO"

    monkeypatch.setenv("DATABASE_URL", "sqlite:///./other.db")
    monkeypatch.setenv("BASE_URL", "https://s.example")
    monkeypatch.setenv("SHORT_CODE_LENGTH", "9")
    monkeypatch.setenv("IP_HASH_SALT", "pepper")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = Settings()
    assert s.database_url == "sqlite:///./other.db"
    assert s.base_url == "https://s.example"
    assert s.short_code_length == 9
    assert s.ip_hash_salt == "pepper"
    assert s.log_level == "DEBUG"

    monkeypatch.setenv("IP_HASH_SALT", "")
    assert Settings().ip_hash_salt is None

    # FR-15 / FR-16: new DB tuning settings, defaults reproduce today's behaviour.
    for var in (
        "SQLITE_BUSY_TIMEOUT_MS",
        "DB_POOL_SIZE",
        "DB_MAX_OVERFLOW",
        "DB_POOL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    s = Settings()
    assert s.sqlite_busy_timeout_ms == 5000
    assert s.db_pool_size == 5
    assert s.db_max_overflow == 10
    assert s.db_pool_timeout_seconds == 30.0
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "1000")
    monkeypatch.setenv("DB_POOL_SIZE", "3")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_TIMEOUT_SECONDS", "2.5")
    s = Settings()
    assert s.sqlite_busy_timeout_ms == 1000
    assert s.db_pool_size == 3
    assert s.db_max_overflow == 0
    assert s.db_pool_timeout_seconds == 2.5


def test_settings_reject_invalid_values():
    """Covers: FR-12, FR-15, FR-16"""
    with pytest.raises(ValueError):
        Settings(log_level="LOUD")
    with pytest.raises(ValueError):
        Settings(short_code_length=2)
    for bad in (
        {"db_pool_size": 0},
        {"db_max_overflow": -1},
        {"db_pool_timeout_seconds": 0},
        {"db_pool_timeout_seconds": 301},
        {"sqlite_busy_timeout_ms": -1},
        {"sqlite_busy_timeout_ms": 60001},
    ):
        with pytest.raises(ValueError):
            Settings(**bad)


def test_env_configured_app_serves_requests(monkeypatch, tmp_path):
    """Covers: FR-12"""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/env.db")
    monkeypatch.setenv("BASE_URL", "https://env.example/")
    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    app = create_app()
    with TestClient(app, headers=AUTH_HEADERS) as client:
        body = create_link(client)
        assert body["short_url"] == f"https://env.example/{body['code']}"
    assert (tmp_path / "env.db").exists()


def test_main_exposes_create_app_and_module_app():
    """Covers: FR-12"""
    import app.main as main

    assert callable(main.create_app)
    assert isinstance(main.app, FastAPI)
    a1, a2 = main.create_app(Settings(database_url="sqlite://")), main.create_app(
        Settings(database_url="sqlite://")
    )
    assert a1 is not a2
    assert a1.state.engine is not a2.state.engine


def test_orm_uses_declarative_base_and_portable_types():
    """Covers: FR-12"""
    assert issubclass(Base, DeclarativeBase)
    for model in (Link, ClickEvent, IdempotencyKey):
        assert issubclass(model, Base)
        assert "__annotations__" in vars(model) or model.__mapper__ is not None
        for col in model.__table__.columns:
            assert type(col.type).__module__ == sqltypes.__name__, (model, col.name, col.type)
    for path in _py_files("models"):
        src = path.read_text(encoding="utf-8")
        assert "Mapped[" in src or path.name in {"__init__.py", "base.py"}
        assert "sqlalchemy.dialects" not in src


# --------------------------------------------------------------------------- FR-13


def test_openapi_lists_endpoints_and_named_schemas(client):
    """Covers: FR-13"""
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    paths = spec["paths"]
    assert "post" in paths["/api/links"]
    assert {"get", "delete"} <= set(paths["/api/links/{code}"])
    assert "get" in paths["/api/links/{code}/stats"]
    assert "get" in paths["/{code}"]
    assert "get" in paths["/healthz"]
    assert "get" in paths["/readyz"]
    schemas = spec["components"]["schemas"]
    for name in ("LinkCreate", "LinkCreatedResponse", "LinkDetailResponse", "LinkStatsResponse"):
        assert name in schemas, name
    ok = paths["/api/links"]["post"]["responses"]["201"]["content"]["application/json"]
    assert ok["schema"]["$ref"].endswith("/LinkCreatedResponse")
    stats_ok = paths["/api/links/{code}/stats"]["get"]["responses"]["200"]
    assert stats_ok["content"]["application/json"]["schema"]["$ref"].endswith(
        "/LinkStatsResponse"
    )
    detail_ok = paths["/api/links/{code}"]["get"]["responses"]["200"]
    assert detail_ok["content"]["application/json"]["schema"]["$ref"].endswith(
        "/LinkDetailResponse"
    )


# --------------------------------------------------------------------------- NFR-2/6/9


def test_no_random_module_and_codes_use_secrets():
    """Covers: NFR-2, FR-3"""
    for path in _py_files():
        mods = _imported_modules(path)
        assert "random" not in mods and not any(m.startswith("random.") for m in mods), path
    assert "secrets" in _imported_modules(APP_DIR / "services" / "codes.py")


def test_no_dynamic_code_execution_or_shell_subprocess():
    """Covers: NFR-2"""
    forbidden_calls = {"eval", "exec", "compile", "__import__"}
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls, (path, node.lineno)
            for kw in node.keywords:
                if kw.arg == "shell":
                    assert not (isinstance(kw.value, ast.Constant) and kw.value.value), (
                        path,
                        node.lineno,
                    )
        assert "subprocess" not in _imported_modules(path), path


def test_sql_text_only_receives_literal_strings():
    """Covers: NFR-2"""
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in {"text", "exec_driver_sql"}:
                for arg in node.args:
                    assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), (
                        path,
                        node.lineno,
                    )


def test_layered_architecture_dependencies_point_inward():
    """Covers: NFR-6"""
    for path in _py_files("services"):
        mods = _imported_modules(path)
        bad = {m for m in mods if m.split(".")[0] in {"fastapi", "starlette"}}
        bad |= {m for m in mods if m.startswith("app.api")}
        assert not bad, (path, bad)
    for path in _py_files("repositories"):
        mods = _imported_modules(path)
        bad = {m for m in mods if m.startswith(("app.api", "app.services", "fastapi"))}
        assert not bad, (path, bad)
    for path in _py_files("models"):
        mods = _imported_modules(path)
        bad = {
            m for m in mods
            if m.startswith("app.") and not m.startswith("app.models")
            or m.split(".")[0] in {"fastapi", "starlette"}
        }
        assert not bad, (path, bad)
    for path in _py_files("schemas"):
        mods = _imported_modules(path)
        bad = {m for m in mods if m.startswith("app.") and not m.startswith("app.schemas")}
        assert not bad, (path, bad)
    for path in _py_files("core"):
        mods = _imported_modules(path)
        bad = {m for m in mods if m.startswith(("app.api", "app.services", "app.repositories"))}
        assert not bad, (path, bad)


def test_no_module_level_settings_or_caches_and_pinned_requirements():
    """Covers: NFR-9, FR-12"""
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in {"lru_cache", "cache"}:
                pytest.fail(f"cache decorator in {path}")
            if isinstance(node, ast.Attribute) and node.attr in {"lru_cache"}:
                pytest.fail(f"cache decorator in {path}")
        for stmt in tree.body:
            if isinstance(stmt, ast.Assign) and isinstance(stmt.value, (ast.Dict, ast.List, ast.Set)):
                targets = [getattr(t, "id", "") for t in stmt.targets]
                assert not any("cache" in t.lower() for t in targets), (path, targets)
    req = (APP_DIR.parent / "requirements.txt").read_text(encoding="utf-8").splitlines()
    lines = [ln.strip() for ln in req if ln.strip() and not ln.startswith("#")]
    assert lines
    for line in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-\[\]]+==[A-Za-z0-9_.\-]+", line), line


# --------------------------------------------------------------------------- NFR-4


def _p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[int(len(ordered) * 0.95) - 1]


@pytest.mark.slow
def test_performance_p95_redirect_and_create(client):
    """Covers: NFR-4"""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    seeded: set[str] = set()
    while len(seeded) < 10_000:
        seeded.add("".join(secrets.choice(alphabet) for _ in range(7)))
    codes = sorted(seeded)
    with client.app.state.session_factory() as s:
        s.add_all(Link(code=c, url=f"https://example.com/{c}") for c in codes)
        s.commit()

    # Warm-up.
    for c in codes[:20]:
        client.get(f"/{c}", follow_redirects=False)

    redirect_times = []
    for i in range(500):
        code = codes[(i * 17) % len(codes)]
        start = time.perf_counter()
        resp = client.get(f"/{code}", follow_redirects=False)
        redirect_times.append((time.perf_counter() - start) * 1000)
        assert resp.status_code == 307

    create_times = []
    for i in range(200):
        start = time.perf_counter()
        resp = client.post("/api/links", json={"url": f"https://example.com/new/{i}"})
        create_times.append((time.perf_counter() - start) * 1000)
        assert resp.status_code == 201

    assert _p95(redirect_times) < 50, _p95(redirect_times)
    assert _p95(create_times) < 100, _p95(create_times)
