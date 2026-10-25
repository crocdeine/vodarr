// Releases: filterable list, detail in tabs (overview, download, identification, cross-seed),
// pipeline timeline, per-file report per candidate, "complete from", retry, delete
// (AC-UI-01..03, AC-SEED-05, AC-PIPE-05).

import { api } from "../api.js";
import { h, replace } from "../dom.js";
import { bytes, language, t, tm } from "../i18n.js";
import { alertBox, button, card, confirmDialog, emptyState, fetchMeta, metaPanel, pill, progress, segmented, timeline, toast, toastError } from "../ui.js";

const FILTERS = [
  ["all", null],
  ["active", ["downloading", "processing"]],
  ["action", ["action_required"]],
  ["done", ["done"]],
  ["error", ["error"]],
];
const TABS = ["overview", "download", "identification", "crossseed"];

// Technical metadata read by ffprobe after download (0.1.1).
const BIBLIOGRAPHIC = { fre: "fr", ger: "de", dut: "nl", chi: "zh", cze: "cs", gre: "el", per: "fa", rum: "ro", slo: "sk", alb: "sq", arm: "hy", baq: "eu", geo: "ka", ice: "is", mac: "mk", may: "ms", wel: "cy" };
const CHANNELS = { 1: "1.0", 2: "2.0", 6: "5.1", 8: "7.1" };

function languageName(code) {
  if (!code) return null;
  try {
    return new Intl.DisplayNames([language()], { type: "language" }).of(BIBLIOGRAPHIC[code] || code);
  } catch {
    return code;
  }
}

function videoLine(media) {
  const v = media?.video;
  if (!v) return null;
  const res = v.height >= 2000 ? "2160p" : v.height ? `${v.height}p` : null;
  return [res, v.codec?.toUpperCase(), v.profile, v.hdr && "HDR", v.fps && `${v.fps} fps`].filter(Boolean).join(" · ");
}

function audioLine(media) {
  return (media?.audio || []).map((a) =>
    [a.codec?.toUpperCase(), CHANNELS[a.channels] || (a.channels && `${a.channels} ch`), languageName(a.language) && `(${languageName(a.language)})`].filter(Boolean).join(" ")).join(", ");
}

function techFacts(media) {
  if (!media) return [];
  const subs = (media.subtitles || []).map(languageName).filter(Boolean);
  return [
    [t("releases.tech.video"), videoLine(media)],
    [t("releases.tech.audio"), audioLine(media)],
    [t("releases.tech.subtitles"), subs.join(", ")],
    [t("releases.tech.duration"), media.duration ? t("releases.tech.minutes", { minutes: Math.round(media.duration / 60) }) : null],
    [t("releases.tech.container"), media.container?.split(",")[0]],
  ].filter(([, value]) => value);
}

export function releasesView(main, releaseId, ctx) {
  const state = { filter: "all", tab: "overview", selected: releaseId, items: [], seen: {} };

  // Polling redraws only what changed: buttons stay put under the pointer.
  function changed(key, data) {
    const json = JSON.stringify(data);
    if (state.seen[key] === json) return false;
    state.seen[key] = json;
    return true;
  }
  const filterRoot = h("div");
  const rows = h("div", { class: "rows", "aria-live": "polite" });
  const listPane = h("section", { class: "pane list" },
    h("header", { class: "pane-head toolbar" }, h("h1", { class: "title" }, t("releases.title")), filterRoot), rows);
  const detailPane = h("section", { class: "pane" });
  main.append(h("div", { class: "split" }, listPane, detailPane));

  function drawFilters() {
    replace(filterRoot, segmented(FILTERS.map(([key]) => [key, t(`releases.filter.${key}`)]), state.filter,
      (key) => { state.filter = key; drawFilters(); drawList(); }, t("releases.title")));
  }

  function line(r) {
    if (r.kind === "movie") return t("releases.movie_line", { year: r.year || "—" });
    return t("releases.season_line", { season: r.season, files: r.files });
  }

  function drawList() {
    const allowed = FILTERS.find(([key]) => key === state.filter)[1];
    const items = state.items.filter((r) => !allowed || allowed.includes(r.status));
    replace(rows, items.length ? items.map((r) => h("a", {
      class: "row", href: `#/releases/${r.id}`, "aria-current": r.id === state.selected ? "true" : undefined,
    }, h("span", { class: "poster" }),
      h("div", { class: "grow" }, h("div", { class: "t" }, r.title), h("div", { class: "s" }, line(r)),
        r.status === "downloading" && r.total ? progress(r.done, r.total) : null),
      pill(r.status))) : emptyState(t("releases.empty")));
  }

  async function loadList() {
    try {
      const page = await api.get("/releases?pageSize=200");
      state.items = page.data;
      if (changed("list", page.data)) drawList();
    } catch (error) {
      replace(rows, emptyState(t(error.code, error.params)));
    }
  }

  async function act(fn, okText) {
    try {
      await fn();
      state.seen = {};
      if (okText) toast(okText);
      await refresh();
    } catch (error) {
      toastError(error);
    }
  }

  function retry(stage) {
    return act(() => api.post(`/releases/${state.selected}/retry`, { stage }));
  }

  async function remove(detail) {
    const box = h("input", { type: "checkbox", name: "deleteFiles" });
    const answer = await confirmDialog(t("releases.delete.title"), t("releases.delete.text"), {
      confirm: t("common.delete"), danger: true, extra: h("label", { class: "check" }, box, t("releases.delete.files")),
    });
    if (!answer) return;
    await act(async () => {
      await api.del(`/releases/${detail.id}?deleteFiles=${answer.deleteFiles ? "true" : "false"}`);
      state.selected = null;
      ctx.navigate("#/releases");
    }, t("releases.deleted"));
  }

  function partialAlerts(detail) {
    const waiting = detail.candidates.filter((c) => c.status === "partial" && !c.completeRequested);
    return waiting.map((c) => {
      const ok = c.files.filter((f) => f.status === "identical").length;
      return alertBox("", t("releases.action.title"), t("releases.action.text", { ok, total: c.files.length, tracker: c.indexer }),
        button(t("releases.complete_from", { tracker: c.indexer }), () =>
          act(() => api.post(`/releases/${detail.id}/complete`, { candidateId: c.id })), { kind: "primary", small: true }));
    });
  }

  function overview(detail) {
    const failed = detail.stages.find((s) => s.state === "error");
    return [
      state.meta?.id === detail.id && metaPanel(state.meta.data),
      ...partialAlerts(detail),
      failed && alertBox("error", t(`stage.${failed.name}`), tm(failed.message),
        button(t("releases.retry_from", { stage: t(`stage.${failed.name}`) }), () => retry(failed.name), { small: true })),
      card(t("releases.timeline"), timeline(detail.stages, retry)),
      card(null,
        h("div", { class: "kv" }, h("span", {}, t("releases.status")), pill(detail.status)),
        h("div", { class: "kv" }, h("span", {}, t("releases.files")), h("span", { class: "num" }, String(detail.files))),
        h("div", { class: "kv" }, h("span", {}, t("releases.size")), h("span", { class: "num" }, bytes(detail.total))),
        techFacts(detail.fileList.find((f) => f.media)?.media).map(([label, value]) =>
          h("div", { class: "kv" }, h("span", {}, label), h("span", {}, value)))),
    ];
  }

  function downloadTab(detail) {
    return card(t("releases.files"), h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, t("releases.file")), h("th", {}, t("releases.release_name")),
        h("th", { class: "r" }, t("releases.size")), h("th", {}, t("releases.status")))),
      h("tbody", {}, detail.fileList.map((f) => h("tr", {},
        h("td", {}, f.filename, f.status === "running" && progress(f.done, f.total),
          f.media && h("small", {}, [videoLine(f.media), audioLine(f.media)].filter(Boolean).join(" — "))),
        h("td", {}, h("small", {}, f.releaseName || t("common.unknown"))),
        h("td", { class: "r num" }, bytes(f.size ?? f.total)),
        h("td", {}, pill(f.status), f.error && h("small", {}, t(f.error, f.errorParams || {}))))))));
  }

  function scopeLabel(scope) {
    if (scope === "pack") return t("releases.scope.pack");
    if (scope === "movie") return t("releases.scope.movie");
    return scope;
  }

  function candidateCard(c) {
    return h("section", { class: "card" },
      h("div", { class: "card-h" }, h("h3", {}, `${c.indexer} · ${scopeLabel(c.scope)}`), pill(c.status)),
      h("div", { class: "card-b stack" },
        h("div", { class: "mono" }, c.infoUrl ? h("a", { href: c.infoUrl, target: "_blank", rel: "noopener noreferrer" }, c.title) : c.title),
        h("div", { class: "inline muted num" }, bytes(c.size), c.seeders !== null && `· ${t("releases.seeders")} ${c.seeders}`),
        c.problem && h("p", { class: "muted" }, t(c.problem, c.problemParams || {}))),
      c.files.length > 0 && h("table", {},
        h("tbody", {}, c.files.map((f) => h("tr", {},
          h("td", {}, h("small", {}, f.path)), h("td", { class: "r num" }, bytes(f.size)), h("td", {}, pill(f.status)))))));
  }

  function identificationTab(detail) {
    const candidates = detail.candidates.filter((c) => c.isCandidate);
    const others = detail.candidates.filter((c) => !c.isCandidate);
    return [
      ...partialAlerts(detail),
      candidates.length ? candidates.map(candidateCard) : emptyState(t("verify.no_candidates")),
      others.length > 0 && card(t("releases.other_versions"), h("table", {}, h("tbody", {}, others.map((c) =>
        h("tr", {}, h("td", {}, c.indexer), h("td", {}, h("small", {}, c.title)), h("td", { class: "r num" }, bytes(c.size))))))),
    ];
  }

  function crossseedTab(detail) {
    const refs = detail.candidates.filter((c) => c.linked);
    if (!refs.length) return emptyState(t("seed.nothing"));
    return card(t("releases.tab.crossseed"), h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, t("releases.tracker")), h("th", {}, t("releases.qbit_state")),
        h("th", { class: "r" }, t("releases.ratio")), h("th", { class: "r" }, t("releases.uploaded")))),
      h("tbody", {}, refs.map((c) => h("tr", {},
        h("td", {}, c.indexer, h("small", {}, c.title)),
        h("td", {}, c.qbittorrent ? h("span", { class: "mono" }, c.qbittorrent.state) : t("releases.not_in_qbit")),
        h("td", { class: "r num" }, c.qbittorrent ? Number(c.qbittorrent.ratio).toFixed(2) : "—"),
        h("td", { class: "r num" }, c.qbittorrent ? bytes(c.qbittorrent.uploaded) : "—"))))));
  }

  const TAB_VIEW = { overview, download: downloadTab, identification: identificationTab, crossseed: crossseedTab };

  function drawDetail(detail) {
    const tabs = h("div", { class: "tabs", role: "tablist" }, TABS.map((tab) => h("button", {
      role: "tab", type: "button", "aria-selected": String(tab === state.tab),
      onclick: () => { state.tab = tab; drawDetail(detail); },
    }, t(`releases.tab.${tab}`))));
    replace(detailPane,
      h("header", { class: "glass-head" },
        h("div", { class: "crumb" }, detail.kind === "movie" ? t("releases.scope.movie") : t("explorer.season", { season: detail.season })),
        h("div", { class: "inline" }, h("h2", { class: "title grow" }, detail.title), pill(detail.status),
          button("", () => remove(detail), { kind: "icon", iconName: "trash" })),
        tabs),
      h("div", { class: "content", role: "tabpanel" }, TAB_VIEW[state.tab](detail)));
    detailPane.querySelector(".btn.icon").setAttribute("aria-label", t("releases.delete"));
  }

  async function loadDetail() {
    if (!state.selected) {
      replace(detailPane, h("div", { class: "content" }, emptyState(t("releases.pick"))));
      return;
    }
    try {
      const detail = await api.get(`/releases/${state.selected}`);
      if (state.meta?.id !== detail.id) {
        state.meta = { id: detail.id, data: null };
        fetchMeta(detail.kind, { tmdbId: detail.tmdbId, title: detail.title, year: detail.year }).then((data) => {
          if (state.meta?.id === detail.id && data) {
            state.meta.data = data;
            state.seen.detail = null;
            drawDetail(detail);
          }
        });
      }
      if (changed("detail", detail)) drawDetail(detail);
    } catch (error) {
      replace(detailPane, h("div", { class: "content" }, emptyState(t(error.code, error.params))));
    }
  }

  async function refresh() {
    await Promise.all([loadList(), loadDetail()]);
  }

  drawFilters();
  refresh();
  return { refresh };
}
