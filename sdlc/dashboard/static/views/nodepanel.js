// Node detail side panel: spec, runtime state, gate results / attempt history (from the
// audit trail), merged diff, and the agent transcript.

import { api } from "../api.js";
import {
  h,
  clear,
  sig,
  kv,
  statusPill,
  fmtDate,
  fmtDuration,
  fmtUsd,
  fmtClock,
  shortHash,
  humanize,
  renderDiff,
  jsonTree,
  emptyState,
  tryParseJson,
  DASH,
  KIND_ICON,
} from "../util.js";
import { section, reportError } from "../ui.js";

const NODE_EVENT_TYPES = [
  "gate.entry",
  "gate.result",
  "node.attempt_started",
  "node.attempt_failed",
  "node.succeeded",
  "node.rolled_back",
  "node.recovered",
  "node.superseded",
  "node.skipped",
  "node.reused",
];

async function fetchAllEvents(runId, types) {
  const out = [];
  let after = 0;
  for (let page = 0; page < 20; page += 1) {
    const batch = await api.events(runId, { after, types, limit: 5000 });
    out.push(...batch);
    if (batch.length < 5000) break;
    after = batch[batch.length - 1].seq;
  }
  return out;
}

function tags(list) {
  if (!list || !list.length) return DASH;
  return h("span", { class: "tag-list" }, list.map((x) => h("span", { class: "tag mono" }, x)));
}

function specSection(n) {
  return kv([
    ["Title", n.title || DASH],
    ["Stage", n.stage],
    ["Kind", h("span", null, `${KIND_ICON[n.kind] || ""} ${n.kind}`)],
    ["Handler", h("code", null, n.handler)],
    ["Depends on", tags(n.deps)],
    ["Inputs", tags(n.inputs)],
    ["Outputs", tags(n.outputs)],
    ["Write scope", n.write_scope && n.write_scope.length ? tags(n.write_scope) : h("span", { class: "muted" }, "read-only")],
    ["Risk", h("span", { class: `tag risk-${n.risk}` }, n.risk)],
    ["Approval", n.approval ? h("span", { class: "tag tag-warn" }, `✋ ${n.approval}`) : h("span", { class: "muted" }, "none")],
    [
      "Retry policy",
      n.retry
        ? `${n.retry.max_attempts} attempt(s), backoff ${n.retry.backoff_s}s, stronger model from attempt ${n.retry.escalate_model_at}`
        : DASH,
    ],
    ["On failure", humanize(n.on_failure || "escalate")],
    ["Timeout", fmtDuration(n.timeout_s)],
    ["Created by", n.created_by ? h("span", { class: "tag tag-dyn" }, n.created_by) : h("span", { class: "muted" }, "static template")],
    ["Params", n.params && Object.keys(n.params).length ? jsonTree(n.params, { openDepth: 0, label: "params" }) : DASH],
  ]);
}

function stateSection(n) {
  const dur = n.started_at && n.finished_at ? n.finished_at - n.started_at : null;
  return h(
    "div",
    null,
    kv([
      ["Status", statusPill(n.status)],
      ["Attempts", String(n.attempts)],
      ["Cost", fmtUsd(n.cost_usd)],
      ["Started", fmtDate(n.started_at)],
      ["Finished", fmtDate(n.finished_at)],
      ["Duration", fmtDuration(dur)],
      ["Base sha", n.base_sha ? h("code", { title: n.base_sha }, shortHash(n.base_sha, 12)) : DASH],
      ["Merge sha", n.merge_sha ? h("code", { title: n.merge_sha }, shortHash(n.merge_sha, 12)) : DASH],
    ]),
    n.summary ? h("div", { class: "callout" }, h("div", { class: "callout-label" }, "Summary"), h("div", { class: "pre-wrap" }, n.summary)) : null,
    n.error ? h("div", { class: "callout callout-error" }, h("div", { class: "callout-label" }, n.status === "succeeded" ? "Error (from an earlier attempt)" : "Error"), h("pre", { class: "pre-wrap" }, n.error)) : null,
  );
}

function historySection(events) {
  if (!events.length) return emptyState("No attempts recorded yet");
  const byAttempt = new Map();
  const misc = [];
  events.forEach((e) => {
    if (e.attempt === null || e.attempt === undefined) {
      misc.push(e);
      return;
    }
    if (!byAttempt.has(e.attempt)) byAttempt.set(e.attempt, []);
    byAttempt.get(e.attempt).push(e);
  });
  const wrap = h("div", { class: "attempts" });
  // Entry gates (attempt-less) first
  const entry = misc.filter((e) => e.type === "gate.entry");
  if (entry.length) {
    const last = entry[entry.length - 1];
    wrap.append(h("div", { class: "attempt" }, h("div", { class: "attempt-head" }, h("strong", null, "Entry gate"), h("span", { class: "muted" }, fmtClock(last.ts))), gateTable(last.data.results || [])));
  }
  [...byAttempt.keys()]
    .sort((a, b) => a - b)
    .forEach((att) => {
      const evs = byAttempt.get(att);
      const started = evs.find((e) => e.type === "node.attempt_started");
      const failed = evs.find((e) => e.type === "node.attempt_failed");
      const ok = evs.find((e) => e.type === "node.succeeded");
      const superseded = evs.find((e) => e.type === "node.superseded");
      const gates = evs.filter((e) => e.type === "gate.result").map((e) => e.data);
      const verdict = failed ? "failed" : ok ? "succeeded" : superseded ? "skipped" : "running";
      const block = h(
        "div",
        { class: `attempt att-${verdict}` },
        h(
          "div",
          { class: "attempt-head" },
          h("strong", null, `Attempt ${att}`),
          statusPill(verdict, "pill-sm"),
          started && started.data.model ? h("span", { class: "tag mono" }, started.data.model) : null,
          h("span", { class: "muted" }, started ? fmtClock(started.ts) : ""),
        ),
      );
      if (gates.length) block.append(gateTable(gates));
      if (failed) {
        block.append(
          h(
            "div",
            { class: "callout callout-error" },
            h(
              "div",
              { class: "callout-label" },
              `Failure · ${failed.data.kind || "error"}`,
              failed.data.duration_s !== undefined ? ` · ${fmtDuration(failed.data.duration_s)}` : "",
              failed.data.rolled_back ? " · rolled back" : "",
            ),
            h("pre", { class: "pre-wrap" }, failed.data.error || DASH),
          ),
        );
      }
      if (superseded) block.append(h("div", { class: "callout" }, `Superseded by ${superseded.data.by || "repair loop"}`));
      if (ok) {
        block.append(
          h(
            "div",
            { class: "muted small" },
            `Succeeded in ${fmtDuration(ok.data.duration_s)}${ok.data.outputs ? ` · outputs: ${Object.keys(ok.data.outputs).join(", ") || "none"}` : ""}`,
          ),
        );
      }
      wrap.append(block);
    });
  misc
    .filter((e) => e.type !== "gate.entry")
    .forEach((e) => wrap.append(h("div", { class: "muted small" }, `${fmtClock(e.ts)} · ${e.type} ${e.data.reason || e.data.by || ""}`)));
  return wrap;
}

function gateTable(gates) {
  if (!gates.length) return null;
  return h(
    "table",
    { class: "gate-table" },
    h("thead", null, h("tr", null, h("th", null, "Gate"), h("th", null, "Result"), h("th", null, "Detail"))),
    h(
      "tbody",
      null,
      gates.map((g) =>
        h(
          "tr",
          { class: g.passed ? "gate-pass" : g.severity === "block" ? "gate-fail" : "gate-warn" },
          h("td", { class: "mono" }, g.name),
          h("td", null, h("span", { class: `tag ${g.passed ? "tag-ok" : g.severity === "block" ? "tag-err" : "tag-warn"}` }, g.passed ? "✓ pass" : `✗ ${g.severity === "block" ? "fail" : g.severity || "warn"}`)),
          h("td", { class: "gate-detail" }, g.detail || ""),
        ),
      ),
    ),
  );
}

function renderTranscript(turns) {
  if (!turns.length) return emptyState("No transcript", "Only LLM agent nodes record transcripts.");
  const list = h("ol", { class: "transcript" });
  turns.forEach((t) => {
    if (t.type === "start") {
      const det = h("details", { class: "t-prompt" }, h("summary", null, `Prompt (${(t.prompt || "").length.toLocaleString()} chars)`), h("pre", { class: "pre-wrap" }, t.prompt || ""));
      list.append(
        h(
          "li",
          { class: "t-start" },
          h("div", { class: "t-head" }, h("strong", null, "Session start"), t.role ? h("span", { class: "tag" }, t.role) : null, t.model ? h("span", { class: "tag mono" }, t.model) : null),
          det,
        ),
      );
      return;
    }
    const li = h(
      "li",
      { class: "t-turn" },
      h(
        "div",
        { class: "t-head" },
        h("strong", null, `Turn ${t.turn ?? "?"}`),
        t.stop ? h("span", { class: "tag" }, t.stop) : null,
        t.latency_s !== undefined ? h("span", { class: "muted" }, fmtDuration(t.latency_s)) : null,
        t.cost_usd !== undefined ? h("span", { class: "muted" }, fmtUsd(t.cost_usd)) : null,
      ),
    );
    if (t.text) li.append(h("div", { class: "t-text pre-wrap" }, t.text));
    (t.calls || []).forEach((c, i) => {
      const res = (t.results || [])[i];
      li.append(
        h(
          "div",
          { class: "t-call" },
          h("div", { class: "t-call-head" }, h("span", { class: "t-arrow", "aria-hidden": "true" }, "→"), h("code", null, c.name)),
          c.input !== undefined ? jsonTree(tryParseJson(c.input), { openDepth: 1, label: "input" }) : null,
          res
            ? h(
                "div",
                { class: `t-result${res.error ? " t-err" : ""}` },
                h("span", { class: "t-arrow", "aria-hidden": "true" }, "←"),
                res.error ? h("span", { class: "tag tag-err" }, "error") : null,
                h("pre", { class: "pre-wrap" }, typeof res.content === "string" ? res.content : JSON.stringify(res.content, null, 2)),
              )
            : null,
        ),
      );
    });
    list.append(li);
  });
  return list;
}

export function createNodePanel(ctx, onClose) {
  const el = h("aside", { class: "node-panel", "aria-label": "Node details", hidden: true });
  const head = h("div", { class: "np-head" });
  const body = h("div", { class: "np-body" });
  el.append(head, body);
  const store = ctx.ui.sections;

  let currentId = null;
  let staticSig = null;
  let fetchSig = null;
  let fetchToken = 0;
  let transcriptAttempt = null;

  const hosts = {
    spec: h("div"),
    state: h("div"),
    history: h("div", null, h("div", { class: "loading" }, "Loading…")),
    diff: h("div", null, h("div", { class: "loading" }, "Loading…")),
    transcript: h("div", null, h("div", { class: "loading" }, "Loading…")),
  };
  const attemptSel = h("select", { class: "select-small", "aria-label": "Transcript attempt" });
  attemptSel.addEventListener("change", () => {
    transcriptAttempt = attemptSel.value ? Number(attemptSel.value) : null;
    loadTranscript();
  });
  attemptSel.addEventListener("click", (e) => e.stopPropagation());

  function build() {
    clear(body);
    body.append(
      section("State", hosts.state, { key: "np-state", store }),
      section("Gates & attempts", hosts.history, { key: "np-history", store }),
      section("Merged diff", hosts.diff, { key: "np-diff", store, open: false }),
      section("Agent transcript", hosts.transcript, { key: "np-transcript", store, open: false, extra: attemptSel }),
      section("Spec", hosts.spec, { key: "np-spec", store, open: false }),
    );
  }

  async function loadTranscript() {
    const id = currentId;
    const token = fetchToken;
    try {
      const turns = await api.transcript(ctx.runId, id, transcriptAttempt);
      if (id !== currentId || token !== fetchToken) return;
      clear(hosts.transcript).append(renderTranscript(turns));
    } catch (err) {
      if (id === currentId) clear(hosts.transcript).append(emptyState("Transcript unavailable", err.message));
    }
  }

  async function loadDynamic(node) {
    const id = node.id;
    const token = ++fetchToken;
    const [evs, diff] = await Promise.allSettled([
      fetchAllEvents(ctx.runId, NODE_EVENT_TYPES),
      node.merge_sha ? api.nodeDiff(ctx.runId, id) : Promise.resolve(""),
    ]);
    if (id !== currentId || token !== fetchToken) return;
    if (evs.status === "fulfilled") {
      clear(hosts.history).append(historySection(evs.value.filter((e) => e.node_id === id)));
    } else {
      clear(hosts.history).append(emptyState("Could not load events", evs.reason.message));
    }
    if (diff.status === "fulfilled") {
      clear(hosts.diff).append(
        node.merge_sha || diff.value
          ? renderDiff(diff.value)
          : emptyState("No merged diff", node.write_scope && node.write_scope.length ? "Nothing merged yet." : "This node is read-only."),
      );
    } else {
      clear(hosts.diff).append(emptyState("Diff unavailable", diff.reason.message));
    }
    await loadTranscript();
  }

  function show(node) {
    el.hidden = false;
    if (node.id !== currentId) {
      currentId = node.id;
      staticSig = null;
      fetchSig = null;
      transcriptAttempt = null;
      build();
      [hosts.history, hosts.diff, hosts.transcript].forEach((x) => clear(x).append(h("div", { class: "loading" }, "Loading…")));
      body.scrollTop = 0;
    }
    const sSig = sig(node);
    if (sSig !== staticSig) {
      staticSig = sSig;
      clear(head).append(
        h(
          "div",
          { class: "np-title" },
          h("span", { class: "kind-glyph", "aria-hidden": "true" }, KIND_ICON[node.kind] || "•"),
          h("h2", null, node.id),
          statusPill(node.status, "pill-sm"),
        ),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close node details", title: "Close", onClick: onClose }, "×"),
        node.title ? h("div", { class: "np-subtitle muted" }, node.title) : null,
      );
      clear(hosts.spec).append(specSection(node));
      clear(hosts.state).append(stateSection(node));
      // attempt picker for transcripts
      const cur = attemptSel.value;
      clear(attemptSel).append(h("option", { value: "" }, "latest attempt"));
      for (let a = 1; a <= Math.max(1, node.attempts); a += 1) attemptSel.append(h("option", { value: String(a) }, `attempt ${a}`));
      attemptSel.value = cur && Number(cur) <= node.attempts ? cur : "";
    }
    const fSig = sig([node.status, node.attempts, node.finished_at, node.merge_sha]);
    if (fSig !== fetchSig) {
      fetchSig = fSig;
      loadDynamic(node).catch((err) => reportError(err, "Loading node details"));
    }
  }

  function hide() {
    el.hidden = true;
    currentId = null;
    fetchToken += 1;
  }

  return { el, show, hide, destroy: () => (fetchToken += 1) };
}
