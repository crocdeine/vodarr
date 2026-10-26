// Settings: sources, services (with tests and the snatch warning), folders, cross-seed layout
// (with config.js import), API key, language and theme. Secrets come back as •••• and are sent
// back unchanged when untouched (AC-SET-02/03).

import { api } from "../api.js";
import { h, replace, trackDirty } from "../dom.js";
import { t, tm } from "../i18n.js";
import { alertBox, button, card, confirmDialog, field, input, select, toast, toastError } from "../ui.js";

export function settingsView(main, _arg, ctx) {
  const content = h("div", { class: "content" });
  main.append(h("section", { class: "pane" }, h("header", { class: "glass-head" }, h("h1", { class: "title" }, t("settings.title"))), content));

  async function save(body) {
    try {
      ctx.settings = await api.put("/settings", body);
      toast(t("common.saved"));
      await ctx.applyUi(ctx.settings.ui);
      return true;
    } catch (error) {
      toastError(error);
      return false;
    }
  }

  /** Tests what is typed in the form, saved or not. */
  async function testService(service, el) {
    const data = new FormData(el.closest("form"));
    const values = {
      prowlarr: { url: data.get("purl"), apiKey: data.get("pkey") },
      qbittorrent: { url: data.get("qurl"), username: data.get("quser"), password: data.get("qpass") },
      tmdb: { apiKey: data.get("tkey") },
    }[service];
    try {
      const r = await api.post("/settings/test", { service, ...values });
      r.ok ? toast(t("settings.test.ok")) : toast(tm(r.error), "error");
    } catch (error) {
      toastError(error);
    }
  }

  function form(onSubmit, ...children) {
    const el = trackDirty(h("form", {
      class: "card-b stack",
      onsubmit: async (e) => {
        e.preventDefault();
        if (await onSubmit(new FormData(el))) el.removeAttribute("data-dirty");
      },
    }, children, h("div", { class: "inline" }, button(t("common.save"), null, { kind: "primary", type: "submit" }))));
    return el;
  }

  // --- sources -----------------------------------------------------------------------------

  function sourceForm(source) {
    const type = select([["xtream", "Xtream Codes"], ["m3u", "M3U"]], source?.type || "xtream", { name: "type", disabled: !!source });
    const xtream = h("div", { class: "stack" },
      field(t("settings.source.server"), input({ name: "server", value: source?.server || "", placeholder: "http://provider:8080" })),
      h("div", { class: "grid2" },
        field(t("settings.source.user"), input({ name: "user", value: source?.user || "", autocomplete: "off" })),
        field(t("settings.source.password"), input({ name: "password", type: "password", value: source?.password || "", autocomplete: "off" }))));
    const m3u = h("div", { class: "stack" },
      field(t("settings.source.url"), input({ name: "url", value: source?.url || "", autocomplete: "off" })),
      field(t("settings.source.path"), input({ name: "path", value: source?.path || "", placeholder: "list.m3u" })));
    const toggle = () => { xtream.hidden = type.value !== "xtream"; m3u.hidden = type.value !== "m3u"; };
    type.addEventListener("change", toggle);
    toggle();
    const split = h("input", { type: "checkbox", name: "splitPrefix", checked: !!source?.splitPrefix });
    return form(async (data) => {
      const body = { name: data.get("name"), splitPrefix: split.checked };
      const kind = source?.type || type.value;
      for (const key of kind === "xtream" ? ["server", "user", "password"] : ["url", "path"]) {
        const value = data.get(key);
        if (value) body[key] = value;
      }
      try {
        if (source) await api.patch(`/sources/${source.id}`, body);
        else await api.post("/sources", { ...body, type: kind });
        toast(t("common.saved"));
        ctx.reload();
        return true;
      } catch (error) {
        toastError(error);
        return false;
      }
    },
    h("div", { class: "grid2" }, field(t("settings.source.name"), input({ name: "name", value: source?.name || "", required: true, maxlength: "100" })),
      field(t("settings.source.type"), type)),
    xtream, m3u, h("label", { class: "check" }, split, t("settings.source.split")),
    source && h("div", { class: "inline" },
      button(t("common.test"), async () => {
        const r = await api.post(`/sources/${source.id}/test`).catch((e) => ({ ok: false, error: { code: e.code, params: e.params } }));
        r.ok ? toast(t("settings.test.ok")) : toast(tm(r.error), "error");
      }, { small: true }),
      button(t("common.refresh"), () => api.post(`/sources/${source.id}/refresh`).then(() => toast(t("common.ok")), toastError), { small: true }),
      button(t("settings.source.delete"), async () => {
        if (!(await confirmDialog(t("settings.source.delete"), source.name, { danger: true, confirm: t("common.delete") }))) return;
        api.del(`/sources/${source.id}`).then(() => ctx.reload(), toastError);
      }, { small: true, kind: "danger" })));
  }

  // --- services, folders, cross-seed, interface ---------------------------------------------

  function services(s) {
    const p = s.services.prowlarr;
    const q = s.services.qbittorrent;
    const k = s.services.tmdb;
    return card(t("settings.services"), form(async (data) => save({ services: {
      prowlarr: { url: data.get("purl"), apiKey: data.get("pkey"), maxChecksPerTracker: Number(data.get("plimit") || 0) },
      qbittorrent: { url: data.get("qurl"), username: data.get("quser"), password: data.get("qpass") },
      tmdb: { apiKey: data.get("tkey") },
    } }),
    h("h4", {}, t("settings.prowlarr")),
    alertBox("info", t("settings.prowlarr"), t("settings.snatch_warning")),
    h("div", { class: "grid2" },
      field(t("settings.url"), input({ name: "purl", value: p.url, placeholder: "http://prowlarr:9696" })),
      field(t("settings.api_key"), input({ name: "pkey", type: "password", value: p.apiKey, autocomplete: "off" }))),
    field(t("settings.max_checks"), input({ name: "plimit", type: "number", min: 0, max: 100, step: 1, value: p.maxChecksPerTracker ?? 0, inputmode: "numeric" }), t("settings.max_checks_hint")),
    h("div", {}, button(t("common.test"), (e) => testService("prowlarr", e.currentTarget), { small: true })),
    h("h4", {}, t("settings.qbittorrent")),
    field(t("settings.url"), input({ name: "qurl", value: q.url, placeholder: "http://qbittorrent:8080" })),
    h("div", { class: "grid2" },
      field(t("auth.username"), input({ name: "quser", value: q.username, autocomplete: "off" })),
      field(t("auth.password"), input({ name: "qpass", type: "password", value: q.password, autocomplete: "off" }))),
    h("div", {}, button(t("common.test"), (e) => testService("qbittorrent", e.currentTarget), { small: true })),
    h("h4", {}, t("settings.tmdb")),
    field(t("settings.api_key"), input({ name: "tkey", type: "password", value: k.apiKey, autocomplete: "off" }), t("settings.tmdb.hint")),
    h("div", {}, button(t("common.test"), (e) => testService("tmdb", e.currentTarget), { small: true }))));
  }

  function folders(s) {
    return card(t("settings.paths"), form(async (data) => save({ paths: {
      downloads: data.get("downloads"), dataRoot: data.get("dataRoot"), qbitDataRoot: data.get("qbitDataRoot"),
    } }),
    field(t("settings.downloads"), input({ name: "downloads", value: s.paths.downloads, placeholder: "/data/vod" }), t("settings.downloads.hint")),
    h("div", { class: "grid2" },
      field(t("settings.data_root"), input({ name: "dataRoot", value: s.paths.dataRoot })),
      field(t("settings.qbit_data_root"), input({ name: "qbitDataRoot", value: s.paths.qbitDataRoot })))));
  }

  function crossseed(s) {
    const cs = s.crossseed;
    const dirs = h("textarea", { class: "input", name: "linkDirs", rows: "3" }, cs.linkDirs.join("\n"));
    const linkType = select([["hardlink", "hardlink"], ["symlink", "symlink"], ["reflink", "reflink"]], cs.linkType, { name: "linkType" });
    const flat = h("input", { type: "checkbox", name: "flatLinking", checked: cs.flatLinking });
    const category = input({ name: "linkCategory", value: cs.linkCategory, maxlength: "100" });
    const importPath = input({ placeholder: "/cross-seed/config.js", "aria-label": t("settings.import.path") });
    const theForm = form(async (data) => save({ crossseed: {
      linkDirs: String(data.get("linkDirs")).split("\n").map((d) => d.trim()).filter(Boolean),
      linkType: data.get("linkType"), flatLinking: flat.checked, linkCategory: data.get("linkCategory"),
    } }),
    field(t("settings.link_dirs"), dirs),
    h("div", { class: "grid2" }, field(t("settings.link_type"), linkType), field(t("settings.category"), category, t("settings.category.hint"))),
    h("label", { class: "check" }, flat, t("settings.flat")),
    h("h4", {}, t("settings.import")),
    field(t("settings.import.path"), importPath),
    h("div", {}, button(t("settings.import"), async () => {
      try {
        const r = await api.post("/settings/cross-seed-import", { path: importPath.value });
        const v = r.values;
        if (v.linkDirs) dirs.value = v.linkDirs.join("\n");
        if (v.linkType) linkType.value = v.linkType;
        if (typeof v.flatLinking === "boolean") flat.checked = v.flatLinking;
        if (v.linkCategory) category.value = v.linkCategory;
        theForm.setAttribute("data-dirty", "");
        toast(r.manual.length ? t("settings.import.manual", { keys: r.manual.join(", ") }) : t("settings.import.done"));
      } catch (error) {
        toastError(error);
      }
    }, { small: true })));
    return card(t("settings.crossseed"), theForm);
  }

  function security() {
    const keyOut = h("code", { class: "mono" }, "••••");
    return card(t("settings.security"), h("div", { class: "card-b stack" },
      h("div", { class: "inline" }, h("span", { class: "muted" }, t("settings.api_key")), keyOut, h("span", { class: "grow" }),
        button(t("settings.reveal"), async () => {
          const pw = input({ name: "password", type: "password", autocomplete: "current-password", required: true });
          const answer = await confirmDialog(t("settings.api_key"), t("settings.reveal.prompt"), { extra: field(t("auth.password"), pw) });
          if (!answer) return;
          try {
            keyOut.textContent = (await api.post("/auth/api-key/reveal", { password: answer.password })).apiKey;
          } catch (error) {
            toastError(error);
          }
        }, { small: true }),
        button(t("settings.regenerate"), async () => {
          if (!(await confirmDialog(t("settings.regenerate"), t("settings.regenerate.confirm"), { danger: true }))) return;
          api.post("/auth/api-key").then(() => { keyOut.textContent = "••••"; toast(t("common.saved")); }, toastError);
        }, { small: true }))));
  }

  function interfaceCard(s) {
    return card(t("settings.interface"), form(async (data) => save({ ui: { language: data.get("language"), theme: data.get("theme") } }),
      h("div", { class: "grid2" },
        field(t("settings.language"), select([["auto", t("settings.lang.auto")], ["en", t("settings.lang.en")], ["fr", t("settings.lang.fr")]], s.ui.language, { name: "language" })),
        field(t("settings.theme"), select([["auto", t("settings.theme.auto")], ["dark", t("settings.theme.dark")], ["light", t("settings.theme.light")]], s.ui.theme, { name: "theme" })))));
  }

  function draw() {
    const s = ctx.settings;
    replace(content,
      card(t("settings.sources"), ctx.sources.map((src) => h("details", { class: "card-b" }, h("summary", {}, `${src.name} · ${src.type}`), sourceForm(src))),
        h("details", { class: "card-b" }, h("summary", {}, t("settings.source.add")), sourceForm(null))),
      services(s), folders(s), crossseed(s), security(), interfaceCard(s));
  }

  draw();
  return {};
}
