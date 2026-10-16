// App shell: hash router, shared state, polling loop, sidebar + main area composition.
//
// Routes:  #/runs/<id>/<tab>   #/approvals   #/   (tab ∈ TABS)
// Polling: selected run every 2 s, runs list + global approvals every 4 s. Views receive
// fresh data through update() and are responsible for not clobbering in-progress input.

import { api } from "./api.js";
import { h, clear, sig, emptyState } from "./util.js";
import { applyTheme, cycleTheme, openSettings, reportError, toast } from "./ui.js";
import { renderRunList, createRunHeader, openNewRunModal } from "./views/runs.js";
import { createPipelineView } from "./views/pipeline.js";
import { createApprovalsView, createInboxView } from "./views/approvals.js";
import { createTimelineView } from "./views/timeline.js";
import { createArtifactsView } from "./views/artifacts.js";
import { createMetricsView } from "./views/metrics.js";
import { createReportView } from "./views/report.js";
import { createFilesView } from "./views/files.js";

const RUN_POLL_MS = 2000;
const LIST_POLL_MS = 4000;

export const TABS = [
  { id: "pipeline", label: "Pipeline", create: createPipelineView },
  { id: "approvals", label: "Approvals", create: createApprovalsView },
  { id: "timeline", label: "Timeline", create: createTimelineView },
  { id: "artifacts", label: "Artifacts", create: createArtifactsView },
  { id: "metrics", label: "Metrics", create: createMetricsView },
  { id: "report", label: "Report", create: createReportView },
  { id: "files", label: "Files", create: createFilesView },
];

const state = {
  route: { view: "home", runId: null, tab: "pipeline" },
  runs: null,
  pendingApprovals: [],
  runData: null, // latest GET /api/runs/{id} for the selected run
  uiByRun: new Map(), // per-run UI state that must survive re-renders
  connected: true,
};

// mounted components
let mounted = { key: null, header: null, tab: null, tabKey: null, inbox: null };
const els = {};

function uiFor(runId) {
  if (!state.uiByRun.has(runId)) {
    state.uiByRun.set(runId, { sections: {}, selectedNode: null, reqOpen: false });
  }
  return state.uiByRun.get(runId);
}

// ------------------------------------------------------------------ routing
function parseHash() {
  const raw = decodeURIComponent((location.hash || "").replace(/^#\/?/, ""));
  const parts = raw.split("/").filter(Boolean);
  if (parts[0] === "approvals") return { view: "approvals", runId: null, tab: null };
  if (parts[0] === "runs" && parts[1]) {
    const tab = TABS.some((t) => t.id === parts[2]) ? parts[2] : "pipeline";
    return { view: "run", runId: parts[1], tab };
  }
  return { view: "home", runId: null, tab: null };
}

export function navigate(hash) {
  if (location.hash === hash) onRoute();
  else location.hash = hash;
}

export function runHash(runId, tab = "pipeline") {
  return `#/runs/${encodeURIComponent(runId)}/${tab}`;
}

function onRoute() {
  const route = parseHash();
  const prev = state.route;
  state.route = route;
  if (route.view === "home" && state.runs && state.runs.length) {
    // default landing: the most recent run
    history.replaceState(null, "", runHash(state.runs[0].id));
    state.route = parseHash();
  }
  if (state.route.runId !== prev.runId) state.runData = null;
  renderSidebar();
  mountMain();
  pollRun(true);
}

// ------------------------------------------------------------------ main area
function destroyMounted() {
  if (mounted.tab && mounted.tab.destroy) mounted.tab.destroy();
  if (mounted.header && mounted.header.destroy) mounted.header.destroy();
  if (mounted.inbox && mounted.inbox.destroy) mounted.inbox.destroy();
  mounted = { key: null, header: null, tab: null, tabKey: null, inbox: null };
}

function mountMain() {
  const r = state.route;
  const key = r.view === "run" ? `run:${r.runId}` : r.view;
  if (mounted.key === key && r.view === "run") {
    if (mounted.tabKey !== r.tab) mountTab();
    return;
  }
  destroyMounted();
  mounted.key = key;
  clear(els.main);

  if (r.view === "approvals") {
    mounted.inbox = createInboxView({ navigate, runHash, refresh: refreshAll });
    els.main.append(mounted.inbox.el);
    mounted.inbox.update(state.pendingApprovals, state.runs || []);
    document.title = "Approvals inbox · Agentic SDLC";
    return;
  }
  if (r.view !== "run") {
    els.main.append(
      h(
        "div",
        { class: "home" },
        state.runs === null
          ? h("div", { class: "loading" }, "Loading runs…")
          : emptyState("No runs yet", "Start a run with “New run” in the sidebar, or from the CLI: sdlc run scenarios/<file>.yaml --approve dashboard"),
        state.runs !== null ? h("button", { class: "btn btn-primary", type: "button", onClick: () => openNewRunModal(onRunStarted) }, "＋ New run") : null,
      ),
    );
    document.title = "Agentic SDLC Orchestrator";
    return;
  }

  const ctx = makeCtx(r.runId);
  mounted.header = createRunHeader(ctx);
  els.tabbar = h("div", { class: "tabbar", role: "tablist", "aria-label": "Run views" });
  els.tabpanel = h("section", { class: "tabpanel", role: "tabpanel", tabindex: "-1" });
  els.main.append(mounted.header.el, els.tabbar, els.tabpanel);
  renderTabbar();
  mountTab();
}

function makeCtx(runId) {
  return {
    runId,
    ui: uiFor(runId),
    navigate,
    runHash,
    refresh: refreshAll,
    getRunData: () => state.runData,
  };
}

function renderTabbar() {
  if (!els.tabbar) return;
  const pending = state.runData ? state.runData.approvals.filter((a) => a.status === "pending").length : 0;
  const key = sig([state.route.tab, pending]);
  if (els.tabbar.dataset.sig === key) return;
  els.tabbar.dataset.sig = key;
  const hadFocus = els.tabbar.contains(document.activeElement);
  clear(els.tabbar);
  TABS.forEach((t, idx) => {
    const selected = t.id === state.route.tab;
    const a = h(
      "a",
      {
        class: `tab${selected ? " active" : ""}`,
        role: "tab",
        id: `tab-${t.id}`,
        href: runHash(state.route.runId, t.id),
        "aria-selected": selected ? "true" : "false",
        tabindex: selected ? "0" : "-1",
      },
      t.label,
      t.id === "approvals" && pending
        ? h("span", { class: "badge badge-amber", "aria-label": `${pending} pending` }, String(pending))
        : null,
    );
    a.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      e.preventDefault();
      const next = TABS[(idx + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
      navigate(runHash(state.route.runId, next.id));
    });
    els.tabbar.append(a);
  });
  if (hadFocus) {
    const active = els.tabbar.querySelector(".tab.active");
    if (active) active.focus();
  }
}

function mountTab() {
  if (mounted.tab && mounted.tab.destroy) mounted.tab.destroy();
  const def = TABS.find((t) => t.id === state.route.tab) || TABS[0];
  mounted.tabKey = def.id;
  mounted.tab = def.create(makeCtx(state.route.runId));
  clear(els.tabpanel);
  els.tabpanel.setAttribute("aria-labelledby", `tab-${def.id}`);
  els.tabpanel.append(mounted.tab.el);
  renderTabbar();
  if (state.runData) safeUpdate(mounted.tab, state.runData);
}

function safeUpdate(component, ...args) {
  try {
    component.update(...args);
  } catch (err) {
    console.error(err);
  }
}

// ------------------------------------------------------------------ sidebar
function renderSidebar() {
  const pending = state.pendingApprovals.length;
  els.inboxCount.textContent = String(pending);
  els.inboxCount.hidden = pending === 0;
  els.inboxLink.classList.toggle("active", state.route.view === "approvals");
  els.inboxLink.setAttribute("aria-current", state.route.view === "approvals" ? "page" : "false");
  renderRunList(els.runList, state.runs, state.route.runId, runHash);
}

// ------------------------------------------------------------------ polling
let runInflight = false;
let listInflight = false;
let runTimer = null;
let listTimer = null;

function setConnected(ok) {
  if (state.connected === ok) return;
  state.connected = ok;
  els.conn.classList.toggle("down", !ok);
  els.conn.textContent = ok ? "Connected" : "Disconnected — retrying";
  if (!ok) toast("Lost connection to the dashboard server; retrying…", "warn");
  else toast("Reconnected", "ok", { timeout: 2000 });
}

async function pollRun(force = false) {
  clearTimeout(runTimer);
  const r = state.route;
  if (r.view === "run" && (force || !document.hidden) && !runInflight) {
    runInflight = true;
    const runId = r.runId;
    try {
      const data = await api.run(runId);
      if (state.route.runId === runId) {
        state.runData = data;
        setConnected(true);
        if (mounted.header) safeUpdate(mounted.header, data);
        renderTabbar();
        if (mounted.tab) safeUpdate(mounted.tab, data);
        document.title = `${data.run.scenario} · ${data.run.status} · Agentic SDLC`;
      }
    } catch (err) {
      if (err.status === 404) {
        if (mounted.header) mounted.header.notFound();
      } else if (err.status === 0) {
        setConnected(false);
      } else {
        reportError(err, "Refreshing run");
      }
    } finally {
      runInflight = false;
    }
  }
  runTimer = setTimeout(pollRun, RUN_POLL_MS);
}

async function pollLists() {
  clearTimeout(listTimer);
  if (!listInflight && !document.hidden) {
    listInflight = true;
    try {
      const [runs, approvals] = await Promise.all([api.runs(), api.approvals("pending")]);
      const firstLoad = state.runs === null;
      state.runs = runs;
      state.pendingApprovals = approvals;
      setConnected(true);
      renderSidebar();
      if (mounted.inbox) safeUpdate(mounted.inbox, approvals, runs);
      if (firstLoad && state.route.view === "home") onRoute();
    } catch (err) {
      if (err.status === 0) setConnected(false);
      else reportError(err, "Refreshing runs");
    } finally {
      listInflight = false;
    }
  }
  listTimer = setTimeout(pollLists, LIST_POLL_MS);
}

/** Trigger an immediate refresh of everything (after a mutation). */
export function refreshAll() {
  pollLists();
  pollRun(true);
}

function onRunStarted(runId) {
  refreshAll();
  navigate(runHash(runId, "pipeline"));
}

// ------------------------------------------------------------------ boot
function boot() {
  applyTheme();
  els.main = document.getElementById("main");
  els.runList = document.getElementById("run-list");
  els.inboxLink = document.getElementById("inbox-link");
  els.inboxCount = document.getElementById("inbox-count");
  els.conn = document.getElementById("conn");
  els.app = document.getElementById("app");

  document.getElementById("new-run").addEventListener("click", () => openNewRunModal(onRunStarted));
  document.getElementById("settings-btn").addEventListener("click", () => openSettings());
  document.getElementById("theme-toggle").addEventListener("click", cycleTheme);
  const sideToggle = document.getElementById("sidebar-toggle");
  sideToggle.addEventListener("click", () => {
    const collapsed = els.app.classList.toggle("sidebar-collapsed");
    sideToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshAll();
  });
  window.addEventListener("hashchange", onRoute);
  window.addEventListener("unhandledrejection", (e) => {
    e.preventDefault();
    reportError(e.reason);
  });

  onRoute();
  pollLists();
}

boot();
