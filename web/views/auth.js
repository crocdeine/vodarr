// First-run admin creation and sign-in (AC-AUTH-01, AC-AUTH-02).

import { api } from "../api.js";
import { h } from "../dom.js";
import { t } from "../i18n.js";
import { button, field, input, toastError } from "../ui.js";

export function authScreen(mode, onDone) {
  const code = input({ name: "setupCode", required: true, maxlength: "64", autocomplete: "one-time-code", spellcheck: "false" });
  const username = input({ name: "username", autocomplete: "username", required: true, maxlength: "100" });
  const password = input({
    name: "password", type: "password", required: true, maxlength: "1024",
    minlength: mode === "setup" ? "12" : undefined,
    autocomplete: mode === "setup" ? "new-password" : "current-password",
  });
  const form = h(
    "form",
    {
      class: "card auth-card",
      onsubmit: async (event) => {
        event.preventDefault();
        try {
          const body = { username: username.value, password: password.value };
          if (mode === "setup") body.setupCode = code.value;
          await api.post(mode === "setup" ? "/auth/setup" : "/auth/login", body);
          onDone();
        } catch (error) {
          toastError(error);
        }
      },
    },
    h("div", { class: "brand" }, h("img", { class: "logo", src: "/icon.svg", alt: "" }), t("app.name")),
    h("h1", { class: "title" }, t(mode === "setup" ? "auth.setup.title" : "auth.login.title")),
    mode === "setup" && h("p", { class: "muted" }, t("auth.setup.text")),
    mode === "setup" && field(t("auth.setup_code"), code, t("auth.setup_code.hint")),
    field(t("auth.username"), username),
    field(t("auth.password"), password, mode === "setup" && t("auth.password.hint")),
    button(t(mode === "setup" ? "auth.setup.submit" : "auth.login.submit"), null, { kind: "primary", type: "submit" }),
  );
  return h("main", { class: "center" }, form);
}
