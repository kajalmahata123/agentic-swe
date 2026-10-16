// Sidebar run list, run header (status, controls, requirement, amendments) and the
// "New run" dialog.

import { api } from "../api.js";
import {
  h,
  clear,
  sig,
  statusPill,
  fmtRelative,
  fmtDate,
  fmtDuration,
  truncate,
  emptyState,
} from "../util.js";
import { modal, toast, reportError, fieldError, asyncButton } from "../ui.js";

// ------------------------------------------------------------------ sidebar list
export function renderRunList(container, runs, selectedId, runHash) {
  if (runs === null) {
    if (!container.childElementCount) container.append(h("li", { class: "muted pad" }, "Loading…"));
    return;
  }
  const rows = runs.map((r) => ({
    id: r.id,
    scenario: r.scenario,
    status: r.status,
    pending: r.pending_approvals,
    live: r.live,
    when: fmtRelative(r.started_at || r.created_at),
    req: truncate(r.requirement, 90),
    selected: r.id === selectedId,
  }));
  const key = sig(rows);
  if (container.dataset.sig === key) return;
  container.dataset.sig = key;
  const focusedId = container.contains(document.activeElement) ? document.activeElement.dataset.runId : null;
  clear(container);
  if (!rows.length) {
    container.append(h("li", { class: "pad" }, emptyState("No runs yet", "Use “New run” to start one.")));
    return;
  }
  rows.forEach((r) => {
    const a = h(
      "a",
      {
        class: `run-item${r.selected ? " active" : ""}`,
        href: runHash(r.id),
        dataset: { runId: r.id },
        "aria-current": r.selected ? "page" : null,
        title: r.req,
      },
      h(
        "div",
        { class: "run-item-top" },
        h("span", { class: "run-scenario" }, r.scenario),
        r.live ? h("span", { class: "live-dot", title: "Executing in this dashboard process", "aria-label": "live" }) : null,
        r.pending
          ? h("span", { class: "badge badge-amber", title: `${r.pending} pending approval(s)`, "aria-label": `${r.pending} pending approvals` }, `✋ ${r.pending}`)
          : null,
      ),
      h(
        "div",
        { class: "run-item-bottom" },
        statusPill(r.status, "pill-sm"),
        h("span", { class: "mono muted run-id" }, r.id.replace(/^run_/, "")),
        h("span", { class: "muted run-when" }, r.when),
      ),
    );
    container.append(h("li", null, a));
  });
  if (focusedId) {
    const el = container.querySelector(`[data-run-id="${CSS.escape(focusedId)}"]`);
    if (el) el.focus();
  }
}

// ------------------------------------------------------------------ new run
export async function openNewRunModal(onStarted) {
  let scenarios;
  try {
    scenarios = await api.scenarios();
  } catch (err) {
    reportError(err, "Loading scenarios");
    return;
  }
  const err = fieldError();
  const mode = h(
    "select",
    { id: "nr-mode" },
    h("option", { value: "dashboard" }, "dashboard — humans approve checkpoints here"),
    h("option", { value: "auto" }, "auto — policy auto-approves what it may (release still needs a human)"),
  );
  const parallel = h("input", { id: "nr-par", type: "number", min: "1", max: "8", value: "3" });
  let chosen = scenarios.length ? scenarios[0].file : null;
  const list = h("div", { class: "scenario-list", role: "radiogroup", "aria-label": "Scenario" });
  scenarios.forEach((sc, idx) => {
    const id = `sc-${idx}`;
    const radio = h("input", { type: "radio", name: "scenario", id, value: sc.file, checked: idx === 0 });
    radio.addEventListener("change", () => (chosen = sc.file));
    list.append(
      h(
        "label",
        { class: "scenario-card", for: id },
        radio,
        h(
          "div",
          { class: "scenario-body" },
          h(
            "div",
            { class: "scenario-head" },
            h("strong", null, sc.title || sc.name),
            h("span", { class: "tag" }, sc.mode),
            sc.seed ? h("span", { class: "tag tag-muted" }, `seed: ${sc.seed}`) : null,
            h("span", { class: "mono muted" }, sc.file),
          ),
          h("p", { class: "scenario-req" }, truncate(sc.requirement, 420)),
        ),
      ),
    );
  });
  const body = h(
    "div",
    { class: "form" },
    scenarios.length
      ? h("fieldset", { class: "field" }, h("legend", null, "Scenario"), list)
      : emptyState("No scenarios found", "Add YAML scenario files to the scenarios/ directory (or set SDLC_SCENARIOS)."),
    h(
      "div",
      { class: "field-row" },
      h("div", { class: "field" }, h("label", { for: "nr-mode" }, "Approval mode"), mode),
      h("div", { class: "field field-narrow" }, h("label", { for: "nr-par" }, "Max parallel"), parallel),
    ),
    err.el,
  );
  modal({
    title: "Start a new run",
    body,
    wide: true,
    actions: [
      { label: "Cancel" },
      {
        label: "Start run",
        kind: "primary",
        onClick: async () => {
          err.hide();
          if (!chosen) {
            err.show("Pick a scenario first.");
            return true;
          }
          const par = Math.max(1, Math.min(8, parseInt(parallel.value, 10) || 3));
          try {
            const res = await api.startRun(chosen, mode.value, par);
            toast(`Run ${res.run_id} started`, "ok");
            onStarted(res.run_id);
            return false;
          } catch (e) {
            err.show(e.message);
            if (e.status === 401) reportError(e);
            return true;
          }
        },
      },
    ],
  });
}

// ------------------------------------------------------------------ run header
export function createRunHeader(ctx) {
  const el = h("header", { class: "run-header" });
  const top = h("div", { class: "rh-top" });
  const banner = h("div", { class: "rh-banner-host" });
  const reqHost = h("div", { class: "rh-req-host" });
  const metaHost = h("div", { class: "rh-meta" });
  el.append(top, banner, metaHost, reqHost);
  top.append(h("div", { class: "loading" }, `Loading run ${ctx.runId}…`));

  let last = null;
  let controlsSig = null;
  const controls = h("div", { class: "rh-actions" });

  const stopBtn = asyncButton("■ Stop", () => confirmStop(ctx), { kind: "danger", title: "Safe-stop: halt at the next checkpoint; state stays resumable" });
  const resumeBtn = asyncButton("▶ Resume", () => doResume(ctx, last), { kind: "secondary", title: "Continue a stopped / failed run from its persisted state" });
  const amendBtn = asyncButton("✎ Amend requirement", () => openAmend(ctx), { title: "Change the requirement mid-run; affected nodes are re-planned" });
  controls.append(stopBtn, resumeBtn, amendBtn);

  function update(data) {
    const run = data.run;
    const ticking = !run.finished_at && run.status === "running" ? Math.floor(Date.now() / 5000) : 0;
    const key = sig([run, data.live, data.amendments, ctx.ui.reqOpen, ticking]);
    if (key === last?.key) return;
    last = { key, data };
    const live = data.live;
    // ---- top row
    clear(top);
    top.append(
      h(
        "div",
        { class: "rh-title" },
        h("h1", null, run.scenario),
        statusPill(run.status),
        live
          ? h("span", { class: "live-chip", title: "The engine is executing this run inside the dashboard process" }, h("span", { class: "live-dot" }), "live")
          : h("span", { class: "idle-chip", title: "Not executing in this dashboard process (finished, stopped, or driven from the CLI)" }, "not live"),
        h("span", { class: "mono muted rh-id", title: "Run id" }, run.id),
      ),
      controls,
    );
    const ctlKey = sig([run.status, live]);
    if (ctlKey !== controlsSig) {
      controlsSig = ctlKey;
      const active = run.status === "running" || run.status === "pending";
      stopBtn.disabled = !(active || live);
      resumeBtn.disabled = live || run.status === "succeeded";
      amendBtn.disabled = run.status === "succeeded";
    }
    // ---- banners: error / stop reason
    clear(banner);
    if (run.error && run.error !== run.stop_reason) {
      banner.append(
        h("div", { class: "banner banner-error", role: "alert" }, h("strong", null, "Run error"), h("span", { class: "banner-text" }, run.error)),
      );
    }
    if (run.stop_reason && (run.status === "stopped" || run.status === "running" || run.status === "failed")) {
      const pendingStop = run.status === "running";
      banner.append(
        h(
          "div",
          { class: `banner ${pendingStop ? "banner-warn" : "banner-info"}`, role: "status" },
          h("strong", null, pendingStop ? "Stop requested" : "Stopped"),
          h("span", { class: "banner-text" }, run.stop_reason),
          run.status === "stopped" ? h("span", { class: "muted" }, "State is persisted — use Resume to continue.") : null,
        ),
      );
    }
    // ---- meta row
    clear(metaHost);
    const cfg = run.config || {};
    const dur = run.started_at ? (run.finished_at || Date.now() / 1000) - run.started_at : null;
    metaHost.append(
      meta("Created", fmtRelative(run.created_at), fmtDate(run.created_at)),
      meta("Started", run.started_at ? fmtDate(run.started_at) : "—"),
      meta(run.finished_at ? "Duration" : "Elapsed", fmtDuration(dur)),
      meta("Approvals", cfg.approve_mode || "—"),
      meta("Model", cfg.model ? `${cfg.model}${cfg.fallback_model ? ` → ${cfg.fallback_model}` : ""}` : "—"),
      meta("Parallel", cfg.max_parallel ?? "—"),
      meta("Version", cfg.version || "—"),
    );
    // ---- requirement + amendments
    clear(reqHost);
    const det = h("details", { class: "rh-req" });
    det.open = ctx.ui.reqOpen;
    det.addEventListener("toggle", () => (ctx.ui.reqOpen = det.open));
    det.append(
      h("summary", null, h("span", { class: "section-title" }, "Requirement"), !det.open ? h("span", { class: "muted rh-req-preview" }, truncate(run.requirement, 140)) : null),
      h("pre", { class: "rh-req-text" }, run.requirement),
    );
    det.addEventListener("toggle", () => {
      const prev = det.querySelector(".rh-req-preview");
      if (prev) prev.hidden = det.open;
    });
    if (data.amendments && data.amendments.length) {
      det.append(
        h("h3", { class: "sub" }, `Amendments (${data.amendments.length})`),
        h(
          "ul",
          { class: "amend-list" },
          data.amendments.map((a) =>
            h(
              "li",
              null,
              h("span", { class: `tag ${a.applied ? "tag-ok" : "tag-warn"}` }, a.applied ? "applied" : "pending"),
              " ",
              h("span", { class: "muted" }, `${a.author} · ${fmtRelative(a.created_at)}`),
              h("div", { class: "amend-text" }, a.text),
            ),
          ),
        ),
      );
    }
    reqHost.append(det);
  }

  function notFound() {
    clear(top);
    clear(banner);
    clear(metaHost);
    clear(reqHost);
    top.append(emptyState("Run not found", `No run with id ${ctx.runId} exists in this SDLC_HOME.`));
  }

  return { el, update, notFound };
}

function meta(label, value, title = null) {
  return h("div", { class: "meta-item", title }, h("span", { class: "meta-label" }, label), h("span", { class: "meta-value" }, value));
}

function confirmStop(ctx) {
  const reason = h("input", { id: "stop-reason", type: "text", placeholder: "optional — recorded in the audit trail", maxlength: "300" });
  const err = fieldError();
  modal({
    title: "Safe-stop this run?",
    body: h(
      "div",
      { class: "form" },
      h("p", null, "The engine halts at the next checkpoint. Completed work and state are preserved, and the run can be resumed later."),
      h("div", { class: "field" }, h("label", { for: "stop-reason" }, "Reason"), reason),
      err.el,
    ),
    actions: [
      { label: "Cancel" },
      {
        label: "Stop run",
        kind: "danger",
        onClick: async () => {
          const text = reason.value.trim();
          if (text && text.length < 3) {
            err.show("Reason must be at least 3 characters (or leave it empty).");
            return true;
          }
          try {
            await api.stop(ctx.runId, text);
            toast("Stop requested — the run will halt at the next safe point", "ok");
            ctx.refresh();
            return false;
          } catch (e) {
            err.show(e.message);
            if (e.status === 401) reportError(e);
            return true;
          }
        },
      },
    ],
  });
}

async function doResume(ctx, last) {
  const status = last?.data?.run?.status;
  const go = async () => {
    try {
      await api.resume(ctx.runId);
      toast("Run resumed in the dashboard process", "ok");
      ctx.refresh();
    } catch (e) {
      reportError(e, "Resume");
    }
  };
  if (status === "running") {
    modal({
      title: "Resume a run marked as running?",
      body: h(
        "p",
        null,
        "This run is marked running but is not executing in this dashboard process. If it is still being driven by a CLI process, resuming here would execute it twice. Only continue if that process is gone.",
      ),
      actions: [{ label: "Cancel" }, { label: "Resume anyway", kind: "danger", onClick: go }],
    });
    return;
  }
  await go();
}

function openAmend(ctx) {
  const text = h("textarea", {
    id: "amend-text",
    rows: "6",
    placeholder: "Describe the change, e.g. “Links must expire after 30 days by default.”",
  });
  const err = fieldError();
  modal({
    title: "Amend the requirement",
    body: h(
      "div",
      { class: "form" },
      h(
        "p",
        { class: "muted" },
        "The amendment is appended to the requirement. The engine re-plans: affected nodes are invalidated and re-run, unaffected results are reused.",
      ),
      h("div", { class: "field" }, h("label", { for: "amend-text" }, "Amendment"), text),
      err.el,
    ),
    actions: [
      { label: "Cancel" },
      {
        label: "Submit amendment",
        kind: "primary",
        onClick: async () => {
          const value = text.value.trim();
          if (value.length < 3) {
            err.show("Please write at least 3 characters.");
            return true;
          }
          try {
            const res = await api.amend(ctx.runId, value);
            toast(`Amendment ${res.amendment_id} recorded`, "ok");
            ctx.refresh();
            return false;
          } catch (e) {
            err.show(e.message);
            if (e.status === 401) reportError(e);
            return true;
          }
        },
      },
    ],
  });
}
