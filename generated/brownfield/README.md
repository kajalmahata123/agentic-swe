# brownfield: `run_2e7440e535cd`

**Status:** succeeded · **Release:** v1.1.0

## Requirement given to the agents

> Change request CR-142 for the existing URL shortener service:
> 
> 1. Custom aliases: when creating a link a client may choose its own short code
>    (3-32 characters, letters, digits, '-' and '_'). Reserved words (api, docs, redoc, healthz,
>    readyz, metrics, openapi.json) are rejected with 422; an alias that is already taken returns 409.
> 2. Link expiration: creation accepts an optional `expires_at` (ISO-8601, must be in the future).
>    Redirecting an expired link returns 410 Gone; link metadata reports whether it is expired.
> 
> Bug report BUG-77 (from support): "A link created with an upper-case scheme like HTTP://Example.com
> was rejected, and we believe a `javascript:` URL was accepted once." Investigate the existing code,
> fix whatever is actually wrong, and add regression tests either way.
> 
> Constraints: existing endpoints and response fields must keep working (backwards compatible);
> existing tests must keep passing; the database schema change must be explicit in the design.

## Where to look

| Path | What it is |
|---|---|
| [`code/`](code/) | the generated service at its release tag: app, tests, docs |
| [`ENGINEERING_SUMMARY.md`](ENGINEERING_SUMMARY.md) | final engineering summary of the run |
| [`artifacts/`](artifacts/) | every artifact version: requirements spec, impact analysis, design (API, ADRs, work packages), plan, test/verification/security reports, release readiness |
| [`transcripts/`](transcripts/) | every agent attempt: turns, tool calls, results, cost (JSONL) |
| [`GIT_HISTORY.md`](GIT_HISTORY.md) | commit + merge per node attempt |
| [`approvals.json`](approvals.json) · [`decisions.json`](decisions.json) | decisions |
| [`audit_log.jsonl`](audit_log.jsonl) | the full hash-chained event log |
| [`metrics.json`](metrics.json) | reliability metrics |
| [`release/`](release/) | release bundle, manifest (sha256, approvals, SBOM), release notes |
| [`console.log`](console.log) | the live CLI output of the run |

## Nodes

| Node | Stage | Kind | Status | Attempts | Cost |
|---|---|---|---|---|---|
| intake | requirements | action | succeeded | 1 | $0.00 |
| codebase_scan | requirements | check | succeeded | 1 | $0.00 |
| impact_analysis | requirements | agent | succeeded | 1 | $0.81 |
| requirements | requirements | agent | succeeded | 1 | $0.61 |
| clarify | requirements | human | succeeded | 1 | $0.00 |
| design | design | agent | succeeded | 3 | $2.62 |
| plan | planning | planner | succeeded | 1 | $0.00 |
| impl:WP-1 | implementation | agent | succeeded | 1 | $0.21 |
| impl:WP-2 | implementation | agent | succeeded | 1 | $0.29 |
| impl:WP-3 | implementation | agent | succeeded | 1 | $0.41 |
| impl:WP-6 | implementation | agent | succeeded | 1 | $1.45 |
| impl:WP-4 | implementation | agent | succeeded | 1 | $0.42 |
| impl:WP-5 | implementation | agent | succeeded | 1 | $0.30 |
| integrate | implementation | join | succeeded | 1 | $0.00 |
| docs | documentation | agent | succeeded | 1 | $0.83 |
| tests | testing | agent | succeeded | 2 | $4.68 |
| verify | testing | check | succeeded | 1 | $0.00 |
| security_review | testing | agent | succeeded | 1 | $0.52 |
| release_gate | release | check | succeeded | 1 | $0.00 |
| publish | release | action | succeeded | 1 | $0.00 |

## Human & policy checkpoints

| Kind | Node | Decision | By | Comment |
|---|---|---|---|---|
| design | design | approved | policy:auto-approve | auto-approved: kind is auto-approvable under policy.autonomy |
| change | impl:WP-4 | approved | policy:auto-approve | auto-approved: kind is auto-approvable under policy.autonomy |
| release | release_gate | approved | human:kajalmahata | Release approved: all readiness checks passed, 0 high security findings. |

## Metrics

```json
{
  "events": 514,
  "e2e_latency_s": 32801.28,
  "nodes_succeeded": 20,
  "nodes_failed": 0,
  "attempts": 23,
  "attempt_success_rate": 0.87,
  "first_pass_yield": 0.9,
  "retries": 3,
  "failed_attempts": 3,
  "failure_kinds": {
    "error": 2,
    "transient": 1
  },
  "rollbacks": 1,
  "mttr_s": 13676.82,
  "recoveries": 2,
  "repair_loops": 0,
  "replans": 0,
  "reused_nodes": 0,
  "safe_stops": 0,
  "approvals_requested": 3,
  "approvals_rejected": 0,
  "approval_wait_s_avg": 576.71,
  "policy_denials": 0,
  "agent_turns": 157,
  "llm_cost_usd": 13.134,
  "input_tokens": 344,
  "output_tokens": 263162,
  "cache_hit_ratio": 0.876
}
```
