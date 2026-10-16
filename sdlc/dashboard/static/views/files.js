// Files tab: browse the run's git workspace (tracked files on main) with line numbers.

import { api } from "../api.js";
import { h, clear, sig, emptyState } from "../util.js";
import { reportError } from "../ui.js";

function buildTree(paths) {
  const root = { dirs: new Map(), files: [] };
  paths.forEach((p) => {
    const parts = p.split("/");
    let node = root;
    parts.slice(0, -1).forEach((d) => {
      if (!node.dirs.has(d)) node.dirs.set(d, { dirs: new Map(), files: [] });
      node = node.dirs.get(d);
    });
    node.files.push({ name: parts[parts.length - 1], path: p });
  });
  return root;
}

function fileIcon(name) {
  if (/\.py$/.test(name)) return "py";
  if (/\.md$/.test(name)) return "md";
  if (/\.(json|ya?ml|toml|ini|cfg)$/.test(name)) return "cfg";
  if (/\.(txt|lock)$/.test(name)) return "txt";
  return "·";
}

export function createFilesView(ctx) {
  const ui = (ctx.ui.files ||= { selected: null, openDirs: {} });
  const el = h("div", { class: "files-view split" });
  const treeHost = h("nav", { class: "file-tree", "aria-label": "Workspace files" }, h("div", { class: "loading" }, "Loading…"));
  const viewer = h("div", { class: "file-viewer" });
  el.append(treeHost, viewer);
  let filesSig = null;
  let mergeSig = null;
  let inflight = false;
  let token = 0;

  function renderDir(node, prefix) {
    const ul = h("ul", { class: "tree" });
    [...node.dirs.keys()].sort().forEach((d) => {
      const path = prefix ? `${prefix}/${d}` : d;
      const det = h("details", { class: "tree-dir" });
      det.open = ui.openDirs[path] ?? true;
      det.addEventListener("toggle", () => (ui.openDirs[path] = det.open));
      det.append(h("summary", null, h("span", { class: "dir-icon", "aria-hidden": "true" }, "▸"), `${d}/`), renderDir(node.dirs.get(d), path));
      ul.append(h("li", null, det));
    });
    node.files
      .sort((a, b) => a.name.localeCompare(b.name))
      .forEach((f) => {
        const active = ui.selected === f.path;
        const b = h(
          "button",
          { type: "button", class: `tree-file${active ? " active" : ""}`, title: f.path, "aria-current": active ? "true" : null },
          h("span", { class: "file-ext", "aria-hidden": "true" }, fileIcon(f.name)),
          f.name,
        );
        b.addEventListener("click", () => {
          ui.selected = f.path;
          treeHost.querySelectorAll(".tree-file.active").forEach((x) => {
            x.classList.remove("active");
            x.removeAttribute("aria-current");
          });
          b.classList.add("active");
          b.setAttribute("aria-current", "true");
          loadFile();
        });
        ul.append(h("li", null, b));
      });
    return ul;
  }

  async function loadFile() {
    const path = ui.selected;
    const my = ++token;
    if (!path) {
      clear(viewer).append(emptyState("Select a file", "Browse the code the agents produced on the main branch."));
      return;
    }
    clear(viewer).append(h("div", { class: "loading" }, `Loading ${path}…`));
    try {
      const res = await api.file(ctx.runId, path);
      if (my !== token) return;
      const lines = res.content.split("\n");
      if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
      const gutter = h("pre", { class: "code-gutter", "aria-hidden": "true" }, lines.map((_, i) => `${i + 1}`).join("\n"));
      const code = h("pre", { class: "code-body" }, h("code", null, res.content));
      clear(viewer).append(
        h("div", { class: "file-head" }, h("span", { class: "mono" }, path), h("span", { class: "muted small" }, `${lines.length} lines · ${res.content.length.toLocaleString()} chars`)),
        h("div", { class: "code", tabindex: "0", "aria-label": `Contents of ${path}` }, gutter, code),
      );
    } catch (err) {
      if (my === token) clear(viewer).append(emptyState("Could not open file", err.message));
    }
  }

  async function loadTree(force) {
    if (inflight) return;
    inflight = true;
    try {
      const res = await api.files(ctx.runId);
      const key = sig(res.files);
      if (key !== filesSig || force) {
        filesSig = key;
        clear(treeHost);
        if (!res.files.length) treeHost.append(emptyState("Workspace is empty"));
        else treeHost.append(h("div", { class: "muted small tree-count" }, `${res.files.length} tracked files`), renderDir(buildTree(res.files), ""));
        if (ui.selected && !res.files.includes(ui.selected)) ui.selected = null;
      }
      if (force || !viewer.childElementCount) loadFile();
    } catch (err) {
      clear(treeHost).append(emptyState("Workspace unavailable", err.message));
      if (err.status !== 404) reportError(err, "Loading files");
    } finally {
      inflight = false;
    }
  }

  return {
    el,
    update(data) {
      // Workspace changes only when a node merges — key off merge shas.
      const key = sig(data.nodes.map((n) => n.merge_sha));
      if (key !== mergeSig) {
        const first = mergeSig === null;
        mergeSig = key;
        loadTree(!first);
      }
    },
  };
}
