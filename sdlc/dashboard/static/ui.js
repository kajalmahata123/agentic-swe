// Cross-cutting UI primitives: toasts, modal dialogs, settings, theme toggle.

import { h, clear, storageGet, storageSet } from "./util.js";
import { TOKEN_KEY, OPERATOR_KEY, ApiError } from "./api.js";

// ------------------------------------------------------------------ toasts
let toastHost = null;

export function toast(message, kind = "info", { timeout = 5000 } = {}) {
  if (!toastHost) {
    toastHost = h("div", { class: "toasts", role: "region", "aria-live": "polite", "aria-label": "Notifications" });
    document.body.append(toastHost);
  }
  const labels = { info: "Info", ok: "Done", warn: "Warning", error: "Error" };
  const close = h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss notification" }, "×");
  const el = h(
    "div",
    { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" },
    h("strong", { class: "toast-kind" }, labels[kind] || "Info"),
    h("span", { class: "toast-msg" }, message),
    close,
  );
  const remove = () => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 200);
  };
  close.addEventListener("click", remove);
  toastHost.append(el);
  if (timeout) setTimeout(remove, kind === "error" ? timeout * 2 : timeout);
  return remove;
}

/** Report an exception as a friendly toast. 401s nudge the user to Settings. */
export function reportError(err, context = "") {
  const prefix = context ? `${context}: ` : "";
  if (err instanceof ApiError) {
    toast(prefix + err.message, "error");
    if (err.status === 401) openSettings({ focusToken: true });
  } else {
    console.error(err);
    toast(`${prefix}Something went wrong in the dashboard (${err && err.message ? err.message : "unknown error"})`, "error");
  }
}

// ------------------------------------------------------------------ modals
let openModalCount = 0;

/**
 * Show a modal dialog. `body` is a Node; `actions` are {label, kind, onClick(close) -> bool|Promise}.
 * Returns a close() function. Escape and backdrop click cancel.
 */
export function modal({ title, body, actions = [], wide = false, onClose = null }) {
  const previouslyFocused = document.activeElement;
  const titleId = `modal-title-${Date.now()}`;
  const dialog = h("div", {
    class: `modal${wide ? " modal-wide" : ""}`,
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": titleId,
  });
  const backdrop = h("div", { class: "modal-backdrop" }, dialog);
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    openModalCount -= 1;
    backdrop.remove();
    document.removeEventListener("keydown", onKey, true);
    if (onClose) onClose();
    if (previouslyFocused && previouslyFocused.focus) previouslyFocused.focus();
  };
  const onKey = (e) => {
    if (e.key === "Escape") {
      e.stopPropagation();
      close();
    } else if (e.key === "Tab") {
      // keep focus inside the dialog
      const focusables = dialog.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])");
      if (!focusables.length) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        last.focus();
        e.preventDefault();
      } else if (!e.shiftKey && document.activeElement === last) {
        first.focus();
        e.preventDefault();
      }
    }
  };
  const footer = h("div", { class: "modal-actions" });
  actions.forEach((a) => {
    const btn = h("button", { class: `btn ${a.kind ? `btn-${a.kind}` : ""}`, type: "button" }, a.label);
    btn.addEventListener("click", async () => {
      if (btn.disabled) return;
      btn.disabled = true;
      try {
        const keepOpen = a.onClick ? await a.onClick(close) : false;
        if (!keepOpen) close();
      } finally {
        btn.disabled = false;
      }
    });
    footer.append(btn);
  });
  dialog.append(
    h(
      "div",
      { class: "modal-head" },
      h("h2", { id: titleId }, title),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close dialog", onClick: close }, "×"),
    ),
    h("div", { class: "modal-body" }, body),
    footer,
  );
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) close();
  });
  document.addEventListener("keydown", onKey, true);
  document.body.append(backdrop);
  openModalCount += 1;
  const autofocus = dialog.querySelector("[autofocus], textarea, input, select") || dialog.querySelector(".modal-actions .btn");
  if (autofocus) setTimeout(() => autofocus.focus(), 0);
  return close;
}

export function isModalOpen() {
  return openModalCount > 0;
}

/** Inline validation message area for forms inside modals/cards. */
export function fieldError() {
  const el = h("div", { class: "field-error", role: "alert", hidden: true });
  return {
    el,
    show(msg) {
      el.textContent = msg;
      el.hidden = false;
    },
    hide() {
      el.textContent = "";
      el.hidden = true;
    },
  };
}

// ------------------------------------------------------------------ theme
const THEME_KEY = "sdlc_theme";

export function applyTheme(theme = storageGet(THEME_KEY, "auto")) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const label = theme === "auto" ? "Theme: system" : `Theme: ${theme}`;
    btn.setAttribute("aria-label", `${label} (click to change)`);
    btn.title = label;
    btn.textContent = theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐";
  }
}

export function cycleTheme() {
  const order = ["auto", "light", "dark"];
  const cur = storageGet(THEME_KEY, "auto");
  const next = order[(order.indexOf(cur) + 1) % order.length];
  storageSet(THEME_KEY, next === "auto" ? "" : next);
  applyTheme(next);
  toast(next === "auto" ? "Theme follows your system setting" : `Theme set to ${next}`, "info", { timeout: 2000 });
}

// ------------------------------------------------------------------ settings
export function openSettings({ focusToken = false } = {}) {
  if (document.querySelector(".modal-settings")) return;
  const operator = h("input", {
    id: "set-operator",
    type: "text",
    value: storageGet(OPERATOR_KEY, ""),
    placeholder: "e.g. alice",
    autocomplete: "username",
    maxlength: "64",
  });
  const token = h("input", {
    id: "set-token",
    type: "password",
    value: storageGet(TOKEN_KEY, ""),
    placeholder: "only needed if SDLC_DASHBOARD_TOKEN is set",
    autocomplete: "off",
  });
  const show = h("input", { id: "set-show", type: "checkbox" });
  show.addEventListener("change", () => (token.type = show.checked ? "text" : "password"));
  const body = h(
    "div",
    { class: "form modal-settings" },
    h(
      "div",
      { class: "field" },
      h("label", { for: "set-operator" }, "Operator name"),
      operator,
      h("div", { class: "hint" }, "Recorded in the audit trail as human:<name> on every decision you make."),
    ),
    h(
      "div",
      { class: "field" },
      h("label", { for: "set-token" }, "Dashboard token (X-SDLC-Token)"),
      token,
      h("label", { class: "check" }, show, " Show token"),
      h("div", { class: "hint" }, "Stored in this browser's localStorage and sent only on approve / reject / stop / resume / amend / start requests."),
    ),
  );
  modal({
    title: "Settings",
    body,
    actions: [
      { label: "Cancel" },
      {
        label: "Save",
        kind: "primary",
        onClick: () => {
          storageSet(OPERATOR_KEY, operator.value.trim());
          storageSet(TOKEN_KEY, token.value.trim());
          document.dispatchEvent(new CustomEvent("sdlc:settings"));
          toast("Settings saved", "ok", { timeout: 2000 });
          return false;
        },
      },
    ],
  });
  setTimeout(() => (focusToken ? token : operator).focus(), 0);
}

// ------------------------------------------------------------------ misc widgets
/** A button that disables itself while its async handler runs. */
export function asyncButton(label, onClick, { kind = "", title = "", type = "button", ariaLabel = null } = {}) {
  const btn = h("button", { class: `btn ${kind ? `btn-${kind}` : ""}`.trim(), type, title: title || null, "aria-label": ariaLabel });
  btn.append(label);
  btn.addEventListener("click", async (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("busy");
    try {
      await onClick(e);
    } finally {
      btn.disabled = false;
      btn.classList.remove("busy");
    }
  });
  return btn;
}

/** Collapsible section (native <details>) whose open state survives re-renders via a key map. */
export function section(title, content, { open = true, key = null, store = null, extra = null } = {}) {
  const det = h("details", { class: "section" });
  det.open = store && key && key in store ? store[key] : open;
  if (store && key) det.addEventListener("toggle", () => (store[key] = det.open));
  det.append(h("summary", null, h("span", { class: "section-title" }, title), extra), h("div", { class: "section-body" }, content));
  return det;
}

export function replaceContent(el, ...nodes) {
  clear(el);
  el.append(...nodes.filter(Boolean));
}
