# Assignment requirements → implementation

Where each requirement of the assignment is met, and how to see it working.

## Core requirements

| # | Requirement | Implementation | Evidence |
|---|---|---|---|
| 1 | **Requirement understanding**: interpret intent, identify ambiguity, normalise | `requirements` agent → `RequirementsSpec` (FRs with acceptance criteria, measurable NFRs, ambiguities with options/default/blocking, assumptions, risks). `clarify` human checkpoint for blocking ambiguities; answers regenerate the spec. Non-blocking defaults are recorded as decisions. | `sdlc/agents/handlers.py` (`RequirementsAgent`), `sdlc/pipelines/steps.py` (`Clarify`); ambiguous scenario; `requirements_spec` artifact |
| 2 | **Task decomposition** with dependencies and sequencing | Architect produces work packages with exact file ownership, `depends_on` and FR mapping. Gates check acyclicity, disjoint ownership and FR coverage. The planner turns them into graph nodes and computes parallel waves. | `design_gates`, `Planner`; `plan` artifact; `sdlc graph RUN` |
| 3 | **Codebase reasoning (brownfield)**: impacted modules/APIs/data flows | Deterministic AST index (modules, layers, routes, ORM models, imports, baseline tests) + `impact_analysis` agent (impacted files, APIs, schema changes, data flows, regression areas, existing defects). A gate rejects invented files. Feeds requirements and design. | `sdlc/checks/codebase.py`, `ImpactAnalyst`; brownfield scenario |
| 4 | **Workflow orchestration** (critical differentiator) | See the next table | `sdlc/engine/*` |
| 5 | **Engineering output**: code, API/schema definitions, tests, docs | Implementers (code), architect (API contract + data model + ADRs), test author (FR-traced pytest), docs writer (README, API.md, OPERATIONS.md, CHANGELOG), release gate (release notes), publish (bundle + manifest + SBOM list). | `.sdlc/runs/<id>/repo`, `.sdlc/runs/<id>/release/` |
| 6 | **Validation & risk control** | Deterministic exit gates at every stage ([TESTING.md §3](TESTING.md)); policy guardrails; security review agent; risks captured by requirements and design and surfaced in release notes and the summary. | `sdlc/engine/gates.py`, `Verify`, `policies/default.yaml` |
| 7 | **Controlled autonomy** | Agents act only through governed tools within write scopes and budgets. Humans answer clarifications, approve design/changes/exceptions/release, handle escalations, can stop, resume or amend at any time. Autonomy levels: `console`/`dashboard`/`auto`, with release and policy exceptions never automatable. | `ApprovalService`, `policies/default.yaml#autonomy` |
| 8 | **Final engineering summary** | Generated per run from artifacts, decisions, approvals and events: plan/rationale, decomposition, ADRs, validation, risks, assumptions, oversight, metrics, limitations, release, lineage. The project-level summary is in [ENGINEERING_SUMMARY.md](ENGINEERING_SUMMARY.md). | `sdlc report RUN`, `.sdlc/runs/<id>/ENGINEERING_SUMMARY.md` |

## Orchestration capabilities (requirement 4)

| Capability | Implementation |
|---|---|
| Full SDLC coverage | Stages: requirements → design → planning → implementation → testing → documentation → release (`Stage` enum; every node belongs to one) |
| Explicit dependency graph | `DependencyGraph`: validated DAG, cycle-safe mutations, topological order, levels |
| Entry / exit gates | `entry_gate` (deps, settled ancestors, inputs, budget); exit = handler gates + diff policy scan |
| Sequential & parallel paths with synchronisation | Waves of independent WPs run concurrently (`max_parallel`); `integrate` join barrier; `tests ‖ docs` joined by `verify` |
| Non-linear, stateful execution | Clarification loop, repair loops, amendment re-plans, escalations; all state persisted in SQLite; resumable |
| Cross-stage context & decision lineage | Content-hashed, versioned artifacts with `derived_from`; `Decision` records with actor and basis; `sdlc lineage` |
| Human approval checkpoints | clarification, design, change, policy_exception, escalation, release; content-hash bound |
| Bounded retries | `RetryPolicy` per node with exponential backoff and feedback |
| Fallback | escalation attempt → fallback model + `xhigh` effort; refusal → fallback; optional nodes skip |
| Rollback | per-attempt git worktree discard; `main` reset on re-plan; publish removes tag/bundle on failure |
| Safe-stop | operator, budget, circuit breaker, fatal provider error, human abort → rollback in-flight, park, resumable |
| Policy guardrails: security, compliance, change control | `policies/default.yaml` + `Policy` (pre-action tool guard, post-action diff scan) |
| Audit-grade observability & traceability | Hash-chained events (`sdlc audit --verify`), transcripts, artifacts, decisions, git history |
| Reliability metrics | success rate, first-pass yield, retries, rollbacks, MTTR, e2e and per-stage latency, repair loops, re-plans, approvals, denials, cost (`sdlc metrics`) |
| Dynamic re-planning | planner expansion, output-change invalidation with memoised reuse, amendments, repair-loop insertion |
| Governance & controlled autonomy maintained during re-plans | superseded approvals, fresh approvals for changed content, ancestor-settled entry gate |

## Deliverables

| Deliverable | Where |
|---|---|
| Working prototype (end to end) | `sdlc run scenarios/*.yaml`; outputs under `.sdlc/runs/` |
| Architecture overview | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Three scenarios | [SCENARIOS.md](SCENARIOS.md), `scenarios/*.yaml` |
| Setup instructions | [README.md](../README.md#setup) |
| Testing approach, limitations, trade-offs | [TESTING.md](TESTING.md) |
