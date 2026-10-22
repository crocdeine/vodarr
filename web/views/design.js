// Design system reference page (AC-UI-07): every component, switchable between both themes.

import { h } from "../dom.js";
import { icon } from "../icons.js";
import { t } from "../i18n.js";
import { alertBox, button, card, field, input, pill, progress, segmented, select, timeline, toast } from "../ui.js";

const COLORS = ["bg", "surface", "surface-2", "text", "text-2", "text-3", "accent", "ok", "running", "action", "error", "skipped"];

export function designView(main) {
  const swatches = h("div", { class: "inline swatches" }, COLORS.map((c) =>
    h("div", { class: "stack" }, h("span", { class: `swatch sw-${c}` }), h("small", { class: "mono" }, `--${c}`))));
  let seg = "all";
  const segRoot = h("div");
  const drawSeg = () => segRoot.replaceChildren(segmented([["all", t("releases.filter.all")], ["active", t("releases.filter.active")],
    ["action", t("releases.filter.action")], ["done", t("releases.filter.done")]], seg, (v) => { seg = v; drawSeg(); }, t("design.tabs")));
  drawSeg();
  const drawer = h("aside", { class: "drawer", "aria-label": t("design.drawer"), dataset: { open: "false" } },
    h("div", { class: "drawer-h" }, h("h2", { class: "heading" }, t("design.drawer")),
      button("", () => (drawer.dataset.open = "false"), { iconName: "x", kind: "icon" })),
    h("div", { class: "drawer-b" }, h("p", { class: "muted" }, t("design.sample_text"))));
  const stages = ["download", "analyse", "search", "verify", "restore", "seed"].map((name, i) => ({
    name, state: ["done", "done", "done", "done", "skipped", "running"][i], pct: 40, message: null }));

  main.append(h("section", { class: "pane" },
    h("header", { class: "glass-head" },
      h("div", { class: "crumb" }, t("app.name")),
      h("h1", { class: "title" }, t("design.title")),
      h("p", { class: "muted" }, t("design.intro")),
      h("div", { class: "inline" }, h("span", { class: "muted" }, t("design.theme")),
        button(t("settings.theme.dark"), () => (document.documentElement.dataset.theme = "dark"), { small: true }),
        button(t("settings.theme.light"), () => (document.documentElement.dataset.theme = "light"), { small: true }))),
    h("div", { class: "content" },
      card(t("design.colors"), h("div", { class: "card-b" }, swatches)),
      card(t("design.type"), h("div", { class: "card-b stack" },
        h("p", { class: "title" }, t("app.name")), h("p", { class: "heading" }, t("design.sample_text")),
        h("p", {}, t("design.sample_text")), h("p", { class: "muted" }, t("design.sample_text")),
        h("p", { class: "faint" }, t("design.sample_text")), h("p", { class: "mono" }, "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP.mkv"))),
      card(t("design.buttons"), h("div", { class: "card-b inline" },
        button(t("design.primary"), null, { kind: "primary", iconName: "down" }), button(t("design.secondary"), null),
        button(t("design.danger"), null, { kind: "danger", iconName: "trash" }), button(t("design.ghost"), null, { kind: "ghost" }),
        button(t("design.disabled"), null, { disabled: true }), button(t("common.retry"), null, { small: true, iconName: "refresh" }))),
      card(t("design.fields"), h("div", { class: "card-b grid2" },
        field(t("settings.url"), input({ value: "http://prowlarr:9696" }), t("settings.category.hint")),
        field(t("settings.link_type"), select([["hardlink", "hardlink"], ["symlink", "symlink"], ["reflink", "reflink"]], "hardlink")),
        h("label", { class: "search" }, icon("search"), input({ placeholder: t("common.search"), "aria-label": t("common.search") })),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: true }), t("settings.flat")))),
      card(t("design.status"), h("div", { class: "card-b inline" },
        ["downloading", "processing", "action_required", "done", "error", "skipped", "identical", "partial", "different"].map((s) => pill(s)))),
      card(t("design.progress"), h("div", { class: "card-b stack" }, progress(62, 100), progress(1, 4))),
      card(t("design.tabs"), h("div", { class: "card-b stack" }, segRoot,
        h("div", { class: "tabs", role: "tablist" }, ["overview", "download", "identification", "crossseed"].map((tab, i) =>
          h("button", { role: "tab", type: "button", "aria-selected": String(i === 0) }, t(`releases.tab.${tab}`)))))),
      card(t("design.timeline"), timeline(stages)),
      card(t("design.table"), h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, t("releases.file")), h("th", { class: "r" }, t("releases.size")), h("th", {}, t("releases.status")))),
        h("tbody", {}, [["Northwind Precinct - S01E01.mkv", "1,2 GB", "identical"], ["Northwind Precinct - S01E02.mkv", "1,1 GB", "different"]].map(([f, size, s]) =>
          h("tr", {}, h("td", {}, f, h("small", {}, "Northwind.Precinct.S01E01.1080p.WEB-DL-GRP.mkv")), h("td", { class: "r num" }, size), h("td", {}, pill(s))))))),
      card(t("design.alerts"), h("div", { class: "card-b stack" },
        alertBox("", t("releases.action.title"), t("releases.action.text", { ok: 18, total: 19, tracker: "Nimbus" }),
          button(t("releases.complete_from", { tracker: "Nimbus" }), null, { kind: "primary", small: true })),
        alertBox("error", t("status.error"), t("seed.torrent_missing", { tracker: "Aurora" })),
        alertBox("info", t("settings.prowlarr"), t("settings.snatch_warning")),
        h("div", { class: "inline" }, button(t("design.show_toast"), () => toast(t("design.sample_toast")), { small: true }),
          button(t("design.open_drawer"), () => (drawer.dataset.open = "true"), { small: true })))),
    ),
    drawer));
}
