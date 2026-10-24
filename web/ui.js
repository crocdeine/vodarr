// Shared components built from the design system (components.css).

import { h } from "./dom.js";
import { icon } from "./icons.js";
import { language, t, tm } from "./i18n.js";
import { api } from "./api.js";

export function pill(status, label) {
  return h("span", { class: `pill s-${status}` }, label ?? t(`status.${status}`));
}

export function progress(done, total) {
  return h("progress", { class: "bar", max: String(total || 1), value: String(Math.min(done || 0, total || 1)) });
}

export function button(label, onclick, { kind = "", iconName, small, disabled, type = "button" } = {}) {
  const cls = ["btn", kind, small ? "small" : ""].filter(Boolean).join(" ");
  return h("button", { class: cls, type, onclick, disabled }, iconName && icon(iconName), label);
}

export function field(label, input, hint) {
  return h("label", { class: "field" }, h("span", {}, label), input, hint && h("small", {}, hint));
}

export function input(attrs = {}) {
  return h("input", { class: "input", ...attrs });
}

export function select(options, value, attrs = {}) {
  return h(
    "select",
    { class: "input", ...attrs },
    options.map(([v, label]) => h("option", { value: v, selected: v === value }, label)),
  );
}

export function card(title, ...body) {
  return h("section", { class: "card" }, title && h("div", { class: "card-h" }, h("h3", {}, title)), body);
}

export function segmented(items, current, onChange, label) {
  const root = h("div", { class: "segmented", role: "tablist", "aria-label": label });
  for (const [value, text] of items) {
    root.append(
      h("button", { role: "tab", type: "button", "aria-selected": String(value === current), onclick: () => onChange(value) }, text),
    );
  }
  return root;
}

const STAGE_ICON = { done: "check", skipped: "skip", error: "x", running: "clock", pending: "dots" };

export function timeline(stages, onRetry) {
  return h(
    "ol",
    { class: "stages" },
    stages.map((s) =>
      h(
        "li",
        { class: `stage s-${s.state}` },
        h("span", { class: `dot s-${s.state}` }, icon(STAGE_ICON[s.state] || "dots", t(`status.${s.state}`))),
        h("b", {}, t(`stage.${s.name}`)),
        h("span", {}, s.state === "running" ? `${s.pct} %` : t(`status.${s.state}`)),
        s.message && h("small", { class: "muted" }, tm(s.message)),
        onRetry && ["error", "done", "skipped"].includes(s.state) && retryButton(s.name, onRetry),
      ),
    ),
  );
}

function retryButton(stage, onRetry) {
  const label = t("releases.retry_from", { stage: t(`stage.${stage}`) });
  return h("button", { class: "btn ghost icon", type: "button", title: label, "aria-label": label, onclick: () => onRetry(stage) }, icon("refresh"));
}

export function alertBox(kind, title, text, ...actions) {
  return h(
    "div",
    { class: `alert ${kind}`, role: kind === "error" ? "alert" : "status" },
    icon(kind === "error" ? "x" : kind === "info" ? "info" : "alert"),
    h("div", { class: "grow" }, h("h4", {}, title), text && h("p", {}, text), actions.length > 0 && h("div", { class: "inline" }, actions)),
  );
}

let toastRoot;

export function toast(text, kind = "ok") {
  toastRoot ||= document.getElementById("toasts");
  const el = h("div", { class: `toast ${kind === "error" ? "error" : ""}` }, icon(kind === "error" ? "alert" : "check"), h("span", {}, text));
  toastRoot.append(el);
  setTimeout(() => el.remove(), 5000);
}

export function toastError(error) {
  toast(error?.code ? t(error.code, error.params) : t("internal"), "error");
}

/** Modal confirmation; resolves with the form values or null. */
export function confirmDialog(title, text, { confirm, danger, extra } = {}) {
  return new Promise((resolve) => {
    const form = h("form", { method: "dialog", class: "stack" }, h("h3", { class: "heading" }, title), text && h("p", { class: "muted" }, text), extra,
      h("div", { class: "inline" },
        button(t("common.cancel"), () => dialog.close("cancel")),
        button(confirm || t("common.ok"), null, { kind: danger ? "danger" : "primary", type: "submit" })));
    const dialog = h("dialog", {}, form);
    dialog.addEventListener("close", () => {
      const values = Object.fromEntries(new FormData(form));
      for (const box of form.querySelectorAll("input[type=checkbox]")) values[box.name] = box.checked;
      dialog.remove();
      resolve(dialog.returnValue === "cancel" ? null : values);
    });
    document.body.append(dialog);
    dialog.showModal();
  });
}

export function emptyState(text) {
  return h("div", { class: "empty" }, text);
}



/** TMDB details for a title (0.1.1): null without a key or a match; never blocks the page. */
export async function fetchMeta(kind, { tmdbId, title, year }) {
  const params = new URLSearchParams({ kind, lang: language() });
  if (tmdbId) params.set("tmdbId", tmdbId);
  if (title) params.set("title", title.replace(/\s*\(\d{4}\)\s*$/, ""));
  if (year && /^\d{4}$/.test(year)) params.set("year", year);
  try {
    return (await api.get(`/metadata?${params}`)).data;
  } catch {
    return null;
  }
}

export function metaPanel(meta) {
  if (!meta) return null;
  const facts = [
    meta.year,
    meta.runtime && t("meta.runtime", { minutes: meta.runtime }),
    meta.seasons && t("meta.seasons", { count: meta.seasons }),
    typeof meta.rating === "number" && meta.rating > 0 && `★ ${meta.rating.toFixed(1)}`,
  ].filter(Boolean);
  return h("section", { class: "card meta" },
    meta.poster && /^https:\/\//.test(meta.poster)
      ? h("img", {
          class: "meta-poster", src: meta.poster, alt: "", loading: "lazy", referrerpolicy: "no-referrer",
          onerror: (e) => e.currentTarget.remove(),
        })
      : null,
    h("div", { class: "card-b stack" },
      meta.tagline && h("p", { class: "muted" }, meta.tagline),
      h("div", { class: "inline muted num" }, facts.join(" · ")),
      meta.genres?.length > 0 && h("div", { class: "inline" }, meta.genres.map((g) => h("span", { class: "chip" }, g))),
      meta.overview && h("p", {}, meta.overview),
      meta.cast?.length > 0 && h("p", { class: "muted" }, t("meta.cast_line", { names: meta.cast.join(", ") })),
      meta.url && h("a", { href: meta.url, target: "_blank", rel: "noopener noreferrer", class: "faint" }, t("meta.source"))));
}
