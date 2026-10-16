# Agentic SDLC Orchestrator

A governed, multi-agent system that turns a software requirement into a **reviewed, tested,
released** service, demonstrated by having it build and evolve a **URL shortener**
(core API, analytics, reliability features).

* **LLM agents do the engineering.** A requirements analyst, codebase impact analyst,
  architect, parallel implementers, test author, repair engineer, security reviewer and
  technical writer, each with a narrow role, a narrow write scope and a structured output contract.
* **An orchestration engine runs them as an explicit dependency graph** with entry/exit gates,
  parallel waves with join barriers, runtime graph expansion, repair loops, bounded retries,
  model fallback, git-exact rollback, safe-stop/resume, and re-planning when upstream outputs
  or requirements change.
* **Deterministic gates verify everything the agents claim**: compile, lint, tests, coverage,
  API-contract conformance, requirement traceability, and a policy scan for security,
  privacy and change control.
* **Humans own the decisions**: clarifications, design approval, policy exceptions, escalations
  and release sign-off. These go through a CLI or a web dashboard and are bound to the exact
  content approved.
* **Everything is auditable**: a hash-chained event log, per-attempt agent transcripts,
  artifact lineage, decision records, reliability metrics (success rate, retries, rollbacks,
  MTTR, latency, cost) and a generated engineering summary per run.

> The URL shortener is **not hand-written**. It is the *output* of the orchestrator. The
> repository contains the system that produces it. Run outputs (code, tests, docs, release
> bundle) land in `.sdlc/runs/<run_id>/`.

## Documentation

> Start with the **[Design Document](docs/DESIGN.md)**, then the [Architecture overview](docs/ARCHITECTURE.md).

| Document | Contents |
|---|---|
| **[docs/DESIGN.md](docs/DESIGN.md)** | **Design document**: goals/non-goals, component & data design, sequence diagrams, interfaces (CLI + REST), concurrency, failure modes, security, alternatives, scaling path; **plus the generated URL-shortener design** (API, data model, ADRs, work packages) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Architecture overview: components, orchestration model, control flow, governance, key decisions |
| [docs/SCENARIOS.md](docs/SCENARIOS.md) | Greenfield, brownfield and ambiguous scenarios: decomposition, orchestration, validation, evidence |
| [docs/TESTING.md](docs/TESTING.md) | Testing approach, limitations and trade-offs |
| [docs/ENGINEERING_SUMMARY.md](docs/ENGINEERING_SUMMARY.md) | Final engineering summary: plan & rationale, artifacts, risks, validation, assumptions, limitations |
| [examples/greenfield-url-shortener](examples/greenfield-url-shortener/) | **The service the agents generated** in the live greenfield run (v1.0.0, 165 tests, 99% coverage) + provenance (engineering summary, release manifest, git history) |
| [docs/REQUIREMENTS_MAP.md](docs/REQUIREMENTS_MAP.md) | Each assignment requirement → where and how it is implemented |

## Setup

Requirements: Python ≥ 3.11, git, an Anthropic API key (for real agent runs; the test-suite
runs offline).

```bash
make setup                      # python3 -m venv .venv && pip install -e ".[dev]"
cp .env.example .env            # then put your key in .env (never in .env.example)
#   ANTHROPIC_API_KEY=sk-ant-...
#   ANTHROPIC_WORKSPACE_ID=...  # only for org-level keys not scoped to a workspace
make test                       # offline suite: real engine, git, pytest; scripted agents
```

## Explore the recorded live runs (no API key needed)

The three scenarios were run live; their complete state (audit trails, approvals, agent
transcripts, artifacts, per-node diffs, releases) is committed as a portable snapshot in
[`demo-state/`](demo-state).

```bash
make setup
make demo            # restores demo-state -> .sdlc-demo, opens http://127.0.0.1:8765
# or via CLI:
.venv/bin/sdlc restore --from demo-state --home .sdlc-demo --force
SDLC_HOME=.sdlc-demo .venv/bin/sdlc runs
SDLC_HOME=.sdlc-demo .venv/bin/sdlc status run_9f7e81be929b
SDLC_HOME=.sdlc-demo .venv/bin/sdlc audit run_9f7e81be929b --verify
SDLC_HOME=.sdlc-demo .venv/bin/sdlc lineage run_9f7e81be929b release
```

| Run | Scenario | Result |
|---|---|---|
| `run_c856232d1346` | greenfield | v1.0.0 · 165 tests · 99.15 % cov |
| `run_2e7440e535cd` | brownfield | v1.1.0 · 338 tests · 99.06 % cov |
| `run_9f7e81be929b` | ambiguous (+ clarification + mid-run amendment) | v1.2.0 · 404 tests · 99.78 % cov |

## Run the three scenarios

```bash
make dashboard                  # optional: oversight UI at http://127.0.0.1:8765

# 1) Greenfield: build the URL shortener from scratch
.venv/bin/sdlc run scenarios/greenfield.yaml --approve console
# 2) Brownfield: custom aliases + expiry + bug investigation on the greenfield output
.venv/bin/sdlc run scenarios/brownfield.yaml --approve console
# 3) Ambiguous: vague ask -> clarification -> mid-run amendment -> re-plan
.venv/bin/sdlc run scenarios/ambiguous.yaml --approve console
```

`--approve` decides who answers checkpoints:
* `console`: prompts in this terminal.
* `dashboard`: waits for the web UI or `sdlc approve`.
* `auto`: auto-approves only what the policy marks auto-approvable. **Release and policy
  exceptions always need a human.**

Each run prints a live, colour-coded timeline and ends with metrics and the path to its
`ENGINEERING_SUMMARY.md`. A run typically costs a few US dollars in API usage. A hard budget
(`budgets.max_run_cost_usd` in `policies/default.yaml`) safe-stops it if exceeded.

### Oversight commands

```bash
sdlc runs                               # list runs
sdlc status [RUN]                       # node-by-node state, pending approvals
sdlc approvals -v                       # pending checkpoints with payloads
sdlc approve APR [-m note] [--answer Q-1='30 days'] [--action retry|skip|abort]
sdlc reject  APR -m "what must change"  # the agent retries with your feedback
sdlc stop RUN                           # safe stop: roll back in-flight work, keep state
sdlc resume RUN                         # continue a stopped run
sdlc amend RUN "new requirement text"   # change the requirement -> re-plan
sdlc metrics [RUN]                      # reliability metrics (one run or portfolio)
sdlc audit RUN --verify                 # verify the hash-chained audit trail
sdlc lineage RUN release                # artifact lineage tree
sdlc graph RUN                          # Mermaid diagram of the (runtime-expanded) DAG
sdlc report RUN -o summary.md           # final engineering summary for a run
```

### Using the generated service

```bash
cd .sdlc/runs/<run_id>/repo             # the generated, git-versioned product
../../../../.venv/bin/python -m pytest -q
../../../../.venv/bin/python -m uvicorn app.main:app --port 8000
ls ../release/                          # tagged release bundle + manifest + release notes
```

## Repository layout

```
sdlc/
  domain/        graph + data model          engine/        executor, re-planner, gates
  governance/    policy, approvals, audit    agents/        loop, tools, prompts, schemas, handlers
  llm/           providers (anthropic, openai-compatible, fake)
  checks/        pytest/coverage/lint/contract/codebase index
  pipelines/     SDLC graph templates + deterministic steps
  workspace/     git worktree workspace       observability/ metrics, reports
  dashboard/     FastAPI API + static SPA     cli.py
policies/default.yaml   guardrails, budgets, autonomy boundaries
scenarios/*.yaml        greenfield / brownfield / ambiguous
tests/                  offline unit + end-to-end tests (scripted agents)
```
