// Timeline tab: the hash-chained audit trail, polled incrementally (?after=<seq>), with
// category filters, node search, expandable event data and audit-chain verification.

import { api } from "../api.js";
import { h, clear, fmtClock, fmtDate, fmtDuration, fmtUsd, jsonTree, emptyState, shortHash, truncate } from "../util.js";
import { asyncButton, reportError } from "../ui.js";

const CATEGORIES = [
  { id: "node", label: "node.*", icon: "◉" },
  { id: "approval", label: "approval.*", icon: "✋" },
  { id: "replan", label: "replan.*", icon: "⟲" },
  { id: "plan", label: "plan.*", icon: "⎇" },
  { id: "tool", label: "tool.*", icon: "⚒" },
  { id: "gate", label: "gate.*", icon: "⛉" },
  { id: "agent", label: "agent.turn", icon: "✦" },
  { id: "run", label: "run.*", icon: "▶" },
  { id: "other", label: "artifact / decision", icon: "◇" },
];
const KNOWN = new Set(CATEGORIES.map((c) => c.id).filter((c) => c !== "other"));
const MAX_ROWS = 1500;

export function category(type) {
  const head = String(type).split(".")[0];
  return KNOWN.has(head) ? head : "other";
}

function tone(e) {
  const t = e.type;
  const d = e.data || {};
  if (t === "node.failed" || t === "node.attempt_failed" || t === "run.failed") return "bad";
  if (t === "gate.result" && !d.passed) return d.severity === "block" ? "bad" : "warn";
  if ((t === "tool.guard" || t === "tool.command") && d.allowed === false) return "bad";
  if (t === "approval.decided") return d.decision === "approved" ? "ok" : "warn";
  if (t === "approval.requested" || t.startsWith("replan.") || t === "node.rolled_back" || t === "run.stopped") return "warn";
  if (t === "node.succeeded" || t === "node.recovered" || t === "run.finished") return d.error ? "bad" : "ok";
  return "info";
}

/** One-line human summary per event type (mirrors the CLI's live log). */
export function describe(e) {
  const d = e.data || {};
  switch (e.type) {
    case "run.created":
      return `run created (${d.mode || "?"}) with ${(d.nodes || []).length} nodes`;
    case "run.started":
      return d.resumed ? "run resumed" : `run started · ${d.nodes} nodes`;
    case "run.finished":
      return d.error ? `run finished with error: ${truncate(d.error, 140)}` : "run finished";
    case "run.stopped":
      return `safe stop: ${d.reason || ""}`;
    case "node.started":
      return `started (${d.stage || ""}/${d.kind || ""})`;
    case "node.attempt_started":
      return `attempt ${e.attempt}${d.model ? ` on ${d.model}` : ""}`;
    case "node.attempt_failed":
      return `attempt ${e.attempt} failed [${d.kind || "error"}]: ${truncate(d.error, 160)}`;
    case "node.succeeded":
      return `${d.summary || "succeeded"} · ${fmtDuration(d.duration_s)}${d.cost_usd ? ` · ${fmtUsd(d.cost_usd)}` : ""}`;
    case "node.failed":
      return `failed: ${truncate(d.error, 160)}`;
    case "node.rolled_back":
      return `rolled back: ${d.detail || d.reason || ""}`;
    case "node.recovered":
      return `recovered after ${fmtDuration(d.recovery_s)}`;
    case "node.superseded":
      return `superseded by ${d.by}`;
    case "node.skipped":
      return `skipped: ${truncate(d.reason, 140)}`;
    case "node.reused":
      return "reused memoised result (inputs unchanged)";
    case "gate.result":
      return `${d.passed ? "✓" : "✗"} ${d.name}${d.detail && d.detail !== "ok" ? ` — ${truncate(d.detail, 140)}` : ""}`;
    case "gate.entry": {
      const res = d.results || [];
      const failed = res.filter((r) => !r.passed);
      return failed.length ? `entry gate blocked: ${failed.map((r) => r.name).join(", ")}` : `entry gate passed (${res.length} checks)`;
    }
    case "approval.requested":
      return `✋ ${d.kind} approval requested: ${truncate(d.summary, 140)}`;
    case "approval.decided":
      return `${d.kind} ${d.decision} by ${d.actor}${d.comment ? ` — “${truncate(d.comment, 100)}”` : ""}`;
    case "plan.expanded":
      return `plan expanded: ${(d.added || []).join(", ")}`;
    case "replan.invalidated":
      return `re-plan invalidated ${(d.nodes || []).join(", ") || "nothing"} — ${truncate(d.reason, 100)}`;
    case "replan.repair_loop":
      return `repair loop #${d.cycle} inserted: ${(d.inserted || []).join(" → ")}`;
    case "replan.workspace_reset":
      return `main reset to ${shortHash(d.to_sha)} (${truncate(d.reason, 80)})`;
    case "tool.guard":
      return `${d.allowed === false ? "DENIED" : "allowed"} ${d.tool} ${d.path || ""}${d.rule ? ` [${d.rule}]` : ""}${d.reason ? ` — ${d.reason}` : ""}`;
    case "tool.command":
      return `${d.allowed === false ? "DENIED" : "ran"} \`${truncate(d.command, 120)}\`${d.reason ? ` — ${d.reason}` : ""}`;
    case "agent.turn":
      return `${d.role || "agent"} turn ${d.turn} · ${d.model || ""} · ${d.input_tokens ?? 0}/${d.output_tokens ?? 0} tok · ${fmtUsd(d.cost_usd)}${(d.tools || []).length ? ` · ${(d.tools || []).join(", ")}` : ""}`;
    case "artifact.stored":
      return `stored ${d.name} v${d.version} (${shortHash(d.hash)})`;
    case "decision.recorded":
      return `${d.actor}: ${truncate(d.title, 140)}`;
    default: {
      const keys = Object.keys(d);
      return keys.length ? truncate(keys.map((k) => `${k}=${typeof d[k] === "object" ? "…" : d[k]}`).join(" "), 160) : "";
    }
  }
}

function iconFor(e) {
  return (CATEGORIES.find((c) => c.id === category(e.type)) || CATEGORIES[CATEGORIES.length - 1]).icon;
}

function eventRow(e) {
  const det = h("details", { class: `ev ev-${tone(e)} cat-${category(e.type)}` });
  const summary = h(
    "summary",
    null,
    h("span", { class: "ev-time mono", title: fmtDate(e.ts) }, fmtClock(e.ts)),
    h("span", { class: "ev-seq mono muted" }, `#${e.seq}`),
    h("span", { class: "ev-icon", "aria-hidden": "true" }, iconFor(e)),
    h("span", { class: "ev-type mono" }, e.type),
    e.node_id ? h("span", { class: "ev-node mono" }, e.node_id) : null,
    e.attempt ? h("span", { class: "ev-att muted" }, `a${e.attempt}`) : null,
    h("span", { class: "ev-desc" }, describe(e)),
  );
  det.append(summary);
  det.addEventListener(
    "toggle",
    () => {
      if (det.open && !det.querySelector(".ev-body")) {
        det.append(
          h(
            "div",
            { class: "ev-body" },
            jsonTree(e.data || {}, { openDepth: 2, label: "data" }),
            h("div", { class: "muted small mono ev-hash" }, `hash ${shortHash(e.hash, 16)} ← prev ${shortHash(e.prev_hash, 16)}`),
          ),
        );
      }
    },
  );
  return det;
}

export function createTimelineView(ctx) {
  const ui = (ctx.ui.timeline ||= { events: [], lastSeq: 0, filters: [], search: "" });
  const el = h("div", { class: "timeline-view" });
  const chips = h("div", { class: "chips", role: "group", "aria-label": "Event type filters" });
  const search = h("input", { type: "search", class: "search", placeholder: "Filter by node id or text…", value: ui.search, "aria-label": "Filter events" });
  const verifyOut = h("div", { class: "verify-out", "aria-live": "polite" });
  const verifyBtn = asyncButton("⛓ Verify audit chain", async () => {
    try {
      const res = await api.verifyAudit(ctx.runId);
      clear(verifyOut).append(
        h("div", { class: `banner ${res.ok ? "banner-ok" : "banner-error"}`, role: "status" }, h("strong", null, res.ok ? "✓ Chain intact" : "✗ Chain broken"), h("span", { class: "banner-text" }, res.message)),
      );
    } catch (err) {
      reportError(err, "Verifying audit chain");
    }
  });
  const countEl = h("span", { class: "muted small" });
  const list = h("div", { class: "ev-list", role: "list" });
  el.append(
    h("div", { class: "tl-toolbar" }, chips, h("div", { class: "tl-right" }, search, verifyBtn)),
    verifyOut,
    countEl,
    list,
  );

  let inflight = false;
  let destroyed = false;

  const matches = (e) => {
    if (ui.filters.length && !ui.filters.includes(category(e.type))) return false;
    if (ui.search) {
      const q = ui.search.toLowerCase();
      return (e.node_id || "").toLowerCase().includes(q) || e.type.includes(q) || describe(e).toLowerCase().includes(q);
    }
    return true;
  };

  function renderChips() {
    const counts = {};
    ui.events.forEach((e) => (counts[category(e.type)] = (counts[category(e.type)] || 0) + 1));
    clear(chips);
    const allBtn = h("button", { type: "button", class: `chip${ui.filters.length ? "" : " on"}`, "aria-pressed": ui.filters.length ? "false" : "true" }, `All ${ui.events.length}`);
    allBtn.addEventListener("click", () => {
      ui.filters = [];
      renderChips();
      renderAll();
    });
    chips.append(allBtn);
    CATEGORIES.forEach((c) => {
      const on = ui.filters.includes(c.id);
      const b = h(
        "button",
        { type: "button", class: `chip cat-${c.id}${on ? " on" : ""}`, "aria-pressed": on ? "true" : "false" },
        h("span", { "aria-hidden": "true" }, c.icon),
        ` ${c.label} `,
        h("span", { class: "chip-count" }, String(counts[c.id] || 0)),
      );
      b.addEventListener("click", () => {
        ui.filters = on ? ui.filters.filter((f) => f !== c.id) : [...ui.filters, c.id];
        renderChips();
        renderAll();
      });
      chips.append(b);
    });
  }

  function renderAll() {
    clear(list);
    const shown = ui.events.filter(matches);
    const slice = shown.slice(-MAX_ROWS).reverse();
    slice.forEach((e) => list.append(eventRow(e)));
    countEl.textContent = `${shown.length} event${shown.length === 1 ? "" : "s"}${shown.length > MAX_ROWS ? ` (showing newest ${MAX_ROWS})` : ""} · newest first`;
    if (!shown.length) list.append(emptyState(ui.events.length ? "No events match the filters" : "No events yet"));
  }

  function prepend(newEvents) {
    const shown = newEvents.filter(matches);
    if (!shown.length) return;
    const empty = list.querySelector(".empty");
    if (empty) empty.remove();
    const frag = document.createDocumentFragment();
    shown
      .slice()
      .reverse()
      .forEach((e) => frag.append(eventRow(e)));
    list.prepend(frag);
    while (list.childElementCount > MAX_ROWS) list.lastElementChild.remove();
    const total = ui.events.filter(matches).length;
    countEl.textContent = `${total} event${total === 1 ? "" : "s"}${total > MAX_ROWS ? ` (showing newest ${MAX_ROWS})` : ""} · newest first`;
  }

  let searchTimer = null;
  search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      ui.search = search.value.trim();
      renderAll();
    }, 200);
  });

  async function poll() {
    if (inflight || destroyed) return;
    inflight = true;
    try {
      const fresh = [];
      for (let page = 0; page < 20; page += 1) {
        const batch = await api.events(ctx.runId, { after: ui.lastSeq, limit: 2000 });
        if (destroyed) return;
        if (!batch.length) break;
        fresh.push(...batch);
        ui.lastSeq = batch[batch.length - 1].seq;
        if (batch.length < 2000) break;
      }
      if (fresh.length) {
        const firstLoad = ui.events.length === 0;
        ui.events.push(...fresh);
        renderChips();
        if (firstLoad) renderAll();
        else prepend(fresh);
      } else if (list.querySelector(".loading")) {
        renderAll();
      }
    } catch (err) {
      if (err.status !== 0) reportError(err, "Loading events");
    } finally {
      inflight = false;
    }
  }

  renderChips();
  if (ui.events.length) renderAll();
  else list.append(h("div", { class: "loading" }, "Loading audit trail…"));

  return {
    el,
    update: () => poll(),
    destroy: () => (destroyed = true),
  };
}
