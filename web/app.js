// Shell: auth gate, hash router, sidebar, polling every 2 s that never rebuilds a form being
// edited (AC-UI-04), theme and language from the settings.

import { api, setUnauthorizedHandler } from "./api.js";
import { editing, h, replace } from "./dom.js";
import { icon } from "./icons.js";
import { setLanguage, t } from "./i18n.js";
import { authScreen } from "./views/auth.js";
import { downloadsDrawer } from "./views/downloads.js";
import { explorerView } from "./views/explorer.js";
import { releasesView } from "./views/releases.js";
import { settingsView } from "./views/settings.js";

const root = document.getElementById("root");
const VIEWS = { explorer: explorerView, releases: releasesView, settings: settingsView };
const POLL_MS = 2000;
const ctx = { settings: null, sources: [], navigate, reload: boot, applyUi, drawer: null };
let current = null;
let timer = null;

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function applyTheme(choice) {
  const dark = choice === "dark" || (choice !== "light" && !matchMedia("(prefers-color-scheme: light)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

async function applyUi(ui) {
  applyTheme(ui?.theme || "auto");
  await setLanguage(ui?.language || "auto");
}

async function boot() {
  clearInterval(timer);
  await setLanguage("auto");
  applyTheme("auto");
  const status = await api.get("/auth/status");
  if (status.setupRequired || !status.authenticated) {
    replace(root, authScreen(status.setupRequired ? "setup" : "login", boot));
    return;
  }
  ctx.settings = await api.get("/settings");
  ctx.sources = await api.get("/sources");
  await applyUi(ctx.settings.ui);
  route();
  timer = setInterval(poll, POLL_MS);
}

function parse() {
  const [, name = "releases", ...rest] = location.hash.split("/");
  return { name: VIEWS[name] ? name : "releases", arg: rest.join("/") || null };
}

function navLink(name, iconName, label) {
  return h("a", { class: "nav", href: `#/${name}`, "aria-current": parse().name === name ? "page" : undefined }, icon(iconName), label);
}

function sidebar() {
  ctx.drawer ||= downloadsDrawer(ctx);
  return h(
    "nav",
    { class: "sidebar", "aria-label": t("app.name") },
    h("div", { class: "brand" }, h("img", { class: "logo", src: "/icon.svg", alt: "" }), t("app.name")),
    navLink("explorer", "compass", t("nav.explorer")),
    navLink("releases", "layers", t("nav.releases")),
    navLink("settings", "gear", t("nav.settings")),
    ctx.sources.length > 0 && h("div", { class: "section-label" }, t("nav.sources")),
    ctx.sources.map((s) => h("a", { class: "nav", href: `#/explorer/${s.id}` }, icon(s.type === "m3u" ? "film" : "tv"), s.name)),
    h("div", { class: "grow" }),
    ctx.drawer.card,
    h("button", { class: "nav", type: "button", onclick: logout }, icon("logout"), t("nav.logout")),
  );
}

async function logout() {
  await api.post("/auth/logout").catch(() => {});
  boot();
}

function route() {
  current?.destroy?.();
  const { name, arg } = parse();
  const main = h("div", { class: "contents" });
  const shell = h("div", { class: "app wide" }, sidebar(), main);
  replace(root, shell, ctx.drawer.panel);
  current = VIEWS[name](main, arg, ctx);
  document.title = `${t(`nav.${name}`)} · ${t("app.name")}`;
}

function poll() {
  if (document.visibilityState !== "visible") return;
  ctx.drawer?.refresh();
  const main = root.querySelector(".contents");
  if (current?.refresh && main && !editing(main)) current.refresh();
}

setUnauthorizedHandler(() => boot());
window.addEventListener("hashchange", route);
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => applyTheme(ctx.settings?.ui?.theme || "auto"));
boot();
