// Report tab: the engineering summary (markdown) rendered client-side, with download.

import { api } from "../api.js";
import { h, clear, renderMarkdown, downloadText, emptyState } from "../util.js";
import { asyncButton, reportError } from "../ui.js";

export function createReportView(ctx) {
  const el = h("div", { class: "report-view" });
  const status = h("span", { class: "muted small" });
  const doc = h("article", { class: "markdown report-doc" }, h("div", { class: "loading" }, "Generating report…"));
  let text = null;
  let lastRunSig = null;
  let inflight = false;

  const refreshBtn = asyncButton("↻ Refresh", () => load(true), { kind: "secondary" });
  const dlBtn = h("button", { type: "button", class: "btn btn-primary" }, "⤓ Download .md");
  dlBtn.disabled = true;
  dlBtn.addEventListener("click", () => {
    if (text !== null) downloadText(`ENGINEERING_SUMMARY-${ctx.runId}.md`, text);
  });
  el.append(h("div", { class: "report-toolbar" }, status, h("div", { class: "tl-right" }, refreshBtn, dlBtn)), doc);

  async function load(force = false) {
    if (inflight) return;
    inflight = true;
    try {
      const next = await api.report(ctx.runId);
      if (force || next !== text) {
        text = next;
        clear(doc);
        if (text.trim()) doc.append(renderMarkdown(text));
        else doc.append(emptyState("Report is empty"));
        dlBtn.disabled = false;
      }
      status.textContent = `Generated from the audit trail · updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      reportError(err, "Loading report");
      if (text === null) clear(doc).append(emptyState("Report unavailable", err.message));
    } finally {
      inflight = false;
    }
  }

  return {
    el,
    update(data) {
      // Re-generate only when something material changed (it's rebuilt server-side each call).
      const runSig = JSON.stringify([data.run.status, data.nodes.map((n) => [n.status, n.attempts]), data.approvals.length]);
      if (runSig !== lastRunSig) {
        lastRunSig = runSig;
        load(false);
      }
    },
  };
}
