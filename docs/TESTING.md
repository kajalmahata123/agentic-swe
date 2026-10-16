# Testing approach, limitations and trade-offs

## 1. Two systems, two kinds of testing

| System under test | How it is tested |
|---|---|
| **The orchestrator** (this repository) | An offline pytest suite (`make test`). It runs the *real* engine, policy guard, git worktrees, approvals, audit chain and deterministic gates, with LLM calls replaced by a **scripted provider** (`sdlc/llm/fake.py`, `tests/fake_agents.py`). Agents act through the real tool layer, so their writes are policy-checked, land in real worktrees and are really compiled and tested. |
| **The generated service** (the URL shortener) | Tested *by the orchestrator* at runtime: the test-author agent writes FR-traced pytest suites, and the `verify` gate runs them with coverage, lint, API-contract conformance and traceability checks. Evidence is recorded per run (`verification_report` artifact, `ENGINEERING_SUMMARY.md`). |

## 2. Orchestrator test-suite

| File | What it proves |
|---|---|
| `tests/test_e2e_greenfield.py::test_greenfield_full_lifecycle` | Full lifecycle end to end, with injected faults: blocking ambiguity → **clarification loop** (requirements regenerated); runtime **graph expansion** (4 WPs, 3 waves, WP-1 ‖ WP-3 in parallel); WP-2 writes non-compiling code → **exit gate fails → worktree rollback → retry → recovered (MTTR)**; `requirements.txt` change → **change-control approval**; tests expose a real product defect → **verify fails → repair loop** (`repair-1 → verify-2`) → passes; human **release approval** (never auto); tag + bundle + manifest with sha256; **audit chain verifies, and tampering is detected**. |
| `…::test_design_rejection_feeds_back_to_architect` | A human **rejects the design** with a comment → attempt rolled back → architect re-runs with the reviewer's feedback → approved. |
| `tests/test_e2e_replan_and_control.py::test_amendment_mid_run_replans_and_rolls_back_workspace` | A requirement **amendment arrives after verification**: requirements → design → plan are invalidated; `main` is **reset** past the stale merges; the planner regenerates the graph with a new WP; tests, repair and verify re-run; one release is cut from the re-planned code; the changed design required a **fresh approval** (approvals bound to content hashes). |
| `…::test_safe_stop_then_resume` | **Safe stop** while waiting for design approval → node parked, run `stopped`; **resume** in a new engine instance completes the run. |
| `…::test_brownfield_on_greenfield_output` | **Brownfield** pipeline seeded from a greenfield run: AST codebase index + baseline test run, impact analysis gate, then the full delivery path. |
| `…::test_budget_exhaustion_safe_stops` | Cost **budget** exceeded → safe stop with reason. |
| `tests/test_governance.py` | Policy rules (dangerous code, secrets, privacy CMP-001, protected paths, dependency allow-list, change budget, command allow-list); tool guard (write scope, path escape, `.git`, unique edits, schema-validated submit, credential stripping, audited denials); approvals (content binding, rejection needs a reason, double decisions refused, **auto mode never approves release/policy exceptions**, supersede); audit chain (deletion → gap detected); DAG validation (cycles, unknown deps, rollback of invalid mutations, levels). |
| `tests/test_llm_providers.py` | Anthropic adapter against a mocked SSE stream: request shape (adaptive thinking, effort, prompt-cache breakpoints, streaming), tool-call parsing, usage/cost accounting, error mapping (401 → fatal, 429/5xx/529 → transient, refusal → fallback); OpenAI-compatible adapter. |

Run: `make test` (≈ 30–60 s; no network). Lint: `make lint`.

## 3. Gates applied to agent output at runtime

| Stage | Deterministic gates |
|---|---|
| Requirements | schema-valid; unique ids; answered questions no longer blocking |
| Impact analysis | every "modified" file exists in the indexed codebase |
| Design | schema; WP ids unique; dependencies resolve and are acyclic; **disjoint file ownership**; explicit paths; tests not owned by implementers; paths allowed; **every must-FR assigned**; non-allow-listed deps → change approval; **human design approval** |
| Implementation | owned files created; compiles; diff policy scan; write scope |
| Integration | compiles; app imports (warn) |
| Tests | every must-FR referenced by a test; suite runs; failures must be explained as product defects (otherwise the agent must fix its tests) |
| Verify (repairable) | compile; lint (correctness rules); **tests pass**; **coverage ≥ 80%**; **API contract**: every approved endpoint is implemented (routes introspected from the real app); **FR traceability**; full-tree policy scan |
| Security review (repairable) | no open high/critical findings |
| Docs | README + API reference exist; every contract endpoint documented |
| Release gate | verification passed on the *current* head; coverage; design approved; security clear; docs present; **human release approval** |
| Publish | the approved commit is still the head; tag + archive + sha256 manifest; rolls back tag/bundle on failure |

## 4. Limitations

* **Shared interpreter sandbox.** Agent commands run in the orchestrator's virtualenv, with a
  scrubbed environment, no shell, an allow-list and timeouts. That is not OS-level isolation.
  Production would run each attempt in a container or microVM with no network egress.
  Dependencies an agent declares must already be installed (the allow-list mirrors what is).
* **Single-host state.** SQLite (WAL) is shared by the CLI and the dashboard on one machine.
  A multi-host deployment would move state to Postgres and events to a log/stream, and run
  workers behind a queue.
* **Non-determinism of LLM output.** Two runs of the same scenario produce different (but
  gate-verified) code. Gates check *conformance* (contract, traceability, coverage, policy),
  not optimality. The human design and release approvals remain the quality backstop.
* **Gates are as strong as the tests.** Coverage and FR traceability show tests exist and run.
  They can't prove the tests are *meaningful*. Mitigations: separation of duties (test author
  ≠ implementer), repair edits to tests need human approval, and an independent security review.
* **Regex/AST policy scanning** is a fast first line, not a full SAST (no taint analysis).
  A production setup would add Bandit/Semgrep and dependency vulnerability scanning to the
  same gate.
* **Docs after repairs** are updated by the repair agent itself (docs are in its write scope and its prompt requires it), but this is not mechanically verified beyond the endpoint-coverage check.
  loop changes code but doesn't re-run the docs agent. The release gate checks docs exist
  and cover every endpoint, but not every detail.
* **Dashboard auth** is a shared token (optional) bound to localhost. Real deployments need
  SSO/RBAC (e.g. only release managers may approve releases).
* **Cost accounting** uses list prices from `sdlc/llm/pricing.py`, so it's an estimate.

## 5. Trade-offs

| Choice | Benefit | Cost |
|---|---|---|
| Own agent loop vs. agent framework | Full control of tools, policy, audit, budget; no hidden harness | More code to own (small: ~150 lines) |
| Git worktree per attempt | Exact rollback, isolation of parallel agents, conflicts surface early | Merge serialisation; disk per attempt (cleaned up) |
| Repair loops vs. retrying implementation | Targeted fixes; keeps passing work | Graph grows at runtime (bounded by `max_repair_cycles`) |
| Strict file ownership in the design | Safe parallelism without locking | Architect must plan integration points (e.g. `app/main.py`) explicitly |
| Approvals bound to content hash | No stale approvals after re-planning | More approval prompts after amendments (mitigated by `auto` for low-risk kinds) |
| Scripted fake provider for tests | Fast, free, deterministic tests of the engine | Doesn't test model quality; live scenario runs cover that |
| Opus 5.5 at high effort for every agent | Best first-pass quality → fewer retries | Higher cost per run than a cheaper model; configurable via `SDLC_MODEL` |
