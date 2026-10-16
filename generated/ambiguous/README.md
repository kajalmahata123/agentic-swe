# ambiguous: `run_9f7e81be929b`

**Status:** succeeded · **Release:** v1.2.0

## Requirement given to the agents

> Marketing is launching a big campaign next week and will put our short links on billboards and
> in a TV ad. Make the link service faster and more trustworthy before then.

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
| impact_analysis | requirements | agent | succeeded | 1 | $1.53 |
| requirements | requirements | agent | succeeded | 1 | $1.47 |
| clarify | requirements | human | succeeded | 1 | $0.00 |
| design | design | agent | succeeded | 1 | $3.91 |
| plan | planning | planner | succeeded | 1 | $0.00 |
| impl:WP-1 | implementation | agent | succeeded | 2 | $1.35 |
| impl:WP-3 | implementation | agent | succeeded | 1 | $0.84 |
| impl:WP-5 | implementation | agent | succeeded | 1 | $0.61 |
| impl:WP-2 | implementation | agent | succeeded | 1 | $0.46 |
| impl:WP-7 | implementation | agent | succeeded | 1 | $1.99 |
| impl:WP-4 | implementation | agent | succeeded | 1 | $0.65 |
| impl:WP-6 | implementation | agent | succeeded | 1 | $0.58 |
| integrate | implementation | join | succeeded | 1 | $0.00 |
| docs | documentation | agent | succeeded | 1 | $1.09 |
| tests | testing | agent | succeeded | 1 | $4.79 |
| repair-1 | implementation | agent | succeeded | 1 | $1.47 |
| verify | testing | check | skipped | 1 | $0.00 |
| verify-2 | testing | check | succeeded | 1 | $0.00 |
| security_review | testing | agent | succeeded | 1 | $0.70 |
| release_gate | release | check | succeeded | 1 | $0.00 |
| publish | release | action | succeeded | 1 | $0.00 |

## Human & policy checkpoints

| Kind | Node | Decision | By | Comment |
|---|---|---|---|---|
| clarification | clarify | approved | human:kajalmahata | Product owner answers |
| design | design | approved | human:kajalmahata | Approved: matches the answered targets (modest load, hardened SQLite). |
| design | design | approved | human:claude-on-behalf-of-kajalmahata | Delegated routine approval: all exit gates passed; reviewed summary. |
| change | impl:WP-1 | approved | human:claude-on-behalf-of-kajalmahata | Delegated routine approval: all exit gates passed; reviewed summary. |
| change | impl:WP-4 | approved | human:claude-on-behalf-of-kajalmahata | Delegated routine approval: all exit gates passed; reviewed summary. |
| change | tests | approved | human:claude-on-behalf-of-kajalmahata | Delegated routine approval: all exit gates passed; reviewed summary. |
| change | repair-1 | approved | human:claude-on-behalf-of-kajalmahata | Delegated routine approval: all exit gates passed; reviewed summary. |
| release | release_gate | approved | human:kajalmahata | Release approved: all readiness checks passed; amendment scope delivered. |

## Metrics

```json
{
  "events": 778,
  "e2e_latency_s": 32932.15,
  "nodes_succeeded": 22,
  "nodes_failed": 0,
  "attempts": 30,
  "attempt_success_rate": 0.933,
  "first_pass_yield": 0.964,
  "retries": 1,
  "failed_attempts": 2,
  "failure_kinds": {
    "transient": 1,
    "gate": 1
  },
  "rollbacks": 1,
  "mttr_s": 244.49,
  "recoveries": 1,
  "repair_loops": 1,
  "replans": 5,
  "reused_nodes": 0,
  "safe_stops": 0,
  "approvals_requested": 8,
  "approvals_rejected": 0,
  "approval_wait_s_avg": 3321.48,
  "policy_denials": 0,
  "agent_turns": 236,
  "llm_cost_usd": 21.4357,
  "input_tokens": 512,
  "output_tokens": 465535,
  "cache_hit_ratio": 0.914
}
```
