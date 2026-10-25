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

  function item(d) {
    const running = d.status === "running";
    return h("div", { class: "card card-b stack" },
      h("div", { class: "inline" }, h("b", { class: "grow" }, d.title), pill(d.status)),
      h("small", { class: "mono" }, d.filename),
      running && progress(d.done, d.total),
      h("div", { class: "inline muted num" },
        d.total ? `${bytes(d.done)} / ${bytes(d.total)}` : bytes(d.done),
        running && d.rate > 0 && `· ${t("downloads.rate", { rate: bytes(d.rate) })}`,
        d.error && `· ${t(d.error, d.errorParams || {})}`,
        h("span", { class: "grow" }),
        ["queued", "running"].includes(d.status) && button(t("downloads.cancel"), () => act(() => api.post(`/downloads/${d.id}/cancel`)), { small: true }),
        ["queued", "cancelled", "error"].includes(d.status) && button(t("downloads.remove"), () => act(() => api.del(`/downloads/${d.id}`)), { small: true, kind: "ghost" })));
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
      replace(list, data.length ? data.map(item) : emptyState(t("downloads.empty")));
    } catch {
      // the sidebar card stays as it was; errors surface in the views
    }
  }

  refresh();
  return { card, panel, refresh };
}
