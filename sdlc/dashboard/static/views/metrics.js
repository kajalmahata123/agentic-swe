// Metrics tab: reliability / governance / cost KPIs derived from the audit trail, plus
// bar charts for per-stage latency and failure kinds, and portfolio-wide aggregates.

import { api } from "../api.js";
import { h, clear, sig, fmtNum, fmtPct, fmtUsd, fmtDuration, humanize, isNil, DASH } from "../util.js";

const GROUPS = [
  {
    title: "Flow & reliability",
    tiles: [
      { key: "e2e_latency_s", label: "End-to-end latency", fmt: fmtDuration, hint: "run.started → run.finished" },
      { key: "attempt_success_rate", label: "Attempt success rate", fmt: (v) => fmtPct(v), hint: "succeeded attempts / all attempts", good: (v) => v >= 0.8 },
      { key: "first_pass_yield", label: "First-pass yield", fmt: (v) => fmtPct(v), hint: "nodes that succeeded on attempt 1", good: (v) => v >= 0.8 },
      { key: "retries", label: "Retries", fmt: fmtNum, hint: "attempts beyond the first" },
      { key: "rollbacks", label: "Rollbacks", fmt: fmtNum, hint: "discarded attempts + workspace resets" },
      { key: "mttr_s", label: "MTTR", fmt: fmtDuration, hint: "mean time from failure to recovery" },
      { key: "repair_loops", label: "Repair loops", fmt: fmtNum, hint: "repair → re-verify cycles inserted" },
      { key: "replans", label: "Re-plans", fmt: fmtNum, hint: "invalidations after clarifications / amendments" },
      { key: "reused_nodes", label: "Reused nodes", fmt: fmtNum, hint: "memoised results kept on re-plan" },
      { key: "safe_stops", label: "Safe stops", fmt: fmtNum, hint: "times the run halted safely" },
    ],
  },
  {
    title: "Governance",
    tiles: [
      { key: "approvals_requested", label: "Approvals requested", fmt: fmtNum },
      { key: "approvals_rejected", label: "Approvals rejected", fmt: fmtNum, warn: (v) => v > 0 },
      { key: "approval_wait_s_avg", label: "Avg approval wait", fmt: fmtDuration, hint: "time a human checkpoint stayed open" },
      { key: "policy_denials", label: "Policy denials", fmt: fmtNum, hint: "tool calls / commands blocked by policy", warn: (v) => v > 0 },
    ],
  },
  {
    title: "Agents & cost",
    tiles: [
      { key: "agent_turns", label: "Agent turns", fmt: fmtNum },
      { key: "llm_cost_usd", label: "LLM cost", fmt: fmtUsd },
      { key: "cache_hit_ratio", label: "Prompt-cache hit ratio", fmt: (v) => fmtPct(v), hint: "cache-read tokens / all input tokens" },
      { key: "_tokens", label: "Tokens (in / out)", fmt: (_, m) => (isNil(m.input_tokens) ? DASH : `${fmtNum(m.input_tokens)} / ${fmtNum(m.output_tokens)}`) },
      { key: "attempts", label: "Attempts", fmt: fmtNum },
      { key: "events", label: "Audit events", fmt: fmtNum },
    ],
  },
];

function tile(def, m) {
  const v = def.key.startsWith("_") ? null : m[def.key];
  const text = def.fmt(v, m);
  let state = "";
  if (!isNil(v) && def.good) state = def.good(v) ? " tile-good" : " tile-warn";
  if (!isNil(v) && def.warn && def.warn(v)) state = " tile-warn";
  return h(
    "div",
    { class: `tile${state}`, title: def.hint || null },
    h("div", { class: "tile-label" }, def.label),
    h("div", { class: "tile-value" }, text),
    def.hint ? h("div", { class: "tile-hint" }, def.hint) : null,
  );
}

function barChart(title, data, fmt, { emptyText = "No data yet", tone = "" } = {}) {
  const entries = Object.entries(data || {}).filter(([, v]) => !isNil(v));
  const wrap = h("figure", { class: "chart" }, h("figcaption", null, title));
  if (!entries.length) {
    wrap.append(h("div", { class: "muted small chart-empty" }, emptyText));
    return wrap;
  }
  entries.sort((a, b) => b[1] - a[1]);
  const max = Math.max(...entries.map(([, v]) => v)) || 1;
  const total = entries.reduce((a, [, v]) => a + v, 0);
  const list = h("div", { class: "bars", role: "list" });
  entries.forEach(([k, v]) => {
    const pct = Math.max(1.5, (v / max) * 100);
    list.append(
      h(
        "div",
        { class: "bar-row", role: "listitem", "aria-label": `${humanize(k)}: ${fmt(v)}` },
        h("span", { class: "bar-label" }, humanize(k)),
        h("span", { class: "bar-track", "aria-hidden": "true" }, h("span", { class: `bar-fill ${tone}`, style: { width: `${pct}%` } })),
        h("span", { class: "bar-value" }, fmt(v), total ? h("span", { class: "muted" }, ` · ${Math.round((v / total) * 100)}%`) : null),
      ),
    );
  });
  wrap.append(list);
  return wrap;
}

export function createMetricsView(ctx) {
  const el = h("div", { class: "metrics-view" });
  const runHost = h("div");
  const portHost = h("div", { class: "portfolio" });
  el.append(runHost, portHost);
  let lastSig = null;
  let portAt = 0;

  async function loadPortfolio() {
    if (Date.now() - portAt < 15000) return;
    portAt = Date.now();
    try {
      const p = await api.portfolio();
      clear(portHost).append(
        h("h2", { class: "view-title" }, "Portfolio (all runs)"),
        h(
          "div",
          { class: "tiles" },
          tile({ key: "runs", label: "Runs", fmt: fmtNum }, p),
          tile({ key: "run_success_rate", label: "Run success rate", fmt: (v) => fmtPct(v), good: (v) => v >= 0.8 }, p),
          tile({ key: "e2e_latency_s_avg", label: "Avg e2e latency", fmt: fmtDuration }, p),
          tile({ key: "mttr_s_avg", label: "Avg MTTR", fmt: fmtDuration }, p),
          tile({ key: "total_retries", label: "Total retries", fmt: fmtNum }, p),
          tile({ key: "total_rollbacks", label: "Total rollbacks", fmt: fmtNum }, p),
          tile({ key: "llm_cost_usd", label: "Total LLM cost", fmt: fmtUsd }, p),
        ),
      );
    } catch {
      /* portfolio is a nice-to-have; ignore transient failures */
    }
  }

  return {
    el,
    update(data) {
      const m = data.metrics || {};
      const key = sig(m);
      if (key !== lastSig) {
        lastSig = key;
        clear(runHost).append(
          ...GROUPS.map((g) => h("section", { class: "tile-group" }, h("h2", { class: "view-title" }, g.title), h("div", { class: "tiles" }, g.tiles.map((t) => tile(t, m))))),
          h(
            "section",
            { class: "charts" },
            barChart("Latency by stage (sum of node durations)", m.stage_latency_s, fmtDuration),
            barChart("Failed attempts by kind", m.failure_kinds, fmtNum, { emptyText: "No failed attempts — clean run", tone: "bar-bad" }),
          ),
        );
      }
      loadPortfolio();
    },
  };
}
