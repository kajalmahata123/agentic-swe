// Approval cards (per kind), the run's Approvals tab, and the global inbox.
//
// Cards are keyed by approval id and created once: polling never rebuilds a pending
// card, so typed comments / chosen answers survive refreshes.

import { api } from "../api.js";
import {
  h,
  clear,
  sig,
  statusPill,
  fmtRelative,
  fmtDate,
  fmtDuration,
  renderDiff,
  renderMarkdown,
  jsonTree,
  emptyState,
  humanize,
  truncate,
  DASH,
} from "../util.js";
import { toast, reportError, fieldError } from "../ui.js";

const KIND_META = {
  clarification: { icon: "?", label: "Clarification", hint: "Answer the blocking questions so design can proceed." },
  design: { icon: "◫", label: "Design review", hint: "Approve the architecture, API contract and work breakdown." },
  change: { icon: "Δ", label: "Change control", hint: "A protected path or dependency was modified." },
  policy_exception: { icon: "⚑", label: "Policy exception", hint: "The policy scanner flagged this change; approve to allow it." },
  escalation: { icon: "!", label: "Escalation", hint: "A node exhausted its retries. Retry with guidance, skip it, or abort the run." },
  release: { icon: "⇪", label: "Release sign-off", hint: "Final human gate before the release is published." },
};

const GENERIC_KEYS = new Set(["gates", "diff", "outputs_preview", "metrics", "attempt"]);
const KIND_KEYS = {
  clarification: ["questions", "requirement"],
  design: ["api_contract", "work_packages", "decisions", "risks"],
  change: ["finding", "files", "reason"],
  policy_exception: ["finding", "files", "reason"],
  escalation: ["last_error", "feedback", "attempts", "options"],
  release: ["checklist", "release_notes"],
};

let uid = 0;
const nextId = (p) => `${p}-${++uid}`;

// ------------------------------------------------------------------ payload sections
function block(title, ...content) {
  return h("div", { class: "ap-block" }, h("h4", null, title), ...content);
}

function gatesBlock(gates, title = "Gate results") {
  if (!Array.isArray(gates) || !gates.length) return null;
  const failed = gates.filter((g) => !g.passed).length;
  return block(
    `${title} (${gates.length - failed}/${gates.length} passed)`,
    h(
      "ul",
      { class: "check-list" },
      gates.map((g) =>
        h(
          "li",
          { class: g.passed ? "ok" : g.severity === "block" ? "bad" : "warn" },
          h("span", { class: "check-mark", "aria-hidden": "true" }, g.passed ? "✓" : "✗"),
          h("span", { class: "sr-only" }, g.passed ? "passed: " : "failed: "),
          h("span", { class: "mono" }, g.name),
          g.detail ? h("span", { class: "muted check-detail" }, g.detail) : null,
        ),
      ),
    ),
  );
}

function designBody(p) {
  const parts = [];
  if (Array.isArray(p.api_contract) && p.api_contract.length) {
    parts.push(block("API contract", h("ul", { class: "plain-list mono" }, p.api_contract.map((x) => h("li", null, typeof x === "string" ? x : JSON.stringify(x))))));
  }
  if (Array.isArray(p.work_packages) && p.work_packages.length) {
    parts.push(
      block(
        `Work packages (${p.work_packages.length})`,
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "data-table" },
            h("thead", null, h("tr", null, ["ID", "Title", "Files", "Depends on", "Risk"].map((c) => h("th", null, c)))),
            h(
              "tbody",
              null,
              p.work_packages.map((wp) =>
                h(
                  "tr",
                  null,
                  h("td", { class: "mono" }, wp.id),
                  h("td", null, wp.title || DASH),
                  h("td", { class: "mono small" }, (wp.files || []).join(", ") || DASH),
                  h("td", { class: "mono small" }, (wp.depends_on || []).join(", ") || DASH),
                  h("td", null, wp.risk ? h("span", { class: `tag risk-${wp.risk}` }, wp.risk) : DASH),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
  if (Array.isArray(p.decisions) && p.decisions.length) {
    parts.push(block("Architecture decisions", h("ul", { class: "plain-list" }, p.decisions.map((d) => h("li", null, typeof d === "string" ? d : `${d.id || ""} ${d.title || ""} — ${d.decision || ""}`)))));
  }
  if (Array.isArray(p.risks) && p.risks.length) {
    parts.push(
      block(
        "Risks",
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "data-table" },
            h("thead", null, h("tr", null, ["ID", "Risk", "Likelihood", "Impact", "Mitigation"].map((c) => h("th", null, c)))),
            h(
              "tbody",
              null,
              p.risks.map((r) =>
                typeof r === "string"
                  ? h("tr", null, h("td", { colspan: "5" }, r))
                  : h(
                      "tr",
                      null,
                      h("td", { class: "mono" }, r.id || DASH),
                      h("td", null, r.description || DASH),
                      h("td", null, r.likelihood || DASH),
                      h("td", null, r.impact || DASH),
                      h("td", null, r.mitigation || DASH),
                    ),
              ),
            ),
          ),
        ),
      ),
    );
  }
  return parts;
}

function changeBody(p) {
  const parts = [];
  const f = p.finding;
  if (f) {
    parts.push(
      block(
        "Finding",
        h(
          "div",
          { class: "finding" },
          h("div", null, h("span", { class: "tag tag-warn mono" }, f.rule), " ", h("strong", null, f.title)),
          h(
            "div",
            { class: "muted small" },
            `severity: ${f.severity || DASH} · `,
            h("span", { class: "mono" }, `${f.path || "?"}${f.line ? `:${f.line}` : ""}`),
          ),
          f.snippet ? h("pre", { class: "snippet" }, f.snippet) : null,
        ),
      ),
    );
  }
  if (Array.isArray(p.files) && p.files.length) {
    parts.push(block("Files", h("ul", { class: "plain-list mono" }, p.files.map((x) => h("li", null, x))), p.reason ? h("p", { class: "muted" }, p.reason) : null));
  }
  return parts;
}

function releaseBody(p) {
  const parts = [];
  if (Array.isArray(p.checklist)) parts.push(gatesBlock(p.checklist, "Release checklist"));
  if (p.release_notes) {
    const md = h("div", { class: "markdown md-compact" });
    md.append(renderMarkdown(p.release_notes));
    parts.push(block("Release notes", md));
  }
  return parts;
}

function escalationInfo(p) {
  const parts = [];
  if (p.last_error) parts.push(block(`Last error${p.attempts ? ` (after ${p.attempts} attempts)` : ""}`, h("pre", { class: "pre-wrap error-text" }, p.last_error)));
  if (Array.isArray(p.feedback) && p.feedback.length) {
    parts.push(block("Recent feedback to the agent", h("ol", { class: "plain-list" }, p.feedback.map((f) => h("li", null, h("pre", { class: "pre-wrap" }, typeof f === "string" ? f : JSON.stringify(f)))))));
  }
  return parts;
}

function genericExtras(a) {
  const p = a.payload || {};
  const parts = [];
  if (p.gates && a.kind !== "release") parts.push(gatesBlock(p.gates));
  if (typeof p.diff === "string" && p.diff.trim()) {
    const det = h("details", { class: "ap-diff", open: a.kind === "change" || a.kind === "policy_exception" });
    det.append(h("summary", null, "Proposed diff"), renderDiff(p.diff));
    parts.push(det);
  }
  const known = new Set([...GENERIC_KEYS, ...(KIND_KEYS[a.kind] || [])]);
  const rest = Object.fromEntries(Object.entries(p).filter(([k]) => !known.has(k)));
  const raw = {};
  if (p.outputs_preview) raw.outputs_preview = p.outputs_preview;
  if (p.metrics) raw.metrics = p.metrics;
  if (p.attempt !== undefined) raw.attempt = p.attempt;
  Object.assign(raw, rest);
  if (Object.keys(raw).length) {
    const det = h("details", { class: "ap-raw" });
    det.append(h("summary", null, "More context (outputs preview, metrics)"), jsonTree(raw, { openDepth: 1 }));
    parts.push(det);
  }
  return parts;
}

// ------------------------------------------------------------------ forms
function clarificationForm(a, submit) {
  const questions = (a.payload && a.payload.questions) || [];
  const form = h("form", { class: "ap-form", novalidate: true });
  const getters = [];
  questions.forEach((q) => {
    const name = nextId(`q-${q.id}`);
    const options = Array.isArray(q.options) ? q.options : [];
    const fs = h(
      "fieldset",
      { class: "question" },
      h("legend", null, h("span", { class: "tag mono" }, q.id), " ", q.question),
      q.why ? h("div", { class: "muted small" }, `Why it matters: ${q.why}`) : null,
    );
    const otherText = h("input", { type: "text", class: "other-text", placeholder: "Your own answer…", "aria-label": `Custom answer for ${q.id}` });
    const radios = [];
    const defaultInOptions = q.default !== null && q.default !== undefined && options.includes(q.default);
    options.forEach((opt) => {
      const id = nextId("opt");
      const r = h("input", { type: "radio", name, id, value: opt, checked: defaultInOptions ? opt === q.default : false });
      radios.push(r);
      fs.append(
        h("label", { class: "radio", for: id }, r, h("span", null, opt), opt === q.default ? h("span", { class: "tag tag-muted" }, "default") : null),
      );
    });
    const otherId = nextId("opt");
    const otherRadio = h("input", { type: "radio", name, id: otherId, value: "__other__", checked: !defaultInOptions });
    radios.push(otherRadio);
    if (!defaultInOptions && q.default) otherText.value = String(q.default);
    otherText.addEventListener("focus", () => (otherRadio.checked = true));
    fs.append(h("label", { class: "radio radio-other", for: otherId }, otherRadio, h("span", null, options.length ? "Other:" : "Answer:"), otherText));
    form.append(fs);
    getters.push(() => {
      const chosen = radios.find((r) => r.checked);
      const value = !chosen || chosen.value === "__other__" ? otherText.value.trim() : chosen.value;
      return [q.id, value];
    });
  });
  const comment = h("textarea", { rows: "2", placeholder: "Optional note for the audit trail / reject reason", "aria-label": "Comment" });
  const err = fieldError();
  form.append(h("div", { class: "field" }, h("label", null, "Comment"), comment), err.el);
  const approveBtn = h("button", { type: "submit", class: "btn btn-primary" }, "Submit answers");
  const rejectBtn = h("button", { type: "button", class: "btn btn-danger-outline" }, "Reject");
  form.append(h("div", { class: "ap-actions" }, approveBtn, rejectBtn));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.hide();
    const answers = {};
    for (const g of getters) {
      const [id, v] = g();
      if (!v) {
        err.show(`Please answer ${id}.`);
        return;
      }
      answers[id] = v;
    }
    submit({ approve: true, body: { answers, comment: comment.value.trim() } }, err, [approveBtn, rejectBtn]);
  });
  rejectBtn.addEventListener("click", () => {
    err.hide();
    const c = comment.value.trim();
    if (c.length < 3) {
      err.show("A reject needs a reason (at least 3 characters) in the comment box.");
      comment.focus();
      return;
    }
    submit({ approve: false, comment: c }, err, [approveBtn, rejectBtn]);
  });
  return form;
}

function escalationForm(a, submit) {
  const form = h("form", { class: "ap-form", novalidate: true });
  const name = nextId("esc");
  const opts = (a.payload && a.payload.options) || ["retry", "skip", "abort"];
  const desc = {
    retry: "Retry the node; your guidance is fed to the agent.",
    skip: "Skip this node and continue (downstream may degrade).",
    abort: "Abort: reject and stop the run safely.",
  };
  const radios = opts.map((o, i) => h("input", { type: "radio", name, value: o, id: nextId("esc-opt"), checked: i === 0 }));
  form.append(
    h(
      "fieldset",
      { class: "question" },
      h("legend", null, "Action"),
      radios.map((r) =>
        h("label", { class: `radio radio-${r.value}`, for: r.id }, r, h("strong", null, r.value), h("span", { class: "muted small" }, desc[r.value] || "")),
      ),
    ),
  );
  const guidance = h("textarea", { rows: "3", placeholder: "Guidance for the agent (e.g. “the test expects 201, not 200”) or reason for skipping/aborting", "aria-label": "Guidance" });
  const err = fieldError();
  form.append(h("div", { class: "field" }, h("label", null, "Guidance / comment"), guidance), err.el);
  const btn = h("button", { type: "submit", class: "btn btn-primary" }, "Submit decision");
  form.append(h("div", { class: "ap-actions" }, btn));
  const syncLabel = () => {
    const action = radios.find((r) => r.checked)?.value;
    btn.textContent = action === "abort" ? "Abort run" : action === "skip" ? "Skip node" : "Retry with guidance";
    btn.className = `btn ${action === "abort" ? "btn-danger" : "btn-primary"}`;
  };
  radios.forEach((r) => r.addEventListener("change", syncLabel));
  syncLabel();
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.hide();
    const action = radios.find((r) => r.checked)?.value || "retry";
    const text = guidance.value.trim();
    if (action === "abort") {
      const reason = text.length >= 3 ? text : "operator chose abort";
      submit({ approve: false, comment: reason }, err, [btn]);
    } else {
      submit({ approve: true, body: { action, comment: text } }, err, [btn]);
    }
  });
  return form;
}

function standardForm(a, submit) {
  const form = h("form", { class: "ap-form", novalidate: true });
  const comment = h("textarea", { rows: "2", placeholder: "Comment (optional to approve, required to reject — it is fed back to the agent)", "aria-label": "Comment" });
  const err = fieldError();
  const approveBtn = h("button", { type: "submit", class: "btn btn-primary" }, a.kind === "release" ? "Approve release" : "Approve");
  const rejectBtn = h("button", { type: "button", class: "btn btn-danger-outline" }, "Reject");
  form.append(h("div", { class: "field" }, h("label", null, "Comment"), comment), err.el, h("div", { class: "ap-actions" }, approveBtn, rejectBtn));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    err.hide();
    submit({ approve: true, body: { comment: comment.value.trim() } }, err, [approveBtn, rejectBtn]);
  });
  rejectBtn.addEventListener("click", () => {
    err.hide();
    const c = comment.value.trim();
    if (c.length < 3) {
      err.show("Rejecting requires a reason of at least 3 characters — the agent uses it as feedback.");
      comment.focus();
      return;
    }
    submit({ approve: false, comment: c }, err, [approveBtn, rejectBtn]);
  });
  return form;
}

// ------------------------------------------------------------------ card
export function approvalCard(a, { showRun = false, runHash = null, onDecided = () => {}, byRun = null } = {}) {
  const runInfo = byRun ? byRun.get(a.run_id) : null;
  const meta = KIND_META[a.kind] || { icon: "•", label: humanize(a.kind), hint: "" };
  const waitEl = h("span", { class: "muted ap-wait", title: fmtDate(a.requested_at) }, `waiting ${fmtRelative(a.requested_at).replace(" ago", "")}`);
  const card = h("article", { class: `ap-card kind-${a.kind}`, dataset: { approval: a.id }, "aria-label": `${meta.label} for ${a.node_id}` });
  card.append(
    h(
      "header",
      { class: "ap-head" },
      h("span", { class: `ap-kind kind-${a.kind}` }, h("span", { class: "ap-icon", "aria-hidden": "true" }, meta.icon), meta.label),
      h("span", { class: "mono ap-node", title: "Node" }, a.node_id),
      showRun && runHash
        ? h("a", { class: "ap-run", href: runHash(a.run_id, "approvals"), title: "Open this run" }, runInfo ? `${runInfo.scenario} · ` : "", h("span", { class: "mono" }, a.run_id))
        : null,
      waitEl,
    ),
    h("p", { class: "ap-summary" }, a.summary),
    meta.hint ? h("p", { class: "muted small ap-hint" }, meta.hint) : null,
  );
  const p = a.payload || {};
  const body = h("div", { class: "ap-body" });
  let parts = [];
  if (a.kind === "design") parts = designBody(p);
  else if (a.kind === "change" || a.kind === "policy_exception") parts = changeBody(p);
  else if (a.kind === "release") parts = releaseBody(p);
  else if (a.kind === "escalation") parts = escalationInfo(p);
  else if (a.kind === "clarification" && p.requirement) parts = [block("Requirement", h("p", { class: "pre-wrap" }, truncate(p.requirement, 1200)))];
  body.append(...parts.filter(Boolean), ...genericExtras(a).filter(Boolean));
  card.append(body);

  const submit = async (decision, err, buttons) => {
    buttons.forEach((b) => (b.disabled = true));
    try {
      if (decision.approve) await api.approve(a.id, decision.body);
      else await api.reject(a.id, decision.comment);
      card.classList.add("decided");
      card.querySelector(".ap-form")?.replaceWith(
        h("div", { class: `ap-done ${decision.approve ? "ok" : "bad"}`, role: "status" }, decision.approve ? "✓ Approved — the engine will continue." : "✗ Rejected — feedback sent to the engine."),
      );
      toast(`${meta.label} ${decision.approve ? "approved" : "rejected"} (${a.node_id})`, "ok");
      onDecided(a);
    } catch (e) {
      err.show(e.message);
      if (e.status === 401) reportError(e);
      buttons.forEach((b) => (b.disabled = false));
    }
  };
  if (a.status === "pending") {
    const form =
      a.kind === "clarification" ? clarificationForm(a, submit) : a.kind === "escalation" ? escalationForm(a, submit) : standardForm(a, submit);
    card.append(form);
  }
  card._tick = () => (waitEl.textContent = `waiting ${fmtRelative(a.requested_at).replace(" ago", "")}`);
  return card;
}

/** Keyed reconciliation of pending cards into `container` (never rebuilds existing cards). */
function syncCards(container, approvals, opts, emptyEl) {
  if (!container._cards) container._cards = new Map();
  const cards = container._cards;
  const ids = new Set(approvals.map((a) => a.id));
  for (const [id, card] of cards) {
    if (!ids.has(id)) {
      card.remove();
      cards.delete(id);
    }
  }
  approvals
    .slice()
    .sort((x, y) => x.requested_at - y.requested_at)
    .forEach((a) => {
      let card = cards.get(a.id);
      if (!card) {
        card = approvalCard(a, opts);
        cards.set(a.id, card);
      } else if (card._tick) card._tick();
      container.append(card); // re-append keeps order without recreating
    });
  if (emptyEl) emptyEl.hidden = approvals.length > 0;
}

function responseText(a) {
  const r = a.response || {};
  if (r.answers) return Object.entries(r.answers).map(([k, v]) => `${k}: ${v}`).join("; ");
  if (r.action) return `action: ${r.action}${r.guidance ? ` — ${r.guidance}` : ""}`;
  return "";
}

function historyTable(decided) {
  if (!decided.length) return emptyState("No decisions yet");
  return h(
    "div",
    { class: "table-wrap" },
    h(
      "table",
      { class: "data-table history" },
      h("thead", null, h("tr", null, ["Kind", "Node", "Decision", "By", "Comment / response", "Wait", "Decided"].map((c) => h("th", null, c)))),
      h(
        "tbody",
        null,
        decided
          .slice()
          .sort((x, y) => (y.decided_at || y.requested_at) - (x.decided_at || x.requested_at))
          .map((a) =>
            h(
              "tr",
              null,
              h("td", null, (KIND_META[a.kind] || {}).label || a.kind),
              h("td", { class: "mono" }, a.node_id),
              h("td", null, statusPill(a.status, "pill-sm")),
              h("td", { class: "mono small" }, a.decided_by || DASH),
              h(
                "td",
                { class: "small" },
                a.comment || DASH,
                responseText(a) ? h("div", { class: "muted" }, responseText(a)) : null,
              ),
              h("td", null, a.decided_at ? fmtDuration(a.decided_at - a.requested_at) : DASH),
              h("td", { class: "small", title: fmtDate(a.decided_at) }, fmtRelative(a.decided_at)),
            ),
          ),
      ),
    ),
  );
}

// ------------------------------------------------------------------ run tab
export function createApprovalsView(ctx) {
  const el = h("div", { class: "approvals-view" });
  const pendingHost = h("div", { class: "ap-list" });
  const emptyEl = emptyState("Nothing waiting for you", "Pending checkpoints for this run will appear here.");
  const historyHost = h("div");
  el.append(
    h("h2", { class: "view-title" }, "Pending approvals"),
    emptyEl,
    pendingHost,
    h("h2", { class: "view-title" }, "Decision history"),
    historyHost,
  );
  let histSig = null;
  return {
    el,
    update(data) {
      const pending = data.approvals.filter((a) => a.status === "pending");
      syncCards(pendingHost, pending, { onDecided: () => ctx.refresh() }, emptyEl);
      const decided = data.approvals.filter((a) => a.status !== "pending");
      const key = sig(decided);
      if (key !== histSig) {
        histSig = key;
        clear(historyHost).append(historyTable(decided));
      }
    },
  };
}

// ------------------------------------------------------------------ global inbox
export function createInboxView(ctx) {
  const el = h("div", { class: "inbox-view" });
  const count = h("span", { class: "badge badge-amber" }, "0");
  const list = h("div", { class: "ap-list" });
  const emptyEl = emptyState("Inbox zero", "No run is waiting on a human decision right now.");
  el.append(
    h(
      "div",
      { class: "inbox-head" },
      h("h1", null, "Approvals inbox ", count),
      h("p", { class: "muted" }, "Every pending human checkpoint across all runs, longest-waiting first."),
    ),
    emptyEl,
    list,
  );
  return {
    el,
    update(approvals, runs) {
      count.textContent = String(approvals.length);
      const byRun = new Map((runs || []).map((r) => [r.id, r]));
      syncCards(list, approvals, { showRun: true, runHash: ctx.runHash, onDecided: () => ctx.refresh(), byRun }, emptyEl);
    },
  };
}
