// Downloads drawer: the global queue with progress and rate, cancel, remove, clear finished
// (AC-DL-05, AC-DL-07). A summary card sits in the sidebar.

import { api } from "../api.js";
import { h, replace } from "../dom.js";
import { icon } from "../icons.js";
import { bytes, t } from "../i18n.js";
import { button, emptyState, pill, progress, toastError } from "../ui.js";

export function downloadsDrawer() {
  let last = "";
  const summary = h("small", { class: "muted num" });
  const bar = h("div");
  const card = h("button", { class: "card nav", type: "button", onclick: () => open(true), "aria-haspopup": "dialog" },
    icon("down"), h("span", { class: "grow stack" }, h("b", {}, t("downloads.title")), summary, bar));
  const list = h("div", { class: "drawer-b", "aria-live": "polite" });
  const panel = h("aside", { class: "drawer", role: "dialog", "aria-label": t("downloads.title"), dataset: { open: "false" } },
    h("div", { class: "drawer-h" }, h("h2", { class: "heading" }, t("downloads.title")),
      h("div", { class: "inline" },
        button(t("downloads.clear"), () => act(() => api.post("/downloads/clear-finished")), { small: true }),
        h("button", { class: "btn icon", type: "button", "aria-label": t("common.close"), onclick: () => open(false) }, icon("x")))),
    list);
  panel.addEventListener("keydown", (e) => e.key === "Escape" && open(false));

  function open(yes) {
    panel.dataset.open = String(yes);
    if (yes) panel.querySelector("button")?.focus();
    refresh();
  }

  async function act(fn) {
    try {
      await fn();
      last = "";
      await refresh();
    } catch (error) {
      toastError(error);
    }
  }

  const pad = (n) => String(n).padStart(2, "0");

  // One card per release ("Lupin · Season 3"), one compact line per file: the queue stays
  // readable with dozens of episodes.
  function group(files) {
    const first = files[0];
    const done = files.filter((d) => d.status === "done").length;
    return h("section", { class: "card dl-group" },
      h("div", { class: "dl-head" },
        h("div", { class: "grow" }, h("b", {}, first.title),
          h("div", { class: "muted small" }, first.season == null ? t("releases.scope.movie") : t("explorer.season", { season: first.season }))),
        h("span", { class: "muted num small" }, t("downloads.group_done", { done, total: files.length }))),
      files.map(line));
  }

  function line(d) {
    const running = d.status === "running";
    const label = d.season != null && d.episode != null ? `S${pad(d.season)}E${pad(d.episode)}` : d.filename;
    return h("div", { class: "dl-line" },
      h("div", { class: "inline" },
        h("span", { class: "mono grow", title: d.filename }, label),
        h("span", { class: "muted num small" },
          d.status === "done" ? bytes(d.total || d.done)
            : d.total ? `${bytes(d.done)} / ${bytes(d.total)}`
            : d.done ? bytes(d.done) : "", // queued: size not known yet
          running && d.rate > 0 && ` · ${t("downloads.rate", { rate: bytes(d.rate) })}`),
        pill(d.status),
        ["queued", "running"].includes(d.status) && iconButton(t("downloads.cancel"), "x", () => api.post(`/downloads/${d.id}/cancel`)),
        ["cancelled", "error"].includes(d.status) && iconButton(t("downloads.remove"), "trash", () => api.del(`/downloads/${d.id}`))),
      running && progress(d.done, d.total),
      d.error && h("small", { class: "error-text" }, t(d.error, d.errorParams || {})));
  }

  function iconButton(label, name, fn) {
    return h("button", { class: "btn ghost icon small", type: "button", "aria-label": label, title: label, onclick: () => act(fn) }, icon(name));
  }

  function groups(data) {
    const byRelease = new Map();
    for (const d of data) byRelease.set(d.releaseId, [...(byRelease.get(d.releaseId) || []), d]);
    return [...byRelease.values()].map(group);
  }

  async function refresh() {
    try {
      const { data } = await api.get("/downloads");
      const json = JSON.stringify(data);
      if (json === last) return;
      last = json;
      const active = data.filter((d) => d.status === "running");
      const queued = data.filter((d) => d.status === "queued");
      summary.textContent = t("downloads.summary", { active: active.length, queued: queued.length });
      replace(bar, active[0] ? progress(active[0].done, active[0].total) : null);
      replace(list, data.length ? groups(data) : emptyState(t("downloads.empty")));
    } catch {
      // the sidebar card stays as it was; errors surface in the views
    }
  }

  refresh();
  return { card, panel, refresh };
}
