"""SDLC dependency-graph templates.

Greenfield:
  intake -> requirements -> clarify(H) -> design(A) -> plan* -> [impl:WP-*] -> integrate(J)
         -> {tests || docs} -> verify(R) -> security_review(R) -> release_gate(A) -> publish
Brownfield adds codebase_scan -> impact_analysis before requirements, and grounds every
later stage in the impact analysis.

  (H) human checkpoint  (A) human approval of output  (J) join barrier
  (R) repairable gate: failure inserts repair-k -> verify-k (-> re-run) and rewires
  *   planner: expands the graph at runtime from the approved design
"""

from __future__ import annotations

from sdlc.domain.models import ApprovalKind, FailurePolicy, NodeKind, NodeSpec, RetryPolicy, Risk, Stage

DETERMINISTIC = RetryPolicy(max_attempts=1)  # re-running the same check yields the same answer
AGENT = RetryPolicy(max_attempts=3, backoff_s=2.0, escalate_model_at=3)


def _common_tail(requirements_inputs: list[str], design_inputs: list[str]) -> list[NodeSpec]:
    return [
        NodeSpec(
            "requirements",
            Stage.REQUIREMENTS,
            NodeKind.AGENT,
            "requirements",
            "Analyse & normalise requirements",
            deps=["intake"] if "codebase_impact" not in requirements_inputs else ["impact_analysis"],
            inputs=requirements_inputs,
            outputs=["requirements_spec"],
            retry=AGENT,
        ),
        NodeSpec(
            "clarify",
            Stage.REQUIREMENTS,
            NodeKind.HUMAN,
            "clarify",
            "Resolve blocking ambiguities",
            deps=["requirements"],
            inputs=["requirements_spec"],
            outputs=["clarification_log"],
            retry=DETERMINISTIC,
            timeout_s=7 * 24 * 3600,
        ),
        NodeSpec(
            "design",
            Stage.DESIGN,
            NodeKind.AGENT,
            "design",
            "Architecture, API contract & work packages",
            deps=["clarify"],
            inputs=design_inputs,
            outputs=["design"],
            risk=Risk.HIGH,
            approval=ApprovalKind.DESIGN,
            retry=AGENT,
            timeout_s=7 * 24 * 3600,
        ),
        NodeSpec(
            "plan",
            Stage.PLANNING,
            NodeKind.PLANNER,
            "plan",
            "Expand design into parallel implementation tasks",
            deps=["design"],
            inputs=["design", "requirements_spec"],
            outputs=["plan"],
            retry=DETERMINISTIC,
            params={"feeds": ["integrate"]},
        ),
        NodeSpec(
            "integrate",
            Stage.IMPLEMENTATION,
            NodeKind.JOIN,
            "integrate",
            "Join barrier + integration smoke test",
            deps=["plan"],
            outputs=["integration"],
            retry=DETERMINISTIC,
        ),
        NodeSpec(
            "tests",
            Stage.TESTING,
            NodeKind.AGENT,
            "write_tests",
            "Author test suite (FR-traced)",
            deps=["integrate"],
            inputs=["requirements_spec", "design"],
            outputs=["test_report"],
            write_scope=["tests/**"],
            retry=AGENT,
            timeout_s=3600,  # suites for a large codebase take many pytest iterations
        ),
        NodeSpec(
            "docs",
            Stage.DOCUMENTATION,
            NodeKind.AGENT,
            "docs",
            "README, API reference, runbook, changelog",
            deps=["integrate"],
            inputs=["requirements_spec", "design"],
            outputs=["docs_report"],
            write_scope=["README.md", "docs/**", "CHANGELOG.md"],
            retry=AGENT,
        ),
        NodeSpec(
            "verify",
            Stage.TESTING,
            NodeKind.CHECK,
            "verify",
            "Tests, coverage, contract, traceability, policy",
            deps=["tests", "docs"],
            inputs=["design", "requirements_spec", "test_report"],
            outputs=["verification_report"],
            retry=DETERMINISTIC,
            params={"repairable": True},
        ),
        NodeSpec(
            "security_review",
            Stage.TESTING,
            NodeKind.AGENT,
            "security_review",
            "Security & privacy review",
            deps=["verify"],
            inputs=["design", "verification_report"],
            outputs=["security_review"],
            retry=AGENT,
            params={"repairable": True},
        ),
        NodeSpec(
            "release_gate",
            Stage.RELEASE,
            NodeKind.CHECK,
            "release_gate",
            "Release readiness + human sign-off",
            deps=["security_review"],
            inputs=["verification_report", "security_review", "docs_report"],
            outputs=["release_readiness"],
            risk=Risk.HIGH,
            approval=ApprovalKind.RELEASE,
            retry=DETERMINISTIC,
            timeout_s=7 * 24 * 3600,
        ),
        NodeSpec(
            "publish",
            Stage.RELEASE,
            NodeKind.ACTION,
            "publish",
            "Tag, bundle, manifest",
            deps=["release_gate"],
            inputs=["release_readiness"],
            outputs=["release"],
            retry=DETERMINISTIC,
            on_failure=FailurePolicy.SAFE_STOP,
        ),
    ]


def greenfield_graph() -> list[NodeSpec]:
    return [
        NodeSpec(
            "intake",
            Stage.REQUIREMENTS,
            NodeKind.ACTION,
            "intake",
            "Capture requirement brief",
            outputs=["requirement"],
            retry=DETERMINISTIC,
        ),
        *_common_tail(["requirement"], ["requirements_spec"]),
    ]


def brownfield_graph() -> list[NodeSpec]:
    return [
        NodeSpec(
            "intake",
            Stage.REQUIREMENTS,
            NodeKind.ACTION,
            "intake",
            "Capture change request",
            outputs=["requirement"],
            retry=DETERMINISTIC,
        ),
        NodeSpec(
            "codebase_scan",
            Stage.REQUIREMENTS,
            NodeKind.CHECK,
            "codebase_scan",
            "Index existing codebase + baseline tests",
            deps=["intake"],
            outputs=["codebase_index"],
            retry=DETERMINISTIC,
        ),
        NodeSpec(
            "impact_analysis",
            Stage.REQUIREMENTS,
            NodeKind.AGENT,
            "impact_analysis",
            "Impacted modules, APIs, data flows, regressions",
            deps=["codebase_scan"],
            inputs=["requirement", "codebase_index"],
            outputs=["codebase_impact"],
            retry=AGENT,
        ),
        *_common_tail(
            ["requirement", "codebase_impact"], ["requirements_spec", "codebase_impact", "codebase_index"]
        ),
    ]


TEMPLATES = {"greenfield": greenfield_graph, "brownfield": brownfield_graph}
