// Shared helpers: safe DOM construction, formatting, markdown, diffs, JSON trees.
//
// Safety model: every piece of dynamic data reaches the DOM through text nodes or
// attribute setters (never innerHTML), so agent output can't inject markup.

const SVG_NS = "http://www.w3.org/2000/svg";

function append(el, child) {
  if (child === null || child === undefined || child === false || child === true) return;
  if (Array.isArray(child)) {
    child.forEach((c) => append(el, c));
    return;
  }
  if (child instanceof Node) {
    el.appendChild(child);
    return;
  }
  el.appendChild(document.createTextNode(String(child)));
}

function applyProps(el, props, isSvg) {
  if (!props) return;
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class" || key === "className") {
      if (isSvg) el.setAttribute("class", value);
      else el.className = value;
    } else if (key === "text") {
      el.textContent = String(value);
    } else if (key === "dataset") {
      Object.entries(value).forEach(([k, v]) => (el.dataset[k] = String(v)));
    } else if (key === "style" && typeof value === "object") {
      Object.entries(value).forEach(([k, v]) => {
        if (k.startsWith("--")) el.style.setProperty(k, String(v));
        else el.style[k] = v;
      });
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (!isSvg && (key === "value" || key === "checked" || key === "disabled" || key === "selected")) {
      el[key] = value;
    } else {
      if (/^on/i.test(key)) continue; // never allow string event handlers
      if ((key === "href" || key === "src") && !isSafeUrl(String(value))) continue;
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
}

/** Create an HTML element. Children may be strings, nodes, arrays or falsy (skipped). */
export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  applyProps(el, props, false);
  append(el, children);
  return el;
}

/** Create an SVG element. */
export function s(tag, props, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyProps(el, props, true);
  append(el, children);
  return el;
}

export function isSafeUrl(url) {
  const u = url.trim().toLowerCase();
  if (u.startsWith("#") || u.startsWith("/") || u.startsWith("blob:")) return true;
  return u.startsWith("http://") || u.startsWith("https://") || u.startsWith("mailto:");
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

/** Glyph per node kind (shape in the DAG also differs; the kind is always labelled in text). */
export const KIND_ICON = {
  agent: "✦",
  check: "✓",
  human: "☺",
  planner: "⎇",
  join: "⋈",
  action: "▸",
};

// --------------------------------------------------------------- formatting
export const DASH = "—";

export function isNil(v) {
  return v === null || v === undefined || (typeof v === "number" && Number.isNaN(v));
}

export function fmtNum(v, digits = 2) {
  if (isNil(v)) return DASH;
  if (typeof v !== "number") return String(v);
  if (Number.isInteger(v)) return v.toLocaleString();
  return v.toLocaleString(undefined, { maximumFractionDigits: digits });
}

export function fmtPct(v, digits = 1) {
  if (isNil(v)) return DASH;
  return `${(v * 100).toFixed(digits)}%`;
}

export function fmtUsd(v) {
  if (isNil(v)) return DASH;
  if (v === 0) return "$0";
  return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

export function fmtDuration(seconds) {
  if (isNil(seconds)) return DASH;
  const sec = Number(seconds);
  if (sec < 1) return `${Math.round(sec * 1000)} ms`;
  if (sec < 60) return `${sec.toFixed(sec < 10 ? 2 : 1)} s`;
  const m = Math.floor(sec / 60);
  const r = Math.round(sec % 60);
  if (m < 60) return `${m}m ${r}s`;
  const hr = Math.floor(m / 60);
  return `${hr}h ${m % 60}m`;
}

export function fmtRelative(ts) {
  if (isNil(ts)) return DASH;
  const diff = Date.now() / 1000 - ts;
  if (diff < 0) return "just now";
  if (diff < 10) return "just now";
  if (diff < 60) return `${Math.floor(diff)}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function fmtDate(ts) {
  if (isNil(ts)) return DASH;
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function fmtClock(ts) {
  if (isNil(ts)) return DASH;
  const d = new Date(ts * 1000);
  const base = d.toLocaleTimeString(undefined, { hour12: false });
  return `${base}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

export function shortHash(hash, n = 10) {
  if (!hash) return DASH;
  return String(hash).slice(0, n);
}

export function truncate(text, n) {
  const t = String(text ?? "");
  return t.length > n ? `${t.slice(0, n - 1)}…` : t;
}

export function humanize(key) {
  return String(key).replace(/_/g, " ");
}

/** Text-labelled status pill (never color-only). */
export function statusPill(status, extraClass = "") {
  const st = status || "pending";
  return h("span", { class: `pill st-${st} ${extraClass}`.trim() }, h("span", { class: "pill-dot", "aria-hidden": "true" }), humanize(st));
}

export function kv(rows) {
  const dl = h("dl", { class: "kv" });
  rows.forEach(([k, v]) => {
    if (v === undefined) return;
    dl.append(h("dt", null, k), h("dd", null, isNil(v) || v === "" ? DASH : v));
  });
  return dl;
}

export function emptyState(title, detail) {
  return h("div", { class: "empty" }, h("div", { class: "empty-title" }, title), detail ? h("div", { class: "empty-detail" }, detail) : null);
}

export function prettyJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Cheap structural signature used to skip re-rendering unchanged data. */
export function sig(value) {
  try {
    return JSON.stringify(value);
  } catch {
    return String(Math.random());
  }
}

// --------------------------------------------------------------- unified diff
export function renderDiff(text, { maxLines = 4000 } = {}) {
  const wrap = h("div", { class: "diff" });
  if (!text || !text.trim()) {
    wrap.append(emptyState("No diff", "This node did not merge any workspace changes."));
    return wrap;
  }
  const lines = text.split("\n");
  const pre = h("pre", { class: "diff-pre" });
  let files = 0;
  lines.slice(0, maxLines).forEach((line) => {
    let cls = "d-ctx";
    if (line.startsWith("diff --git")) {
      cls = "d-file";
      files += 1;
    } else if (line.startsWith("+++") || line.startsWith("---")) cls = "d-meta";
    else if (line.startsWith("@@")) cls = "d-hunk";
    else if (line.startsWith("+")) cls = "d-add";
    else if (line.startsWith("-")) cls = "d-del";
    else if (/^(index |new file|deleted file|similarity|rename |old mode|new mode)/.test(line)) cls = "d-meta";
    pre.append(h("span", { class: `d-line ${cls}` }, line || " "));
  });
  const adds = lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length;
  const dels = lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
  wrap.append(
    h(
      "div",
      { class: "diff-stats" },
      h("span", null, `${files} file${files === 1 ? "" : "s"}`),
      h("span", { class: "d-add-t" }, `+${adds}`),
      h("span", { class: "d-del-t" }, `−${dels}`),
      lines.length > maxLines ? h("span", { class: "muted" }, `(showing first ${maxLines} lines)`) : null,
    ),
    pre,
  );
  return wrap;
}

// --------------------------------------------------------------- JSON tree
function jsonScalar(value) {
  if (value === null) return h("span", { class: "j-null" }, "null");
  switch (typeof value) {
    case "string":
      return h("span", { class: "j-str" }, JSON.stringify(value));
    case "number":
      return h("span", { class: "j-num" }, String(value));
    case "boolean":
      return h("span", { class: "j-bool" }, String(value));
    default:
      return h("span", null, String(value));
  }
}

/** Collapsible JSON tree built from <details>; depth < openDepth starts expanded. */
export function jsonTree(value, { openDepth = 2, label = null } = {}) {
  const build = (val, key, depth) => {
    const keyEl = key !== null ? h("span", { class: "j-key" }, `${key}: `) : null;
    if (val !== null && typeof val === "object") {
      const isArr = Array.isArray(val);
      const entries = isArr ? val.map((v, i) => [i, v]) : Object.entries(val);
      if (!entries.length) {
        return h("div", { class: "j-row" }, keyEl, h("span", { class: "j-punc" }, isArr ? "[]" : "{}"));
      }
      const det = h("details", { class: "j-node" });
      if (depth < openDepth) det.open = true;
      det.append(
        h(
          "summary",
          null,
          keyEl,
          h("span", { class: "j-punc" }, isArr ? `[${entries.length}]` : `{${entries.length}}`),
        ),
      );
      const body = h("div", { class: "j-children" });
      // Render children lazily for big collapsed subtrees.
      const fill = () => {
        if (body.childElementCount) return;
        entries.forEach(([k, v]) => body.append(build(v, k, depth + 1)));
      };
      if (det.open) fill();
      else det.addEventListener("toggle", fill, { once: true });
      det.append(body);
      return det;
    }
    if (typeof val === "string" && val.includes("\n")) {
      return h("div", { class: "j-row" }, keyEl, h("pre", { class: "j-multiline" }, val));
    }
    return h("div", { class: "j-row" }, keyEl, jsonScalar(val));
  };
  return h("div", { class: "json-tree" }, build(value, label, 0));
}

// --------------------------------------------------------------- markdown
const INLINE_RULES = [
  { re: /`([^`\n]+)`/, make: (m) => h("code", null, m[1]) },
  { re: /\*\*([^*\n]+?)\*\*/, make: (m) => h("strong", null, inline(m[1])) },
  { re: /__([^_\n]+?)__/, make: (m) => h("strong", null, inline(m[1])) },
  { re: /\*([^*\n]+?)\*/, make: (m) => h("em", null, inline(m[1])) },
  { re: /(^|[^\w])_([^_\n]+?)_(?=[^\w]|$)/, make: (m) => [m[1], h("em", null, inline(m[2]))] },
  {
    re: /\[([^\]\n]+)\]\(([^)\s]+)\)/,
    make: (m) =>
      isSafeUrl(m[2])
        ? h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, inline(m[1]))
        : `${m[1]} (${m[2]})`,
  },
];

/** Inline markdown -> array of nodes/strings (safe: text only, no raw HTML). */
export function inline(text) {
  const out = [];
  let rest = String(text ?? "");
  while (rest) {
    let best = null;
    for (const rule of INLINE_RULES) {
      const m = rule.re.exec(rest);
      if (m && (best === null || m.index < best.m.index)) best = { m, rule };
    }
    if (!best) {
      out.push(rest);
      break;
    }
    if (best.m.index > 0) out.push(rest.slice(0, best.m.index));
    out.push(best.rule.make(best.m));
    rest = rest.slice(best.m.index + best.m[0].length);
  }
  return out;
}

function splitRow(line) {
  let t = line.trim();
  if (t.startsWith("|")) t = t.slice(1);
  if (t.endsWith("|")) t = t.slice(0, -1);
  return t.split("|").map((c) => c.trim());
}

/** Small block-level markdown renderer -> DocumentFragment. */
export function renderMarkdown(src) {
  const frag = document.createDocumentFragment();
  const lines = String(src ?? "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;
  let para = [];

  const flushPara = () => {
    if (!para.length) return;
    const p = h("p");
    para.forEach((ln, idx) => {
      const hardBreak = /\s{2,}$/.test(ln);
      append(p, inline(ln.trim()));
      if (idx < para.length - 1) append(p, hardBreak ? h("br") : " ");
    });
    frag.append(p);
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*(```|~~~)\s*([\w+-]*)\s*$/.exec(line);
    if (fence) {
      flushPara();
      const lang = fence[2] || "";
      const body = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      const pre = h("pre", { class: `md-code${lang ? ` lang-${lang.replace(/[^\w-]/g, "")}` : ""}` });
      if (lang) pre.append(h("span", { class: "md-lang" }, lang));
      pre.append(h("code", null, body.join("\n")));
      frag.append(pre);
      continue;
    }
    if (!line.trim()) {
      flushPara();
      i += 1;
      continue;
    }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      flushPara();
      frag.append(h(`h${heading[1].length}`, { class: "md-h" }, inline(heading[2].replace(/\s+#+\s*$/, ""))));
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flushPara();
      frag.append(h("hr"));
      i += 1;
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flushPara();
      const head = splitRow(line);
      const aligns = splitRow(lines[i + 1]).map((c) =>
        c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : null,
      );
      i += 2;
      const tbody = h("tbody");
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const cells = splitRow(lines[i]);
        tbody.append(h("tr", null, head.map((_, ci) => h("td", { style: aligns[ci] ? { textAlign: aligns[ci] } : null }, inline(cells[ci] ?? "")))));
        i += 1;
      }
      frag.append(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "md-table" },
            h("thead", null, h("tr", null, head.map((c, ci) => h("th", { style: aligns[ci] ? { textAlign: aligns[ci] } : null }, inline(c))))),
            tbody,
          ),
        ),
      );
      continue;
    }
    if (/^\s*>/.test(line)) {
      flushPara();
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i += 1;
      }
      const bq = h("blockquote");
      bq.append(renderMarkdown(quote.join("\n")));
      frag.append(bq);
      continue;
    }
    const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
    if (listRe.test(line)) {
      flushPara();
      frag.append(parseList(lines, i, listRe, (next) => (i = next)));
      continue;
    }
    para.push(line);
    i += 1;
  }
  flushPara();
  return frag;
}

function parseList(lines, start, listRe, setIndex) {
  const first = listRe.exec(lines[start]);
  const indent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const list = h(ordered ? "ol" : "ul", { class: "md-list" });
  let i = start;
  let current = null;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // a blank line ends the list unless the next line continues it
      const nxt = lines[i + 1];
      if (nxt && listRe.test(nxt) && listRe.exec(nxt)[1].length >= indent) {
        i += 1;
        continue;
      }
      break;
    }
    const m = listRe.exec(line);
    if (m && m[1].length === indent && /\d/.test(m[2]) !== ordered) break; // list type changed
    if (m && m[1].length === indent) {
      current = h("li");
      const task = /^\[( |x|X)\]\s+(.*)$/.exec(m[3]);
      if (task) {
        current.classList.add("md-task");
        append(current, [h("span", { class: "md-check", "aria-hidden": "true" }, task[1].trim() ? "☑" : "☐"), " ", inline(task[2])]);
      } else {
        append(current, inline(m[3]));
      }
      list.append(current);
      i += 1;
    } else if (m && m[1].length > indent && current) {
      current.append(parseList(lines, i, listRe, (next) => (i = next)));
    } else if (!m && current && /^\s+/.test(line)) {
      append(current, [" ", ...inline(line.trim())]);
      i += 1;
    } else {
      break;
    }
  }
  setIndex(i);
  return list;
}

// --------------------------------------------------------------- misc
export function downloadText(filename, text, mime = "text/markdown") {
  const blob = new Blob([text], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function storageGet(key, fallback = "") {
  try {
    const v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    if (value === null || value === undefined || value === "") window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable (private mode) — settings just won't persist */
  }
}

/** Parse python-repr-ish or JSON strings in transcripts into a value when possible. */
export function tryParseJson(text) {
  if (typeof text !== "string") return text;
  const t = text.trim();
  if (!t || !/^[[{"]/.test(t)) return text;
  try {
    return JSON.parse(t);
  } catch {
    return text;
  }
}
