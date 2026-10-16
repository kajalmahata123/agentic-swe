# Scenarios: decomposition, orchestration, validation (live evidence)

All three scenarios were executed **live** with Claude Opus 5.5 agents through the
orchestrator (Anthropic Messages API, own agent loop). Numbers below come from each run's
audit trail (`sdlc metrics <run>`, `sdlc audit <run> --verify`) and artifacts. Per-run
engineering summaries are generated at `.sdlc/runs/<run>/ENGINEERING_SUMMARY.md`.

> **Explore these exact runs yourself, without an API key:** `make demo` restores the recorded
> state from [`demo-state/`](../demo-state) and opens the dashboard at http://127.0.0.1:8765.
> Or use the CLI with `SDLC_HOME=.sdlc-demo`: `sdlc status <run>`, `sdlc audit <run> --verify`,
> `sdlc lineage <run> release`, `sdlc report <run>`, `sdlc graph <run>`.

| Scenario | Run | Input | Outcome |
|---|---|---|---|
| Greenfield | `run_c856232d1346` | well-defined requirement for a new service | **released v1.0.0** |
| Brownfield | `run_2e7440e535cd` | change request + bug report on the v1.0.0 code | **released v1.1.0** |
| Ambiguous | `run_9f7e81be929b` | "make it faster and more trustworthy" + mid-run amendment | **released v1.2.0** |

---

## 1. Greenfield: URL shortener from scratch (`scenarios/greenfield.yaml`)

### Requirement understanding
The requirements agent normalised a one-page brief into **13 FRs** (create, validation,
collision-safe codes, idempotency, redirect, metadata, deactivate, click events, stats,
probes, JSON errors, config/persistence, OpenAPI) and **9 measurable NFRs**. It raised **7
ambiguities**, all correctly judged *non-blocking*:
- Q-1 auth? → none this iteration
- Q-2 deactivated redirect → 410
- Q-3 idempotency scope/TTL
- Q-4 aliases/expiry → out of scope
- Q-5 performance targets
- Q-6 stats window → 30 days / top 10
- Q-7 which IP behind a proxy → socket peer

Their defaults were recorded as decisions, and no human question was needed.

### Decomposition
The architect designed a layered FastAPI service with **7 endpoints, 3 entities, 10 ADRs** and
**5 work packages** with disjoint file ownership (details in [DESIGN.md Part B](DESIGN.md#part-b-url-shortener-design-generated)).
The planner expanded the graph at runtime into 3 waves:

```
wave 1 (parallel): WP-1 core/config/db/errors · WP-2 models+repositories · WP-3 schemas+validation
wave 2:            WP-4 services (codes, lifecycle, redirect/clicks, stats)
wave 3:            WP-5 routers + DI + app factory
→ integrate (join) → tests ‖ docs → verify → security_review → release_gate → publish
```

### Orchestration: what actually happened
| Event | Mechanism exercised |
|---|---|
| WP-1, WP-2, WP-3 ran concurrently in separate git worktrees and merged cleanly | parallel paths + join barrier |
| `requirements.txt` written by WP-1 → **change-control checkpoint** (CHG-001) | protected-path policy |
| WP-4 marked high-risk by the architect → **change approval** before merge | risk-based sign-off |
| WP-5's redirect router reads the client IP → **CMP-001 privacy policy exception**, never auto-approvable. **A human approved it** after confirming the IP is HMAC-SHA256-pseudonymised in the service layer | compliance guardrail + human decision |
| The test author's first suite contained the literal `"shell=True"` (an assertion that it is *never* used) → policy scan **blocked the merge** → worktree **rolled back** → retried with gate feedback → **156 tests, 99.15 % coverage** | exit gate, rollback, bounded retry, MTTR (≈ 490 s) |
| `verify` passed: tests, coverage ≥ 80 %, all 7 contract endpoints present in the real routes, every must-FR traced, full-tree policy scan | deterministic validation |
| Security reviewer found **1 high**: mutating/analytics endpoints unauthenticated → gate failed → **repair loop inserted at runtime** (`repair-1 → verify-2 → security_review-2`) → repair added fail-closed API-key auth (its edits to tests went through the guarded-path change checkpoint) → 165/165 tests, 99.19 % → re-review: 0 high, 4 medium, 6 low | dynamic re-planning (repair loop) |
| Release checklist all green → **human release sign-off** → tag `v1.0.0`, `git archive` bundle, `manifest.json` (commit, sha256, approvals, checklist, SBOM), release notes | release governance |

**Metrics:** 20 attempts, attempt success 0.905, 1 retry, 1 rollback, 1 repair loop,
6 approvals (2 human, 4 auto under `--approve auto`), MTTR ≈ 490 s, e2e ≈ 46 min, LLM cost
≈ $8.92. Audit chain verified intact (502 events).

### Validation of the generated product, independently of the agents
The released service was started with uvicorn and exercised over HTTP:
- `/healthz` 200 and `/readyz` 200
- POST without key → 401; with key → 201
- idempotent replay → 201, same key with a different body → 409
- `javascript:` URL → 422
- redirect → 307 with the correct `Location`
- stats → total, per-day and referrers correct
- DELETE → 204, then redirect → 410; unknown code → 404
- the database contains only 64-character IP hashes

### Lessons fed back into the orchestrator
Each one is now covered by a regression test:
1. Code-shape policy rules now ignore string literals and comments (tokenizer-based); secret
   rules still scan strings.
2. The privacy rule follows imports into the service layer, so a router forwarding the IP to a
   hashing service isn't a false positive.
3. A high security finding is now a **human risk decision** (accept with a recorded waiver,
   or reject → repair loop) instead of an automatic fix that may contradict an approved ADR.
4. Repair agents may update the docs affected by their fix.
5. Mid-stream connection drops are classified as transient.
6. The release checklist names the actual design approver.

---

## 2. Brownfield: change request on existing code (`scenarios/brownfield.yaml`)

**Input:**
- **CR-142:** custom aliases (format rules, reserved words → 422, taken → 409) and optional
  expiry (`expires_at`, expired → 410, metadata reports `is_expired`).
- **BUG-77:** "upper-case schemes rejected, `javascript:` accepted once".
- **Constraints:** backwards compatibility.

The seed is the released v1.0.0 repository from the greenfield run.

### Codebase reasoning
* Deterministic index: 46 files, 3,284 Python LOC, 7 routes, 3 ORM models, baseline test run.
* Impact analysis: **18 impacted files**, API and schema changes (`links.expires_at` added;
  idempotency hash semantics widened), data flows, regression areas, and **8 existing defects**:
  * **BUG-77 investigated and found not reproducible.** The validator already accepts
    upper-case schemes and rejects `javascript:`. Regression tests are still required.
  * Latent issues the change would expose:
    - The base62-only lookup check would make aliases containing `-`/`_` unreachable.
    - The idempotency hash covers only the URL.
    - An alias conflict would be mis-retried as a code collision → 503.
    - `metrics` is missing from the reserved words.
    - `GoneError`'s message is deactivation-specific.
    - `create_all` can't add a column to an existing table (migration gap).
  * **Stale docs** from the greenfield security repair ("API says no auth, but auth is
    enforced"). This directly motivated lesson 4 above.

### Orchestration highlights
* 14 FRs / 8 NFRs, 0 blocking ambiguities (9 defaults recorded).
* The design stage hit **two real transport failures**: a mid-stream disconnect, then a read
  timeout while the host was asleep overnight. Bounded retries rolled back each attempt and
  attempt 3 (escalated effort) succeeded. This is the reliability layer working under real
  faults (lesson 5).
* Parallel implementation of 6 work packages → integrate → tests ‖ docs → verify →
  security → release.
* The test author's first attempt exceeded the 30-minute node timeout. Two live runs and a
  regression suite were competing for one laptop's CPU, which slowed every pytest
  iteration. The engine treated it as a transient failure, discarded the worktree and
  restarted the attempt. The test stage's timeout is now 60 minutes for future runs.

### Result
* **Released v1.1.0 after a human release sign-off:** 338/338 tests, 99.06 % coverage, contract
  and FR traceability complete, security 0 high (3 medium, 3 low).
* **Metrics:** 22 attempts, 3 retries (all from the network faults), 1 rollback, 3 approvals,
  ≈ $13.13. Audit chain intact (514 events).
* Summary: [`generated/brownfield/`](../generated/brownfield/).

---

## 3. Ambiguous: vague business ask + mid-run change (`scenarios/ambiguous.yaml`)

**Input:** *"Marketing is launching a big campaign next week… Make the link service faster and
more trustworthy before then."* (brownfield, seeded with v1.0.0).

### Requirement understanding & human clarification
* First pass: 8 FRs, 9 NFRs, 8 ambiguities, **2 blocking**, put to the product owner:
  * **Q-1:** measurable targets (peak RPS, p95/p99, availability)? → *human answered:
    "Modest: up to ~50 RPS, p95 < 100 ms, best effort."*
  * **Q-2:** production database/topology (SQLite vs PostgreSQL/MySQL + driver)? → *human
    answered: "Keep SQLite, harden it (WAL, busy_timeout, configurable pool); no new driver;
    document the PostgreSQL path."*
* The answers were folded into the brief → `requirements` **invalidated and regenerated** →
  8 FRs, 11 NFRs, 0 blocking → `clarify` re-ran automatically (output changed) and passed.

### Design (human-approved)
The architect's design matched the answers:
- SQLite hardening (WAL, `synchronous=NORMAL`, busy_timeout, configurable pool)
- a lean redirect hot path (one narrow SELECT, no cache)
- SQL-side daily stats aggregation with bounded memory
- HEAD on the health probes; case-sensitive codes on MySQL; a startup tuning log

That's 7 backwards-compatible endpoints, 7 work packages (5 parallel) and 8 ADRs.
**Approved by a human.**

### Mid-run amendment → dynamic re-planning
Immediately after design approval, the product owner amended the requirement: *"campaign
links must stop working automatically when the campaign ends (31 Dec 23:59 UTC), and we need
clicks per hour during the TV ad slots."* The orchestrator:
1. applied the amendment to the requirement brief (new hash, audited),
2. **invalidated** `impact_analysis` and `requirements` (consumers of the brief),
3. blocked everything downstream through the *ancestors-settled* entry gate, so the
   now-stale approved design could not be planned or implemented,
4. regenerated the impact analysis and requirements (13 FRs, 13 NFRs), re-ran `clarify`, and
   produced a **new design that needed a fresh approval**, because approvals are bound to
   content hashes and the old approval did not carry over.

### Result
* The final specification carries both the clarified intent and the amendment. Its **13 FRs**
  include automatic link expiry enforced on redirect, setting/changing/clearing expiry,
  **hourly click counts**, and an idempotent startup schema upgrade for existing databases,
  next to the hardening work (HEAD probes, contention-tolerant redirects, configurable pool,
  bounded-memory stats).
* Verification initially failed on 3 tests → **repair loop** (`repair-1 → verify-2`) fixed two
  root causes → **404/404 tests, 99.78 % coverage, 11/11 must-FRs traced**; security 0 high.
* **Released v1.2.0** after a human release sign-off. The re-planned design was approved by the
  delegated approver (recorded as `human:claude-on-behalf-of-kajalmahata`, so delegation is
  visible in the audit trail).
* **Metrics:** 29 attempts, 5 re-plans, 1 repair loop, 1 retry, 1 rollback, 8 approvals,
  ≈ $21.44. Audit chain intact (778 events).
* **Lineage** (`sdlc lineage run_9f7e81be929b release`): release ← release_readiness ←
  verification_report v2 ← design v2 ← requirements_spec v3 ← requirement **v3** (original
  brief → + clarification answers → + amendment).
* Summary: [`generated/ambiguous/`](../generated/ambiguous/).

---

## 4. Offline reproductions (no API key needed)

Every control path above also runs deterministically in the offline suite with scripted
agents and injected faults (see [TESTING.md](TESTING.md)): clarification loop, parallel waves,
compile-failure rollback/retry, change approvals, verify → repair loop, design rejection with
feedback, mid-run amendment with workspace reset, safe-stop/resume, brownfield seeding, budget
stop, security waiver vs. reject-to-repair.
