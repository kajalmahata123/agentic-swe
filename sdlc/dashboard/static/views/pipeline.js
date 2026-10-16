// Pipeline tab: the run's dependency graph as a layered inline-SVG DAG plus a node
// detail side panel.

import { h, s, clear, sig, fmtUsd, humanize, truncate, KIND_ICON } from "../util.js";
import { createNodePanel } from "./nodepanel.js";

const NODE_W = 184;
const NODE_H = 64;
const COL_GAP = 64;
const ROW_GAP = 20;
const PAD = 28;

export { KIND_ICON };

export const STATUS_ORDER = [
  "pending",
  "running",
  "awaiting_approval",
  "succeeded",
  "failed",
  "skipped",
  "stale",
  "cancelled",
];

/** Edges = declared deps + virtual "repair loop" edges from the check that triggered a repair. */
export function graphEdges(nodes) {
  const ids = new Set(nodes.map((n) => n.id));
  const edges = [];
  nodes.forEach((n) => {
    (n.deps || []).forEach((d) => ids.has(d) && edges.push({ from: d, to: n.id, virtual: false }));
    const trigger = n.params && n.params.trigger;
    if (trigger && ids.has(trigger) && !(n.deps || []).includes(trigger)) {
      edges.push({ from: trigger, to: n.id, virtual: true, label: "repair loop" });
    }
  });
  return edges;
}

/** Longest-path layering over all edges (incl. virtual) so repair loops sit after their trigger. */
export function layout(nodes) {
  const edges = graphEdges(nodes);
  const preds = new Map(nodes.map((n) => [n.id, []]));
  edges.forEach((e) => preds.get(e.to).push(e.from));
  const level = new Map();
  const visiting = new Set();
  const depth = (id) => {
    if (level.has(id)) return level.get(id);
    if (visiting.has(id)) return 0; // defensive: cycle
    visiting.add(id);
    const ps = preds.get(id) || [];
    const lv = ps.length ? 1 + Math.max(...ps.map(depth)) : 0;
    visiting.delete(id);
    level.set(id, lv);
    return lv;
  };
  nodes.forEach((n) => depth(n.id));

  const columns = [];
  nodes.forEach((n, idx) => {
    const lv = level.get(n.id) ?? n.level ?? 0;
    (columns[lv] ||= []).push({ node: n, idx });
  });
  // Barycentric ordering: place each node near the average row of its predecessors.
  const row = new Map();
  columns.forEach((col, lv) => {
    if (!col) return;
    if (lv > 0) {
      col.forEach((c) => {
        const ps = (preds.get(c.node.id) || []).filter((p) => row.has(p));
        c.bary = ps.length ? ps.reduce((a, p) => a + row.get(p), 0) / ps.length : c.idx;
      });
      col.sort((a, b) => a.bary - b.bary || a.idx - b.idx);
    }
    col.forEach((c, i) => row.set(c.node.id, i));
  });

  const maxRows = Math.max(1, ...columns.map((c) => (c ? c.length : 0)));
  const totalH = maxRows * NODE_H + (maxRows - 1) * ROW_GAP;
  const pos = new Map();
  columns.forEach((col, lv) => {
    if (!col) return;
    const colH = col.length * NODE_H + (col.length - 1) * ROW_GAP;
    const y0 = PAD + (totalH - colH) / 2;
    col.forEach((c, i) => {
      pos.set(c.node.id, { x: PAD + lv * (NODE_W + COL_GAP), y: y0 + i * (NODE_H + ROW_GAP) });
    });
  });
  return {
    pos,
    edges,
    width: PAD * 2 + columns.length * NODE_W + Math.max(0, columns.length - 1) * COL_GAP,
    height: PAD * 2 + totalH,
  };
}

function shapeFor(kind) {
  const w = NODE_W;
  const hgt = NODE_H;
  switch (kind) {
    case "check": {
      const c = 12;
      return s("path", { class: "shape", d: `M${c},0 H${w - c} L${w},${c} V${hgt - c} L${w - c},${hgt} H${c} L0,${hgt - c} V${c} Z` });
    }
    case "human":
      return s("rect", { class: "shape", width: w, height: hgt, rx: hgt / 2 });
    case "planner":
      return s(
        "g",
        null,
        s("rect", { class: "shape", width: w, height: hgt, rx: 10 }),
        s("rect", { class: "shape-inner", x: 4, y: 4, width: w - 8, height: hgt - 8, rx: 7 }),
      );
    case "join":
      return s(
        "g",
        null,
        s("rect", { class: "shape", width: w, height: hgt, rx: 3 }),
        s("line", { class: "shape-inner", x1: 7, y1: 6, x2: 7, y2: hgt - 6 }),
        s("line", { class: "shape-inner", x1: w - 7, y1: 6, x2: w - 7, y2: hgt - 6 }),
      );
    case "action":
      return s("rect", { class: "shape", width: w, height: hgt, rx: 3 });
    default:
      return s("rect", { class: "shape", width: w, height: hgt, rx: 10 });
  }
}

function nodeTooltip(n) {
  const lines = [
    `${n.id} — ${n.title || ""}`,
    `kind: ${n.kind} · stage: ${n.stage} · status: ${humanize(n.status)}`,
    `attempts: ${n.attempts} · cost: ${fmtUsd(n.cost_usd)}`,
  ];
  if (n.approval) lines.push(`requires ${n.approval} approval`);
  if (n.created_by) lines.push(`created dynamically by ${n.created_by}`);
  if (n.summary) lines.push(`summary: ${truncate(n.summary, 200)}`);
  if (n.error) lines.push(`error: ${truncate(n.error, 200)}`);
  return lines.join("\n");
}

function renderNode(n, p, selected, onSelect) {
  const g = s("g", {
    class: `node st-${n.status} k-${n.kind}${selected ? " selected" : ""}${n.created_by ? " dynamic" : ""}`,
    transform: `translate(${p.x},${p.y})`,
    tabindex: "0",
    role: "button",
    "aria-label": `${n.id}, ${n.kind} node, status ${humanize(n.status)}${n.approval ? `, requires ${n.approval} approval` : ""}`,
    "aria-pressed": selected ? "true" : "false",
    dataset: { node: n.id },
  });
  g.append(s("title", null, nodeTooltip(n)));
  if (n.status === "running") g.append(s("rect", { class: "pulse", x: -4, y: -4, width: NODE_W + 8, height: NODE_H + 8, rx: 14 }));
  g.append(shapeFor(n.kind));
  // kind icon
  g.append(
    s("circle", { class: "kind-bg", cx: 20, cy: 22, r: 11 }),
    s("text", { class: "kind-icon", x: 20, y: 26, "text-anchor": "middle" }, KIND_ICON[n.kind] || "•"),
  );
  // labels
  g.append(
    s("text", { class: "n-title", x: 38, y: 21 }, truncate(n.id, 19)),
    s("text", { class: "n-sub", x: 38, y: 37 }, `${n.kind} · ${humanize(n.status)}`),
  );
  const facts = [];
  if (n.attempts) facts.push(`${n.attempts} attempt${n.attempts === 1 ? "" : "s"}`);
  if (n.cost_usd) facts.push(fmtUsd(n.cost_usd));
  if (facts.length) g.append(s("text", { class: "n-facts", x: 38, y: 53 }, facts.join(" · ")));
  // retry marker
  if (n.attempts > 1) {
    g.append(s("text", { class: "n-retry", x: NODE_W - 10, y: 53, "text-anchor": "end" }, `↻${n.attempts - 1}`));
  }
  // approval badge (top-right)
  if (n.approval) {
    const label = `✋ ${n.approval}`;
    const bw = 12 + label.length * 6.2;
    g.append(
      s(
        "g",
        { class: `n-badge appr${n.status === "awaiting_approval" ? " waiting" : ""}`, transform: `translate(${NODE_W - bw - 6},-9)` },
        s("rect", { width: bw, height: 18, rx: 9 }),
        s("text", { x: bw / 2, y: 13, "text-anchor": "middle" }, label),
      ),
    );
  }
  // dynamic-node marker (top-left)
  if (n.created_by) {
    const label = n.created_by.includes("repair") ? "⟲ repair loop" : `+ by ${truncate(n.created_by, 12)}`;
    const bw = 12 + label.length * 6;
    g.append(
      s(
        "g",
        { class: "n-badge dyn", transform: "translate(10,-9)" },
        s("rect", { width: bw, height: 18, rx: 4 }),
        s("text", { x: bw / 2, y: 13, "text-anchor": "middle" }, label),
      ),
    );
  }
  g.addEventListener("click", () => onSelect(n.id));
  g.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onSelect(n.id);
    }
  });
  return g;
}

function edgePath(a, b) {
  const x1 = a.x + NODE_W;
  const y1 = a.y + NODE_H / 2;
  const x2 = b.x - 6;
  const y2 = b.y + NODE_H / 2;
  const dx = Math.max(32, (x2 - x1) / 2);
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
}

function renderGraph(nodes, selectedId, onSelect, zoom) {
  const { pos, edges, width, height } = layout(nodes);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const svg = s("svg", {
    class: "dag",
    width: Math.round(width * zoom),
    height: Math.round(height * zoom),
    viewBox: `0 0 ${width} ${height}`,
    role: "group",
    "aria-label": `Pipeline graph with ${nodes.length} nodes`,
  });
  svg.append(
    s(
      "defs",
      null,
      s(
        "marker",
        { id: "arrow", viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        s("path", { class: "arrow-head", d: "M0,0 L10,5 L0,10 z" }),
      ),
      s(
        "marker",
        { id: "arrow-hi", viewBox: "0 0 10 10", refX: "8", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        s("path", { class: "arrow-head hi", d: "M0,0 L10,5 L0,10 z" }),
      ),
    ),
  );
  const edgeLayer = s("g", { class: "edges" });
  edges.forEach((e) => {
    const a = pos.get(e.from);
    const b = pos.get(e.to);
    if (!a || !b) return;
    const hi = selectedId && (e.from === selectedId || e.to === selectedId);
    const src = byId.get(e.from);
    const cls = ["edge", e.virtual ? "virtual" : "", hi ? "hi" : "", src && src.status === "succeeded" ? "done" : "", src && src.status === "running" ? "flow" : ""]
      .filter(Boolean)
      .join(" ");
    const path = s("path", { class: cls, d: edgePath(a, b), "marker-end": hi ? "url(#arrow-hi)" : "url(#arrow)" });
    path.append(s("title", null, `${e.from} → ${e.to}${e.virtual ? " (repair loop)" : ""}`));
    edgeLayer.append(path);
    if (e.virtual) {
      edgeLayer.append(
        s("text", { class: "edge-label", x: (a.x + NODE_W + b.x) / 2, y: (a.y + b.y) / 2 + NODE_H / 2 - 6, "text-anchor": "middle" }, e.label),
      );
    }
  });
  svg.append(edgeLayer);
  const nodeLayer = s("g", { class: "nodes" });
  nodes.forEach((n) => {
    const p = pos.get(n.id);
    if (p) nodeLayer.append(renderNode(n, p, n.id === selectedId, onSelect));
  });
  svg.append(nodeLayer);
  return svg;
}

function legend() {
  return h(
    "div",
    { class: "legend", "aria-label": "Legend" },
    h("span", { class: "legend-title" }, "Status"),
    STATUS_ORDER.map((st) => h("span", { class: `legend-item st-${st}` }, h("span", { class: "swatch", "aria-hidden": "true" }), humanize(st))),
    h("span", { class: "legend-sep", "aria-hidden": "true" }),
    h("span", { class: "legend-title" }, "Kind"),
    Object.entries(KIND_ICON).map(([k, icon]) => h("span", { class: "legend-item" }, h("span", { class: "kind-glyph", "aria-hidden": "true" }, icon), k)),
    h("span", { class: "legend-sep", "aria-hidden": "true" }),
    h("span", { class: "legend-item" }, h("span", { class: "tag tag-warn" }, "✋"), "approval gate"),
    h("span", { class: "legend-item" }, h("span", { class: "tag tag-dyn" }, "+"), "planned at runtime"),
  );
}

function summaryStrip(nodes) {
  const counts = {};
  nodes.forEach((n) => (counts[n.status] = (counts[n.status] || 0) + 1));
  const done = (counts.succeeded || 0) + (counts.skipped || 0);
  const pct = nodes.length ? Math.round((done / nodes.length) * 100) : 0;
  return h(
    "div",
    { class: "pipe-summary" },
    h(
      "div",
      { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(pct), "aria-label": "Pipeline progress" },
      h("div", { class: "progress-bar", style: { width: `${pct}%` } }),
    ),
    h("span", { class: "pipe-pct" }, `${done}/${nodes.length} nodes settled (${pct}%)`),
    STATUS_ORDER.filter((st) => counts[st]).map((st) => h("span", { class: `pill pill-sm st-${st}` }, h("span", { class: "pill-dot", "aria-hidden": "true" }), `${counts[st]} ${humanize(st)}`)),
  );
}

export function createPipelineView(ctx) {
  const ui = ctx.ui;
  if (!ui.zoom) ui.zoom = "fit";
  const el = h("div", { class: "pipeline-view" });
  const stripHost = h("div");
  const canvas = h("div", { class: "dag-canvas", tabindex: "-1" });
  const toolbar = h(
    "div",
    { class: "dag-toolbar" },
    h("button", { class: "icon-btn", type: "button", "aria-label": "Zoom out", title: "Zoom out", onClick: () => setZoom(currentZoom - 0.15) }, "−"),
    h("button", { class: "btn btn-small", type: "button", title: "Fit graph to width", onClick: () => setZoom("fit") }, "Fit"),
    h("button", { class: "btn btn-small", type: "button", title: "Actual size", onClick: () => setZoom(1) }, "100%"),
    h("button", { class: "icon-btn", type: "button", "aria-label": "Zoom in", title: "Zoom in", onClick: () => setZoom(currentZoom + 0.15) }, "+"),
  );
  const left = h("div", { class: "pipe-main" }, stripHost, h("div", { class: "dag-wrap" }, toolbar, canvas), legend());
  const panel = createNodePanel(ctx, () => select(null));
  el.append(left, panel.el);

  let lastData = null;
  let graphSig = null;
  let currentZoom = 1;

  function setZoom(z) {
    ui.zoom = z === "fit" ? "fit" : Math.min(2, Math.max(0.4, Math.round(z * 100) / 100));
    graphSig = null;
    if (lastData) draw(lastData);
  }

  function select(nodeId) {
    ui.selectedNode = ui.selectedNode === nodeId ? null : nodeId;
    graphSig = null;
    if (lastData) {
      draw(lastData);
      syncPanel(lastData);
    }
    if (ui.selectedNode) {
      const focusEl = canvas.querySelector(`[data-node="${CSS.escape(ui.selectedNode)}"]`);
      if (focusEl) focusEl.focus({ preventScroll: true });
    }
  }

  function draw(data) {
    if (ui.zoom === "fit") {
      const avail = Math.max(300, canvas.clientWidth - 8);
      currentZoom = Math.min(1, Math.max(0.75, Math.floor((avail / layout(data.nodes).width) * 100) / 100));
    } else currentZoom = ui.zoom;
    const key = sig([data.nodes, ui.selectedNode, currentZoom]);
    if (key === graphSig) return;
    graphSig = key;
    const scroll = { left: canvas.scrollLeft, top: canvas.scrollTop };
    const focused = canvas.contains(document.activeElement) ? document.activeElement.dataset?.node : null;
    clear(canvas);
    if (!data.nodes.length) {
      canvas.append(h("div", { class: "empty" }, "This run has no nodes yet."));
      return;
    }
    canvas.append(renderGraph(data.nodes, ui.selectedNode, select, currentZoom));
    canvas.scrollLeft = scroll.left;
    canvas.scrollTop = scroll.top;
    if (focused) {
      const f = canvas.querySelector(`[data-node="${CSS.escape(focused)}"]`);
      if (f) f.focus({ preventScroll: true });
    }
    clear(stripHost);
    stripHost.append(summaryStrip(data.nodes));
  }

  function syncPanel(data) {
    const node = ui.selectedNode ? data.nodes.find((n) => n.id === ui.selectedNode) : null;
    el.classList.toggle("with-panel", Boolean(node));
    if (node) panel.show(node, data);
    else panel.hide();
  }

  function update(data) {
    lastData = data;
    if (ui.selectedNode && !data.nodes.some((n) => n.id === ui.selectedNode)) ui.selectedNode = null;
    draw(data);
    syncPanel(data);
  }

  canvas.append(h("div", { class: "loading" }, "Loading pipeline…"));
  return { el, update, destroy: () => panel.destroy() };
}
