// Explorer: sources → categories (by provider) → titles (search, pages) → seasons / episodes,
// then download a season, a selection of episodes, or a movie (AC-UI-01).

import { api } from "../api.js";
import { h, replace } from "../dom.js";
import { icon } from "../icons.js";
import { t } from "../i18n.js";
import { button, card, emptyState, fetchMeta, input, metaPanel, pill, poster, segmented, select, toast, toastError } from "../ui.js";

const PAGE = 50;

export function explorerView(main, arg, ctx) {
  // #/explorer/<source>[/<kind>/<item>]: a release links back to the exact catalogue entry.
  const [sourceId, linkKind, linkItem] = (arg || "").split("/");
  if (!ctx.sources.length) {
    main.append(h("section", { class: "pane" }, h("header", { class: "glass-head" }, h("h1", { class: "title" }, t("explorer.title"))),
      h("div", { class: "content" }, emptyState(t("explorer.no_source")), h("a", { class: "btn primary", href: "#/settings" }, t("nav.settings")))));
    return {};
  }
  const state = { source: ctx.sources.find((s) => s.id === sourceId) || ctx.sources[0], kind: "series", category: "", q: "", page: 1, item: null };
  const listPane = h("section", { class: "pane list" });
  const detailPane = h("section", { class: "pane" }, h("div", { class: "content" }, emptyState(t("explorer.pick"))));
  main.append(h("div", { class: "split" }, listPane, detailPane));

  const search = input({ type: "search", placeholder: t("common.search"), "aria-label": t("common.search"), maxlength: "200" });
  let debounce;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { state.q = search.value; state.page = 1; loadItems(); }, 250);
  });
  const sourceSelect = select(ctx.sources.map((s) => [s.id, s.name]), state.source.id, { "aria-label": t("nav.sources") });
  sourceSelect.addEventListener("change", () => ctx.navigate(`#/explorer/${sourceSelect.value}`));
  const kindRoot = h("div");
  const categoryRoot = h("div");
  const rows = h("div", { class: "rows", "aria-live": "polite" });
  const pager = h("div", { class: "pager" });

  replace(listPane, h("header", { class: "pane-head toolbar" }, h("h1", { class: "title" }, t("explorer.title")),
    ctx.sources.length > 1 && sourceSelect, kindRoot, categoryRoot, h("label", { class: "search" }, icon("search"), search)), rows, pager);

  function drawKind() {
    replace(kindRoot, segmented([["series", t("explorer.series")], ["movie", t("explorer.movies")]], state.kind, (k) => {
      state.kind = k; state.category = ""; state.page = 1; drawKind(); loadCategories(); loadItems();
    }, t("explorer.title")));
  }

  async function loadCategories() {
    try {
      const cats = await api.get(`/sources/${state.source.id}/categories?kind=${state.kind}`);
      const groups = Map.groupBy ? Map.groupBy(cats, (c) => c.provider) : cats.reduce((m, c) => m.set(c.provider, [...(m.get(c.provider) || []), c]), new Map());
      const el = h("select", { class: "input", "aria-label": t("explorer.category") },
        h("option", { value: "" }, t("explorer.all_categories")),
        [...groups].map(([provider, list]) => h("optgroup", { label: provider }, list.map((c) => h("option", { value: c.id, selected: c.id === state.category }, c.label)))));
      el.addEventListener("change", () => { state.category = el.value; state.page = 1; loadItems(); });
      replace(categoryRoot, el);
    } catch (error) {
      replace(categoryRoot);
      toastError(error);
    }
  }

  async function loadItems() {
    const params = new URLSearchParams({ kind: state.kind, page: state.page, pageSize: PAGE, q: state.q });
    if (state.category) params.set("category", state.category);
    replace(rows, h("div", { class: "empty" }, t("common.loading")));
    try {
      const page = await api.get(`/sources/${state.source.id}/items?${params}`);
      replace(rows, page.data.length ? page.data.map(itemRow) : emptyState(t("explorer.empty")));
      const { page: n, totalPages } = page.pagination;
      replace(pager,
        button(t("common.previous"), () => { state.page -= 1; loadItems(); }, { small: true, disabled: n <= 1 }),
        h("span", { class: "num" }, t("common.page", { page: n, pages: totalPages })),
        button(t("common.next"), () => { state.page += 1; loadItems(); }, { small: true, disabled: n >= totalPages }));
    } catch (error) {
      replace(rows, emptyState(t(error.code, error.params)));
    }
  }

  function itemRow(item) {
    return h("button", {
      class: "row", type: "button", "aria-current": state.item?.id === item.id ? "true" : undefined,
      onclick: (e) => {
        state.item = item;
        for (const r of rows.children) r.removeAttribute("aria-current");
        e.currentTarget.setAttribute("aria-current", "true");
        loadDetail();
      },
    }, poster(item.cover), h("div", { class: "grow" }, h("div", { class: "t" }, item.name), h("div", { class: "s num" }, item.year || ""),
      queuedPills(item.releases)));
  }

  /** Releases already made from this title: "S4 · Done"… so one provider's entry stands out. */
  function queuedPills(releases) {
    if (!releases?.length) return null;
    return h("div", { class: "inline queued" }, releases.map((r) =>
      pill(r.status, r.season == null ? t(`status.${r.status}`) : `S${r.season} · ${t(`status.${r.status}`)}`)));
  }

  function releaseLink(release) {
    return release && h("a", { class: "btn small", href: `#/releases/${release.id}` }, pill(release.status), t("explorer.open_release"));
  }

  async function loadDetail() {
    const item = state.item;
    replace(detailPane, h("div", { class: "content" }, emptyState(t("common.loading"))));
    try {
      const path = state.kind === "series" ? "series" : "movies";
      const detail = await api.get(`/sources/${state.source.id}/${path}/${encodeURIComponent(item.id)}`);
      if (state.item !== item) return; // another title was picked meanwhile: its answer wins
      replace(detailPane, state.kind === "series" ? seriesDetail(item, detail) : movieDetail(item, detail));
      const meta = await fetchMeta(state.kind, { tmdbId: detail.tmdbId, title: detail.name || item.name, year: detail.year || item.year });
      if (meta && state.item === item) detailPane.querySelector(".content")?.prepend(metaPanel(meta));
    } catch (error) {
      if (state.item !== item) return;
      replace(detailPane, h("div", { class: "content" }, emptyState(t(error.code, error.params))));
    }
  }

  function header(item, detail) {
    return h("header", { class: "glass-head" }, h("div", { class: "crumb" }, state.source.name),
      h("h2", { class: "title" }, detail.name || item.name), detail.year && h("div", { class: "muted num" }, detail.year),
      detail.plot && h("p", { class: "muted" }, detail.plot));
  }

  async function queue(item, body) {
    try {
      await api.post("/releases", { sourceId: state.source.id, itemId: item.id, ...body });
      toast(t("explorer.queued"));
      ctx.drawer?.refresh();
      if (state.item === item) loadDetail(); // show the new release on its season
    } catch (error) {
      toastError(error);
    }
  }

  function seriesDetail(item, detail) {
    return h("div", {}, header(item, detail), h("div", { class: "content" }, detail.seasons.map((season) => {
      const boxes = [];
      const selectionButton = button("", () => queue(item, { kind: "series", season: season.season, episodes: boxes.filter((b) => b.checked).map((b) => b.value) }), { disabled: true });
      const update = () => {
        const n = boxes.filter((b) => b.checked).length;
        selectionButton.disabled = n === 0;
        selectionButton.textContent = t("explorer.download_selection", { count: n });
      };
      update();
      const made = (detail.releases || []).find((r) => r.season === season.season);
      return card(t("explorer.season", { season: season.season }),
        h("div", { class: "card-b inline" }, h("span", { class: "muted" }, t("explorer.episodes", { count: season.episodes.length })),
          h("span", { class: "grow" }), releaseLink(made),
          button(t("explorer.download_season"), () => queue(item, { kind: "series", season: season.season }), { kind: "primary", iconName: "down", small: true }),
          selectionButton),
        season.episodes.map((ep) => {
          const box = h("input", { type: "checkbox", value: ep.id, onchange: update, "aria-label": ep.title });
          boxes.push(box);
          return h("label", { class: "episode check" }, box, h("span", {}, `E${String(ep.num).padStart(2, "0")} · ${ep.title}`),
            ep.release && h("small", {}, ep.release));
        }));
    })));
  }

  function movieDetail(item, detail) {
    return h("div", {}, header(item, detail), h("div", { class: "content" },
      card(t("explorer.release"), h("div", { class: "card-b stack" }, h("p", { class: "mono" }, detail.release || t("common.unknown")),
        h("div", { class: "inline" }, button(t("explorer.download_movie"), () => queue(item, { kind: "movie" }), { kind: "primary", iconName: "down" }),
          releaseLink((detail.releases || [])[0]))))));
  }

  if (linkItem && (linkKind === "series" || linkKind === "movie")) {
    state.kind = linkKind;
    state.item = { id: decodeURIComponent(linkItem), name: "" };
    loadDetail();
  }
  drawKind();
  loadCategories();
  loadItems();
  return {};
}
