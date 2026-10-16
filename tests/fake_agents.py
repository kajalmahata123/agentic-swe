"""Scripted agents for offline end-to-end tests of the orchestrator.

They behave like real agents *through the real tool layer* (writes go through the policy
guard, code lands in git worktrees, tests really run) and inject realistic faults:
  * a blocking ambiguity on the first requirements pass (clarification loop)
  * WP-2's first attempt writes code that does not compile (retry + rollback)
  * WP-2 ships a product defect (200 instead of 201) that the tests catch (repair loop)
"""

from __future__ import annotations

import re
from collections import defaultdict

from sdlc.llm.fake import FakeProvider, FakeTurn, call, respond

STORE = '''"""In-memory link store."""
import secrets

_LINKS: dict[str, str] = {}


def create(url: str) -> str:
    code = secrets.token_urlsafe(5)
    _LINKS[code] = url
    return code


def resolve(code: str) -> str | None:
    return _LINKS.get(code)
'''

API_BROKEN = "from fastapi import APIRouter\nrouter = APIRouter(\n"  # syntax error

API_DEFECT = '''"""Link routes."""
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, HttpUrl

from app import store

router = APIRouter()


class LinkIn(BaseModel):
    url: HttpUrl


@router.post("/api/v1/links", status_code=200)
def create_link(body: LinkIn) -> dict:
    return {"code": store.create(str(body.url))}


@router.get("/{code}")
def follow(code: str) -> RedirectResponse:
    url = store.resolve(code)
    if url is None:
        raise HTTPException(status_code=404, detail="not found")
    return RedirectResponse(url, status_code=307)
'''

HEALTH = '''"""Health route."""
from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
'''

MAIN = '''"""App factory."""
from fastapi import FastAPI

from app import api, health


def create_app() -> FastAPI:
    app = FastAPI(title="shortener")
    app.include_router(health.router)
    app.include_router(api.router)
    return app


app = create_app()
'''

TESTS = '''from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_create_link():
    """Covers: FR-1"""
    r = client.post("/api/v1/links", json={"url": "https://example.com/a"})
    assert r.status_code == 201
    assert r.json()["code"]


def test_redirect():
    """Covers: FR-2"""
    code = client.post("/api/v1/links", json={"url": "https://example.com/b"}).json()["code"]
    r = client.get(f"/{code}", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "https://example.com/b"


def test_unknown_code():
    """Covers: FR-2"""
    assert client.get("/nope", follow_redirects=False).status_code == 404


def test_rejects_bad_url():
    """Covers: FR-1"""
    assert client.post("/api/v1/links", json={"url": "not a url"}).status_code == 422


def test_health():
    """Covers: FR-3"""
    assert client.get("/healthz").json() == {"status": "ok"}
'''

README = "# Shortener\n\nEndpoints: `POST /api/v1/links`, `GET /{code}`, `GET /healthz`.\n"
API_DOC = "# API\n\n- POST /api/v1/links\n- GET /{code}\n- GET /healthz\n"

SPEC = {
    "title": "URL shortener",
    "problem_statement": "Shorten long URLs and redirect.",
    "functional": [
        {
            "id": "FR-1",
            "title": "Create short link",
            "description": "POST a URL, get a code",
            "acceptance_criteria": ["201 with code", "422 on invalid URL"],
            "priority": "must",
        },
        {
            "id": "FR-2",
            "title": "Redirect",
            "description": "GET /{code} redirects",
            "acceptance_criteria": ["307 to target", "404 unknown"],
            "priority": "must",
        },
        {
            "id": "FR-3",
            "title": "Health",
            "description": "liveness",
            "acceptance_criteria": ["200 ok"],
            "priority": "must",
        },
    ],
    "non_functional": [
        {"id": "NFR-1", "category": "reliability", "requirement": "codes unique", "measure": "unit test"}
    ],
    "assumptions": ["single region"],
    "out_of_scope": ["auth"],
    "risks": [],
}

DESIGN = {
    "overview": "FastAPI app with in-memory store",
    "components": [{"name": "api", "responsibility": "HTTP", "files": ["app/api.py"]}],
    "api_contract": [
        {
            "method": "POST",
            "path": "/api/v1/links",
            "summary": "create",
            "responses": {"201": "created"},
            "fr_ids": ["FR-1"],
        },
        {
            "method": "GET",
            "path": "/{code}",
            "summary": "redirect",
            "responses": {"307": "redirect"},
            "fr_ids": ["FR-2"],
        },
        {
            "method": "GET",
            "path": "/healthz",
            "summary": "health",
            "responses": {"200": "ok"},
            "fr_ids": ["FR-3"],
        },
    ],
    "data_model": [],
    "decisions": [
        {
            "id": "ADR-1",
            "title": "In-memory store",
            "decision": "dict",
            "rationale": "prototype",
            "alternatives": ["sqlite"],
        }
    ],
    "risks": [{"id": "R-1", "description": "data lost on restart", "mitigation": "documented"}],
    "work_packages": [
        {
            "id": "WP-1",
            "title": "store",
            "description": "store module",
            "files": ["requirements.txt", "app/__init__.py", "app/store.py"],
            "depends_on": [],
            "fr_ids": ["FR-1"],
        },
        {
            "id": "WP-2",
            "title": "link api",
            "description": "routes",
            "files": ["app/api.py"],
            "depends_on": ["WP-1"],
            "fr_ids": ["FR-1", "FR-2"],
        },
        {
            "id": "WP-3",
            "title": "health",
            "description": "health",
            "files": ["app/health.py"],
            "depends_on": [],
            "fr_ids": ["FR-3"],
        },
        {
            "id": "WP-4",
            "title": "wiring",
            "description": "main",
            "files": ["app/main.py"],
            "depends_on": ["WP-2", "WP-3"],
            "fr_ids": [],
        },
    ],
    "dependencies": ["fastapi", "pydantic", "httpx", "pytest"],
    "test_strategy": "pytest + TestClient",
    "config": [],
}

WP_FILES = {
    "WP-1": {
        "requirements.txt": "fastapi\npydantic\nhttpx\npytest\n",
        "app/__init__.py": "",
        "app/store.py": STORE,
    },
    "WP-2": {"app/api.py": API_DEFECT},
    "WP-3": {"app/health.py": HEALTH},
    "WP-4": {"app/main.py": MAIN},
}


EXPIRY = '''"""Expiry rule (added by amendment)."""


def is_expired(expires_at: float | None, now: float) -> bool:
    return expires_at is not None and now >= expires_at
'''

EXPIRY_TESTS = '''from app.expiry import is_expired


def test_expiry_rule():
    """Covers: FR-4"""
    assert is_expired(10.0, 11.0)
    assert not is_expired(None, 11.0)
    assert not is_expired(12.0, 11.0)
'''

FR4 = {
    "id": "FR-4",
    "title": "Link expiry",
    "description": "links stop working after expiry",
    "acceptance_criteria": ["expired links are rejected"],
    "priority": "must",
}
WP5 = {
    "id": "WP-5",
    "title": "expiry",
    "description": "expiry rule",
    "files": ["app/expiry.py"],
    "depends_on": [],
    "fr_ids": ["FR-4"],
}
WP_FILES["WP-5"] = {"app/expiry.py": EXPIRY}

IMPACT = {
    "summary": "Layered FastAPI app; the change touches the link API.",
    "architecture": [
        {"path": "app/api.py", "responsibility": "link routes"},
        {"path": "app/store.py", "responsibility": "storage"},
    ],
    "impacted_files": [
        {"path": "app/api.py", "change": "modify", "reason": "new behaviour"},
        {"path": "app/expiry.py", "change": "add", "reason": "expiry rule"},
    ],
    "apis_affected": ["POST /api/v1/links"],
    "data_model_changes": [],
    "data_flows": ["POST /api/v1/links -> store.create"],
    "regression_areas": ["redirects"],
    "existing_defects": [],
    "risks": [],
}


def build_provider(
    inject_faults: bool = True, ambiguous: bool = True, security_high: bool = False
) -> FakeProvider:
    seen: dict[str, int] = defaultdict(int)

    def requirements(turn: FakeTurn):
        spec = {**SPEC, "functional": list(SPEC["functional"])}
        if "## Amendments" in turn.prompt:
            spec["functional"].append(FR4)
        answered = "ANSWER:" in turn.prompt
        spec["ambiguities"] = [
            {
                "id": "Q-1",
                "question": "Should links expire?",
                "why": "changes the data model",
                "options": ["never", "30 days"],
                "default": "never",
                "blocking": ambiguous and not answered,
                "resolved_by": "never" if answered else None,
            }
        ]
        return respond(call("submit", result=spec))

    def architect(turn: FakeTurn):
        design = {**DESIGN, "work_packages": list(DESIGN["work_packages"])}
        if "FR-4" in turn.prompt:
            design["work_packages"].append(WP5)
        return respond(call("submit", result=design))

    def impact(turn: FakeTurn):
        if turn.turn == 1:
            return respond(call("read_file", path="app/api.py"))
        return respond(call("submit", result=IMPACT))

    def implementer(turn: FakeTurn):
        wp = re.search(r"Your work package: (WP-\d+)", turn.prompt).group(1)
        if turn.turn == 1:
            seen[wp] += 1
            files = WP_FILES[wp]
            if inject_faults and wp == "WP-2" and seen[wp] == 1:
                files = {"app/api.py": API_BROKEN}
            return respond(*[call("write_file", path=p, content=c) for p, c in files.items()])
        return respond(call("submit", result={"summary": f"{wp} done", "files_changed": list(WP_FILES[wp])}))

    def test_author(turn: FakeTurn):
        if turn.turn == 1:
            writes = [
                call("write_file", path="tests/__init__.py", content=""),
                call("write_file", path="tests/test_app.py", content=TESTS),
            ]
            if "FR-4" in turn.prompt:
                writes.append(call("write_file", path="tests/test_expiry.py", content=EXPIRY_TESTS))
            return respond(*writes)
        if turn.turn == 2:
            return respond(call("run_command", command="python -m pytest -q"))
        failing = "failed" in (turn.last_results[0].content if turn.last_results else "")
        coverage = {"FR-1": ["test_create_link"], "FR-2": ["test_redirect"], "FR-3": ["test_health"]}
        if "FR-4" in turn.prompt:
            coverage["FR-4"] = ["test_expiry_rule"]
        return respond(
            call(
                "submit",
                result={
                    "summary": "tests written",
                    "test_files": ["tests/test_app.py"],
                    "fr_coverage": coverage,
                    "defects_found": [
                        {
                            "description": "create returns 200 not 201",
                            "location": "app/api.py",
                            "failing_test": "test_create_link",
                        }
                    ]
                    if failing
                    else [],
                },
            )
        )

    def repair(turn: FakeTurn):
        if turn.turn == 1:
            return respond(call("read_file", path="app/api.py"))
        if turn.turn == 2:
            current = turn.last_results[0].content if turn.last_results else ""
            if "status_code=200" in current:
                return respond(
                    call(
                        "edit_file",
                        path="app/api.py",
                        old_string="status_code=200",
                        new_string="status_code=201",
                    )
                )
            return respond(
                call(
                    "write_file",
                    path="app/api.py",
                    content=current + "\n# S-2: mutating endpoints require an API key (gateway)\n",
                )
            )
        return respond(call("submit", result={"summary": "repaired", "files_changed": ["app/api.py"]}))

    def docs(turn: FakeTurn):
        if turn.turn == 1:
            return respond(
                call("write_file", path="README.md", content=README),
                call("write_file", path="docs/API.md", content=API_DOC),
            )
        return respond(call("submit", result={"summary": "docs", "files": ["README.md", "docs/API.md"]}))

    def security(turn: FakeTurn):
        seen["security"] += 1 if turn.turn == 1 else 0
        findings = [
            {
                "id": "S-1",
                "severity": "medium",
                "title": "no rate limiting",
                "location": "app/api.py",
                "recommendation": "add a limiter",
            }
        ]
        if security_high and seen["security"] == 1:
            findings.append(
                {
                    "id": "S-2",
                    "severity": "high",
                    "title": "no auth on mutating endpoints",
                    "location": "app/api.py",
                    "recommendation": "require an API key",
                }
            )
        return respond(call("submit", result={"summary": "review done", "findings": findings}))

    return FakeProvider(
        {
            "requirements": requirements,
            "architect": architect,
            "impact_analyst": impact,
            "implementer": implementer,
            "test_author": test_author,
            "repair": repair,
            "docs_writer": docs,
            "security_reviewer": security,
        }
    )
