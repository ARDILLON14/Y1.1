// API client: session cookie + CSRF header on every state-changing request.
let csrf = null;
export function setCsrf(token) { csrf = token; }

export class ApiError extends Error {
  constructor(status, detail) { super(detail); this.status = status; }
}

async function request(method, path, body) {
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
  const resp = await fetch(`/api${path}`, {
    method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await resp.json(); } catch { data = null; }
  if (!resp.ok) {
    let detail = data && data.detail !== undefined ? data.detail : resp.statusText;
    if (Array.isArray(detail)) detail = detail.map((d) => `${(d.loc || []).join(".")}: ${d.msg}`).join("; ");
    if (resp.status === 401) window.dispatchEvent(new CustomEvent("auth-required"));
    throw new ApiError(resp.status, String(detail));
  }
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b = {}) => request("POST", p, b),
  patch: (p, b = {}) => request("PATCH", p, b),
  del: (p) => request("DELETE", p),
};
