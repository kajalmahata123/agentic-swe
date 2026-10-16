"""Unit tests: policy guardrails, tool guard, approvals, audit chain, graph."""

from __future__ import annotations

import pytest

from sdlc.agents.schemas import ImplementationReport
from sdlc.agents.tools import SubmitAccepted, Toolbox, sandbox_env
from sdlc.domain.graph import DependencyGraph, GraphError
from sdlc.domain.models import ApprovalKind, ApprovalStatus, NodeKind, NodeSpec, Stage
from sdlc.governance.approvals import ApprovalError, ApprovalService
from sdlc.llm.base import ToolCall


# ------------------------------------------------------------------- policy
def test_policy_blocks_dangerous_code(policy):
    scan = policy.scan_changes(
        {"app/x.py": "import subprocess\nsubprocess.run(cmd, shell=True)\nr = eval(s)\n"}, 3
    )
    rules = {f.rule for f in scan.blocking}
    assert {"SEC-001", "SEC-002"} <= rules


def test_code_rules_ignore_strings_and_comments(policy):
    test_src = 'def test_arch():\n    assert "shell=True" not in src  # never shell=True\n'
    assert not policy.scan_changes({"tests/test_arch.py": test_src}, 2).blocking
    leaked = 'API_KEY = "abcdefghijklmnopqrstuvwxyz123456"\n'  # secrets ARE found inside strings
    assert any(f.rule == "SEC-003" for f in policy.scan_changes({"app/k.py": leaked}, 1).blocking)


def test_policy_requires_approval_for_protected_paths_and_unknown_deps(policy):
    scan = policy.scan_changes({"requirements.txt": "fastapi\nleftpad==1.0\n"}, 2)
    assert {f.rule for f in scan.needs_approval} == {"CHG-001", "DEP-001"}


def test_policy_privacy_rule_requires_hashing(policy):
    raw = "def f(request):\n    ip = request.client.host\n    save(ip)\n"
    hashed = (
        "import hashlib\ndef f(request):\n    ip = hashlib.sha256(request.client.host.encode()).hexdigest()\n"
    )
    assert any(f.rule == "CMP-001" for f in policy.scan_changes({"app/a.py": raw}, 3).findings)
    assert not any(f.rule == "CMP-001" for f in policy.scan_changes({"app/a.py": hashed}, 3).findings)


def test_privacy_rule_follows_imports_into_service_layer(policy):
    router = (
        "from app.services import link_service\n\n"
        "def r(request):\n    link_service.click(request.client.host)\n"
    )
    service = "import hmac\n\ndef click(ip):\n    return hmac.new(b'k', ip.encode(), 'sha256')\n"
    plain = "def click(ip):\n    save(ip)\n"
    changed = {"app/api/r.py": router}

    def cmp001(tree):
        scan = policy.scan_changes(changed, 4, resolve=tree.get)
        return any(f.rule == "CMP-001" for f in scan.findings)

    assert not cmp001({**changed, "app/services/link_service.py": service})  # hashed downstream
    assert cmp001({**changed, "app/services/link_service.py": plain})  # stored raw downstream


def test_policy_change_budget(policy):
    scan = policy.scan_changes({"app/a.py": "x = 1\n"}, 10_000)
    assert any(f.rule == "CHG-002" for f in scan.needs_approval)


def test_command_allow_list(policy):
    assert policy.check_command("python -m pytest -q")[0].allowed
    for bad in ("curl http://evil", "python -m pytest; rm -rf /", "pip install x", "bash -c ls", "rm -rf ."):
        assert not policy.check_command(bad)[0].allowed, bad


# ------------------------------------------------------------- tool guard
@pytest.fixture
def toolbox(tmp_path, policy):
    events = []
    tb = Toolbox(tmp_path, policy, ["app/api.py"], lambda t, **d: events.append((t, d)), ImplementationReport)
    tb.events = events
    return tb


def _call(name, **kw):
    return ToolCall("c1", name, kw)


def test_guard_enforces_write_scope_and_workspace_boundary(toolbox, tmp_path):
    assert not toolbox.execute(_call("write_file", path="app/api.py", content="x = 1\n")).is_error
    denied = toolbox.execute(_call("write_file", path="app/other.py", content="x"))
    assert denied.is_error and "SCOPE-004" in denied.content
    escape = toolbox.execute(_call("read_file", path="../../etc/passwd"))
    assert escape.is_error and "SCOPE-001" in escape.content
    git = toolbox.execute(_call("write_file", path=".git/config", content="x"))
    assert git.is_error
    assert toolbox.stats.denied == 3
    assert any(t == "tool.guard" and not d["allowed"] for t, d in toolbox.events)


def test_guard_edit_requires_unique_match(toolbox, tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/api.py").write_text("a = 1\na = 1\n")
    res = toolbox.execute(_call("edit_file", path="app/api.py", old_string="a = 1", new_string="b"))
    assert res.is_error and "exactly once" in res.content


def test_submit_validates_schema(toolbox):
    bad = toolbox.execute(_call("submit", result={"summary": "x"}))
    assert bad.is_error and "schema" in bad.content
    with pytest.raises(SubmitAccepted):
        toolbox.execute(_call("submit", result={"summary": "x", "files_changed": ["app/api.py"]}))


def test_sandbox_env_strips_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    env = sandbox_env(tmp_path)
    assert "ANTHROPIC_API_KEY" not in env and env["PYTHONPATH"] == str(tmp_path)


def test_denied_command_is_audited(toolbox):
    res = toolbox.execute(_call("run_command", command="curl https://example.com"))
    assert res.is_error and "CMD-003" in res.content
    assert any(t == "tool.command" and not d["allowed"] for t, d in toolbox.events)


# --------------------------------------------------------------- approvals
def test_approvals_are_bound_to_content_and_need_reasons(store, audit, policy):
    svc = ApprovalService(store, audit, policy, mode="dashboard")
    a = svc.request("run_1", "design", ApprovalKind.DESIGN, "approve design", {}, "hash-A")
    with pytest.raises(ApprovalError):
        svc.decide(a.id, False, "human:bob", "")  # rejection without a reason
    svc.decide(a.id, True, "human:bob", "ok")
    # same content -> reuse decision; different content -> new approval
    assert svc.request("run_1", "design", ApprovalKind.DESIGN, "x", {}, "hash-A").id == a.id
    assert svc.request("run_1", "design", ApprovalKind.DESIGN, "x", {}, "hash-B").id != a.id
    with pytest.raises(ApprovalError):
        svc.decide(a.id, True, "human:eve", "double decision")


def test_auto_mode_never_auto_approves_release(store, audit, policy):
    svc = ApprovalService(store, audit, policy, mode="auto")
    design = svc.request("run_1", "design", ApprovalKind.DESIGN, "d", {}, "h1")
    release = svc.request("run_1", "release_gate", ApprovalKind.RELEASE, "r", {}, "h2")
    exception = svc.request("run_1", "impl", ApprovalKind.POLICY_EXCEPTION, "p", {}, "h3")
    assert design.status == ApprovalStatus.APPROVED and design.decided_by == "policy:auto-approve"
    assert release.status == ApprovalStatus.PENDING and exception.status == ApprovalStatus.PENDING


def test_supersede_pending(store, audit, policy):
    svc = ApprovalService(store, audit, policy)
    a = svc.request("run_1", "design", ApprovalKind.DESIGN, "d", {}, "h1")
    svc.supersede("run_1", {"design"}, "requirement amended")
    assert store.get_approval(a.id).status == ApprovalStatus.SUPERSEDED


# -------------------------------------------------------------------- audit
def test_audit_chain_detects_tampering_and_deletion(store, audit):
    for i in range(5):
        audit.record("run_x", "node.succeeded", f"n{i}", 1, i=i)
    assert audit.verify("run_x")[0]
    store._conn.execute("DELETE FROM events WHERE run_id='run_x' AND seq=3")
    store._conn.commit()
    ok, msg = audit.verify("run_x")
    assert not ok and "gap" in msg


# -------------------------------------------------------------------- graph
def _n(nid, deps=()):
    return NodeSpec(nid, Stage.DESIGN, NodeKind.AGENT, "design", deps=list(deps))


def test_graph_rejects_cycles_and_unknown_deps():
    g = DependencyGraph([_n("a"), _n("b", ["a"])])
    with pytest.raises(GraphError):
        g.add_dependency("a", "b")
    assert g.get("a").deps == []  # mutation rolled back
    with pytest.raises(GraphError):
        g.add(_n("c", ["zzz"]))
    assert "c" not in g


def test_graph_levels_and_descendants():
    g = DependencyGraph([_n("a"), _n("b", ["a"]), _n("c", ["a"]), _n("d", ["b", "c"])])
    assert g.levels() == {"a": 0, "b": 1, "c": 1, "d": 2}
    assert g.descendants("a") == {"b", "c", "d"}
