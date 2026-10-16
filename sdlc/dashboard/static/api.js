// Thin fetch wrapper around the dashboard API. Turns backend errors into friendly
// ApiError messages (FastAPI `detail`, never stack traces) and attaches the operator
// name + optional X-SDLC-Token on mutating requests.

import { storageGet } from "./util.js";

export const TOKEN_KEY = "sdlc_token";
export const OPERATOR_KEY = "sdlc_operator";

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function operatorName() {
  return storageGet(OPERATOR_KEY, "").trim() || "dashboard";
}

function detailMessage(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d) => {
        if (typeof d === "string") return d;
        const loc = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        return loc ? `${loc}: ${d.msg}` : d.msg || JSON.stringify(d);
      })
      .join("; ");
  }
  if (typeof detail === "object" && detail.msg) return detail.msg;
  return JSON.stringify(detail);
}

function statusText(status) {
  switch (status) {
    case 401:
      return "Not authorised — set a valid dashboard token in Settings";
    case 403:
      return "Forbidden";
    case 404:
      return "Not found";
    case 409:
      return "Conflict";
    case 422:
      return "Invalid request";
    default:
      return status >= 500 ? "The server hit an internal error" : `Request failed (${status})`;
  }
}

async function request(path, { method = "GET", body, text = false } = {}) {
  const headers = { Accept: text ? "text/plain" : "application/json" };
  const init = { method, headers, cache: "no-store" };
  if (method !== "GET") {
    const token = storageGet(TOKEN_KEY, "").trim();
    if (token) headers["X-SDLC-Token"] = token;
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("Cannot reach the dashboard server — is it still running?", 0, null);
  }
  if (!res.ok) {
    let detail = null;
    try {
      const data = await res.json();
      detail = data && data.detail !== undefined ? data.detail : null;
    } catch {
      /* non-JSON error body (e.g. plain 500) — don't surface raw text */
    }
    const msg = detailMessage(detail);
    const base = statusText(res.status);
    const message = res.status === 401 ? `${base}${msg ? ` (${msg})` : ""}` : msg ? `${base}: ${msg}` : base;
    throw new ApiError(message, res.status, detail);
  }
  if (text) return res.text();
  if (res.status === 204) return null;
  return res.json();
}

const enc = encodeURIComponent;

/** Node ids contain ':' (impl:WP-1) — keep it readable but encode everything else. */
const encNode = (id) => enc(id).replace(/%3A/gi, ":");

function withOperator(body = {}) {
  return { ...body, operator: operatorName() };
}

export const api = {
  runs: () => request("/api/runs"),
  run: (id) => request(`/api/runs/${enc(id)}`),
  events: (id, { after = 0, types = null, limit = 1000 } = {}) => {
    const q = new URLSearchParams({ after: String(after), limit: String(limit) });
    if (types) q.set("types", Array.isArray(types) ? types.join(",") : types);
    return request(`/api/runs/${enc(id)}/events?${q}`);
  },
  verifyAudit: (id) => request(`/api/runs/${enc(id)}/audit/verify`),
  artifacts: (id) => request(`/api/runs/${enc(id)}/artifacts`),
  artifact: (id, name, version) =>
    request(`/api/runs/${enc(id)}/artifacts/${enc(name)}${version ? `?version=${enc(version)}` : ""}`),
  decisions: (id) => request(`/api/runs/${enc(id)}/decisions`),
  report: (id) => request(`/api/runs/${enc(id)}/report`, { text: true }),
  files: (id) => request(`/api/runs/${enc(id)}/files`),
  file: (id, path) => request(`/api/runs/${enc(id)}/files?path=${enc(path)}`),
  nodeDiff: (id, node) => request(`/api/runs/${enc(id)}/nodes/${encNode(node)}/diff`, { text: true }),
  transcript: (id, node, attempt) =>
    request(`/api/runs/${enc(id)}/nodes/${encNode(node)}/transcript${attempt ? `?attempt=${enc(attempt)}` : ""}`),
  approvals: (status = "pending") => request(`/api/approvals?status=${enc(status)}`),
  approve: (approvalId, body) =>
    request(`/api/approvals/${enc(approvalId)}/approve`, { method: "POST", body: withOperator(body) }),
  reject: (approvalId, comment) =>
    request(`/api/approvals/${enc(approvalId)}/reject`, { method: "POST", body: withOperator({ comment }) }),
  scenarios: () => request("/api/scenarios"),
  startRun: (scenario, approveMode, maxParallel) =>
    request("/api/runs", {
      method: "POST",
      body: { scenario, approve_mode: approveMode, max_parallel: maxParallel },
    }),
  stop: (id, reason) =>
    request(`/api/runs/${enc(id)}/stop`, {
      method: "POST",
      body: reason && reason.trim().length >= 3 ? withOperator({ text: reason.trim() }) : undefined,
    }),
  resume: (id) => request(`/api/runs/${enc(id)}/resume`, { method: "POST" }),
  amend: (id, text) => request(`/api/runs/${enc(id)}/amend`, { method: "POST", body: withOperator({ text }) }),
  portfolio: () => request("/api/metrics"),
};
