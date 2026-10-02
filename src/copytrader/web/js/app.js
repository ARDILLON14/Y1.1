import { api, setCsrf } from "./api.js";
import { h, clear, fill, toast, pct } from "./dom.js";

const VIEWS = {
  overview: () => import("./views/overview.js"),
  wallets: () => import("./views/wallets.js"),
  wallet: () => import("./views/wallet_detail.js"),
  trades: () => import("./views/trades.js"),
  signals: () => import("./views/signals.js"),
  positions: () => import("./views/positions.js"),
  alerts: () => import("./views/alerts.js"),
  risk: () => import("./views/risk.js"),
  analytics: () => import("./views/analytics.js"),
  backtest: () => import("./views/backtest.js"),
  config: () => import("./views/config.js"),
  system: () => import("./views/system.js"),
};

const view = document.getElementById("view");
let refreshTimer = null;
let authed = false;

// ------------------------------------------------------------------ theme
function applyTheme(t) {
  if (t) document.documentElement.setAttribute("data-theme", t);
  else document.documentElement.removeAttribute("data-theme");
}
try { applyTheme(localStorage.getItem("ct-theme")); } catch { /* storage unavailable */ }
document.getElementById("theme-toggle").addEventListener("click", () => {
  const dark = document.documentElement.getAttribute("data-theme") === "dark"
    || (!document.documentElement.getAttribute("data-theme") && matchMedia("(prefers-color-scheme: dark)").matches);
  const next = dark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem("ct-theme", next); } catch { /* ignore */ }
  route();
});

// ------------------------------------------------------------------- auth
function showLogin(message) {
  authed = false;
  clearInterval(refreshTimer);
  document.getElementById("logout").hidden = true;
  clear(document.getElementById("status-strip"));
  const user = h("input", { type: "text", value: "admin", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
  const totp = h("input", { type: "text", inputmode: "numeric", autocomplete: "one-time-code", placeholder: "opcional" });
  const err = h("p", { class: "neg small" }, message || "");
  const form = h("form", {
    class: "card", style: { width: "min(360px, 100%)" },
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        const r = await api.post("/auth/login", { username: user.value, password: pw.value, totp: totp.value || null });
        setCsrf(r.csrf);
        authed = true;
        document.getElementById("logout").hidden = false;
        route();
      } catch (ex) { err.textContent = ex.message; pw.value = ""; pw.focus(); }
    },
  },
  h("h1", {}, "Acceso"),
  h("label", { class: "field" }, "Usuario", user),
  h("label", { class: "field section" }, "Contraseña", pw),
  h("label", { class: "field section" }, "Código TOTP", totp),
  err,
  h("button", { class: "primary section", type: "submit" }, "Entrar"));
  clear(view).appendChild(h("div", { class: "login-wrap" }, form));
  pw.focus();
}
window.addEventListener("auth-required", () => { if (authed) showLogin("La sesión ha caducado."); });
document.getElementById("logout").addEventListener("click", async () => {
  try { await api.post("/auth/logout"); } catch { /* ignore */ }
  showLogin();
});

// ------------------------------------------------------------ status strip
async function refreshStatus() {
  if (!authed) return;
  try {
    const s = await api.get("/system/status");
    const strip = document.getElementById("status-strip");
    const m = s.mode;
    const live = m.trade_mode === "live";
    fill(strip,
      h("span", { class: "pill", title: m.live_block_reason || "" }, h("span", { class: `dot ${live ? "critical" : "accent"}` }),
        `Nivel ${m.level} · ${m.level_name}`),
      h("span", { class: "pill" }, h("span", { class: `dot ${live ? "critical" : m.trade_mode ? "good" : ""}` }),
        m.trade_mode ? (live ? "DINERO REAL" : "PAPER") : "Sin ejecución"),
      s.providers_mode === "simulated" ? h("span", { class: "pill" }, h("span", { class: "dot warning" }), "Datos simulados") : null,
      h("span", { class: "pill" }, h("span", { class: `dot ${s.overall === "ok" ? "good" : s.overall === "down" ? "critical" : "warning"}` }),
        `Sistema: ${s.overall}`),
    );
    const ov = await api.get("/overview");
    const ks = ov.kill_switches;
    if (ks.global.active || ks.daily.active) {
      strip.append(h("span", { class: "pill" }, h("span", { class: "dot critical" }), `⛔ Kill switch ${ks.global.active ? "GLOBAL" : "DIARIO"}`));
    }
    strip.append(h("span", { class: "pill" }, `Riesgo ${pct(Math.max(ov.risk_used.exposure, ov.risk_used.daily_loss) * 100, 0)}`));
  } catch { /* shown by views */ }
}

// ------------------------------------------------------------------ router
async function route() {
  if (!authed) {
    try {
      const me = await api.get("/auth/me");
      setCsrf(me.csrf);
      authed = true;
      document.getElementById("logout").hidden = false;
    } catch { showLogin(); return; }
  }
  clearInterval(refreshTimer);
  clear(document.getElementById("modal-root"));
  const [name, ...params] = (location.hash.replace(/^#\//, "") || "overview").split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active",
    a.getAttribute("href") === `#/${name === "wallet" ? "wallets" : name}`));
  const loader = VIEWS[name] || VIEWS.overview;
  const mod = await loader();
  const container = clear(view);
  const render = async () => {
    try { await mod.render(container, params.map(decodeURIComponent)); }
    catch (e) { if (e.status !== 401) { clear(container).appendChild(h("div", { class: "banner critical" }, `Error: ${e.message}`)); } }
  };
  await render();
  refreshStatus();
  if (mod.refreshSeconds) {
    refreshTimer = setInterval(() => { if (!document.getElementById("modal-root").firstChild && !document.hidden) { (mod.refresh || render)(); refreshStatus(); } }, mod.refreshSeconds * 1000);
  }
  view.focus({ preventScroll: true });
}
window.addEventListener("hashchange", route);
window.addEventListener("error", (e) => toast(`Error: ${e.message}`, true));
route();
