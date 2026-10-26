// Translation: the backend sends codes and parameters, the interface turns them into text
// (ADR-0007). Unknown codes are shown as is, so a missing key is visible.

const dictionaries = {};
let lang = "en";

export async function setLanguage(choice) {
  const wanted = choice && choice !== "auto" ? choice : (navigator.language || "en").slice(0, 2);
  lang = ["en", "fr"].includes(wanted) ? wanted : "en";
  for (const code of new Set(["en", lang])) {
    if (!dictionaries[code]) {
      const response = await fetch(`/i18n/${code}.json`, { credentials: "same-origin" });
      dictionaries[code] = await response.json();
    }
  }
  document.documentElement.lang = lang;
  return lang;
}

export function language() {
  return lang;
}

export function t(key, params = {}) {
  let k = key;
  if (typeof params.count === "number") {
    const plural = new Intl.PluralRules(lang).select(params.count) === "one" ? "one" : "other";
    if (`${key}.${plural}` in (dictionaries[lang] || {})) k = `${key}.${plural}`;
  }
  const text = dictionaries[lang]?.[k] ?? dictionaries.en?.[k] ?? String(key ?? "");
  return text.replace(/\{(\w+)\}/g, (_, name) => (name in params && params[name] !== null ? fmt(params[name]) : "—"));
}

/** A {code, params} message from the API. */
export function tm(message) {
  if (!message) return "";
  const params = { ...message.params };
  if (message.code === "analyse.done" && !params.release) params.release = t("analyse.unknown_release");
  return t(message.code, params);
}

function fmt(value) {
  return typeof value === "number" ? new Intl.NumberFormat(lang).format(value) : String(value);
}

export function bytes(n) {
  if (n === null || n === undefined) return "—";
  const units = lang === "fr" ? ["o", "Ko", "Mo", "Go", "To"] : ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i += 1;
  }
  return `${new Intl.NumberFormat(lang, { maximumFractionDigits: i ? 1 : 0 }).format(v)} ${units[i]}`;
}

export function date(seconds) {
  return new Intl.DateTimeFormat(lang, { dateStyle: "medium", timeStyle: "short" }).format(new Date(seconds * 1000));
}
