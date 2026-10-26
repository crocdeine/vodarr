// JSON client for /api: one error shape ({error: {code, params}}), 401 sends back to sign-in.

export class ApiError extends Error {
  constructor(status, error) {
    super(error?.code || `http.${status}`);
    this.status = status;
    this.code = error?.code || "http.error";
    this.params = error?.params || {};
  }
}

let onUnauthorized = () => {};

export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn;
}

async function request(method, path, body) {
  const init = { method, credentials: "same-origin", headers: { Accept: "application/json" } };
  if (method !== "GET" && method !== "DELETE") {
    // Cookie mutations must be JSON (CSRF guard), even without a payload.
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body ?? {});
  }
  let response;
  try {
    response = await fetch(`/api${path}`, init);
  } catch {
    throw new ApiError(0, { code: "http.unreachable" }); // server stopped or network down
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new ApiError(response.status, data?.error);
    if (response.status === 401 && !path.startsWith("/auth/")) onUnauthorized(error);
    throw error;
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body),
  put: (path, body) => request("PUT", path, body),
  patch: (path, body) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),
};
