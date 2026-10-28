// DOM helpers: elements are built with createElement/textContent only. Titles, file names and
// tracker names come from untrusted sources: never innerHTML with data (ADR-0008).

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, value);
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

export function replace(el, ...children) {
  // Polling rebuilds lists: a focused control with a data-key gets the focus back.
  const key = el.contains(document.activeElement) ? document.activeElement.dataset?.key : null;
  el.replaceChildren();
  append(el, children);
  if (key) el.querySelector(`[data-key="${CSS.escape(key)}"]`)?.focus();
  return el;
}

/** True while the user is typing in this subtree: polling must not rebuild it (AC-UI-04). */
export function editing(root) {
  const active = document.activeElement;
  if (active && root.contains(active) && active.matches("input:not([type=checkbox]), textarea")) return true;
  return !!root.querySelector("form[data-dirty]");
}

export function trackDirty(form) {
  form.addEventListener("input", () => form.setAttribute("data-dirty", ""));
  return form;
}
