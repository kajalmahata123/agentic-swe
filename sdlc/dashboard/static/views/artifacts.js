// Artifacts tab: versioned, content-addressed artifacts with lineage links and a JSON
// viewer, plus the recorded decisions (ADR-lite) list.

import { api } from "../api.js";
import { h, clear, sig, fmtRelative, fmtDate, shortHash, jsonTree, prettyJson, emptyState, DASH } from "../util.js";
import { toast, reportError } from "../ui.js";

export function createArtifactsView(ctx) {
  const ui = (ctx.ui.artifacts ||= { mode: "artifacts", selected: null, raw: false, openGroups: {} });
  const el = h("div", { class: "artifacts-view" });
  const seg = h("div", { class: "segmented", role: "tablist", "aria-label": "Artifacts or decisions" });
  const body = h("div");
  el.append(seg, body);

  const listHost = h("nav", { class: "art-list", "aria-label": "Artifact versions" });
  const viewer = h("div", { class: "art-viewer" });
  const split = h("div", { class: "split" }, listHost, viewer);
  const decisionsHost = h("div", { class: "decisions" });

  let arts = [];
  let artSig = null;
  let decSig = null;
  let inflight = false;
  let viewToken = 0;

  function renderSeg() {
    clear(seg);
    [
      ["artifacts", "Artifacts"],
      ["decisions", "Decisions"],
    ].forEach(([id, label]) => {
      const on = ui.mode === id;
      const b = h("button", { type: "button", class: `seg${on ? " on" : ""}`, role: "tab", "aria-selected": on ? "true" : "false" }, label);
      b.addEventListener("click", () => {
        ui.mode = id;
        renderSeg();
        mountBody();
        poll(true);
      });
      seg.append(b);
    });
  }

  function mountBody() {
    clear(body).append(ui.mode === "artifacts" ? split : decisionsHost);
  }

  function findByHash(hash) {
    return arts.find((a) => a.hash === hash);
  }

  function select(name, version) {
    ui.selected = { name, version };
    ui.openGroups[name] = true;
    renderList();
    loadViewer();
  }

  function lineageLink(name, hash) {
    const target = findByHash(hash);
    if (!target) return h("span", { class: "mono muted", title: hash }, `${name}@${shortHash(hash, 8)}`);
    const b = h("button", { type: "button", class: "link-btn mono", title: `Open ${target.name} v${target.version} (${hash})` }, `${target.name} v${target.version}`);
    b.addEventListener("click", () => select(target.name, target.version));
    return b;
  }

  function renderList() {
    clear(listHost);
    if (!arts.length) {
      listHost.append(emptyState("No artifacts yet", "Nodes store their outputs here as they succeed."));
      return;
    }
    const groups = new Map();
    arts.forEach((a) => {
      if (!groups.has(a.name)) groups.set(a.name, []);
      groups.get(a.name).push(a);
    });
    groups.forEach((versions, name) => {
      versions.sort((x, y) => y.version - x.version);
      const det = h("details", { class: "art-group" });
      det.open = Boolean(ui.openGroups[name]) || Boolean(ui.selected && ui.selected.name === name);
      det.addEventListener("toggle", () => (ui.openGroups[name] = det.open));
      det.append(
        h(
          "summary",
          null,
          h("span", { class: "mono art-name" }, name),
          h("span", { class: "tag tag-muted" }, `v${versions[0].version}`),
          versions.length > 1 ? h("span", { class: "muted small" }, `${versions.length} versions`) : null,
        ),
      );
      const ul = h("ul", { class: "art-versions" });
      versions.forEach((a) => {
        const active = ui.selected && ui.selected.name === a.name && ui.selected.version === a.version;
        const btn = h(
          "button",
          { type: "button", class: `art-ver${active ? " active" : ""}`, "aria-current": active ? "true" : null },
          h("span", { class: "art-ver-top" }, h("strong", null, `v${a.version}`), h("span", { class: "mono muted" }, shortHash(a.hash, 10))),
          h("span", { class: "muted small" }, `by ${a.producer} · ${fmtRelative(a.created_at)}`),
        );
        btn.addEventListener("click", () => select(a.name, a.version));
        ul.append(h("li", null, btn));
      });
      det.append(ul);
      listHost.append(det);
    });
  }

  async function loadViewer() {
    const selected = ui.selected;
    const token = ++viewToken;
    if (!selected) {
      clear(viewer).append(emptyState("Select an artifact", "Pick a version on the left to inspect its value and lineage."));
      return;
    }
    clear(viewer).append(h("div", { class: "loading" }, "Loading artifact…"));
    try {
      const art = await api.artifact(ctx.runId, selected.name, selected.version);
      if (token !== viewToken) return;
      const meta = arts.find((a) => a.name === art.name && a.version === art.version) || {};
      const consumers = arts.filter((a) => Object.values(a.derived_from || {}).includes(art.hash));
      const rawBtn = h("button", { type: "button", class: "btn btn-small" }, ui.raw ? "Tree view" : "Raw JSON");
      const copyBtn = h("button", { type: "button", class: "btn btn-small" }, "Copy JSON");
      const valueHost = h("div", { class: "art-value" });
      const renderValue = () => {
        clear(valueHost).append(ui.raw ? h("pre", { class: "json-raw" }, prettyJson(art.value)) : jsonTree(art.value, { openDepth: 2 }));
        rawBtn.textContent = ui.raw ? "Tree view" : "Raw JSON";
      };
      rawBtn.addEventListener("click", () => {
        ui.raw = !ui.raw;
        renderValue();
      });
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(prettyJson(art.value));
          toast("Copied artifact JSON", "ok", { timeout: 1500 });
        } catch {
          toast("Clipboard is not available in this browser context", "warn");
        }
      });
      const derived = Object.entries(art.derived_from || {});
      clear(viewer).append(
        h(
          "div",
          { class: "art-head" },
          h("h3", { class: "mono" }, `${art.name} `, h("span", { class: "tag" }, `v${art.version}`)),
          h("div", { class: "art-tools" }, rawBtn, copyBtn),
        ),
        h(
          "dl",
          { class: "kv kv-inline" },
          h("dt", null, "Hash"),
          h("dd", { class: "mono" }, art.hash),
          h("dt", null, "Producer"),
          h("dd", { class: "mono" }, art.producer),
          h("dt", null, "Created"),
          h("dd", null, fmtDate(meta.created_at)),
          h("dt", null, "Derived from"),
          h("dd", null, derived.length ? h("span", { class: "lineage" }, derived.map(([n, hsh]) => lineageLink(n, hsh))) : h("span", { class: "muted" }, "no inputs (root)")),
          h("dt", null, "Consumed by"),
          h("dd", null, consumers.length ? h("span", { class: "lineage" }, consumers.map((c) => lineageLink(c.name, c.hash))) : h("span", { class: "muted" }, DASH)),
        ),
        valueHost,
      );
      renderValue();
    } catch (err) {
      if (token === viewToken) clear(viewer).append(emptyState("Could not load artifact", err.message));
    }
  }

  function renderDecisions(decisions) {
    clear(decisionsHost);
    if (!decisions.length) {
      decisionsHost.append(emptyState("No decisions recorded", "Agents, humans and the policy engine record decisions as the run progresses."));
      return;
    }
    decisions
      .slice()
      .sort((a, b) => b.ts - a.ts)
      .forEach((d) => {
        const actorKind = String(d.actor || "").split(":")[0];
        const based = Object.entries(d.based_on || {});
        decisionsHost.append(
          h(
            "article",
            { class: "decision" },
            h(
              "header",
              null,
              h("span", { class: `tag actor-${actorKind}` }, d.actor),
              h("strong", null, d.title),
              h("span", { class: "mono muted small" }, d.node_id),
              h("span", { class: "muted small", title: fmtDate(d.ts) }, fmtRelative(d.ts)),
            ),
            d.rationale ? h("p", { class: "pre-wrap" }, h("span", { class: "muted" }, "Rationale: "), d.rationale) : null,
            d.alternatives && d.alternatives.length
              ? h("div", { class: "small" }, h("span", { class: "muted" }, "Alternatives considered: "), d.alternatives.map((a) => h("span", { class: "tag tag-muted" }, a)))
              : null,
            based.length
              ? h("div", { class: "small" }, h("span", { class: "muted" }, "Based on: "), h("span", { class: "lineage" }, based.map(([n, hsh]) => lineageLink(n, hsh))))
              : null,
          ),
        );
      });
  }

  async function poll(force = false) {
    if (inflight) return;
    inflight = true;
    try {
      const list = await api.artifacts(ctx.runId);
      const key = sig(list);
      if (key !== artSig || force) {
        artSig = key;
        arts = list;
        if (ui.mode === "artifacts") {
          renderList();
          if (!viewer.childElementCount || force) loadViewer();
        }
      }
      if (ui.mode === "decisions") {
        const decisions = await api.decisions(ctx.runId);
        const dk = sig(decisions);
        if (dk !== decSig || force) {
          decSig = dk;
          renderDecisions(decisions);
        }
      }
    } catch (err) {
      if (err.status !== 0) reportError(err, "Loading artifacts");
    } finally {
      inflight = false;
    }
  }

  renderSeg();
  mountBody();
  listHost.append(h("div", { class: "loading" }, "Loading…"));
  return { el, update: () => poll(false) };
}
