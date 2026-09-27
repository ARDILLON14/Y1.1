// Safe DOM builder: text is always set with textContent (no innerHTML with data).
export function h(tag, attrs = {}, ...children) {
  const svg = ["svg", "path", "line", "circle", "rect", "text", "g", "polyline"].includes(tag);
  const el = svg ? document.createElementNS("http://www.w3.org/2000/svg", tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "value" && !svg) el.value = v;
    else if (k === "checked" && !svg) el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

// ---------------------------------------------------------------- formatting
const nf = (d) => new Intl.NumberFormat("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d });
export function usd(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a > 0 && a < 0.01) return "$" + Number(v).toPrecision(4);
  return (v < 0 ? "-$" : "$") + nf(digits).format(a);
}
export function compactUsd(v) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v), s = v < 0 ? "-$" : "$";
  if (a >= 1e9) return s + nf(1).format(a / 1e9) + "B";
  if (a >= 1e6) return s + nf(1).format(a / 1e6) + "M";
  if (a >= 1e3) return s + nf(1).format(a / 1e3) + "K";
  return s + nf(a < 10 ? 2 : 0).format(a);
}
// Axis labels: precision follows the tick step so neighbouring ticks never read the same.
export function axisUsd(v, step) {
  if (step === undefined) return usd(v);
  if (step >= 1000) return compactUsd(v);
  return usd(v, step >= 1 && Number.isInteger(step) ? 0 : step >= 0.1 ? 1 : 2);
}
export function price(v) {
  if (v === null || v === undefined) return "—";
  if (Math.abs(v) >= 1) return "$" + nf(4).format(v);
  return "$" + Number(v).toPrecision(4);
}
export function pct(v, digits = 1, signed = false) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return (signed && v > 0 ? "+" : "") + nf(digits).format(v) + "%";
}
export function frac(v, digits = 0) { return v === null || v === undefined ? "—" : pct(v * 100, digits); }
export function num(v, digits = 2) { return v === null || v === undefined ? "—" : nf(digits).format(v); }
export function short(addr, n = 4) { return !addr ? "—" : addr.length <= 2 * n + 1 ? addr : `${addr.slice(0, n)}…${addr.slice(-n)}`; }
export function ago(iso) {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `hace ${Math.max(0, Math.round(s))}s`;
  if (s < 3600) return `hace ${Math.round(s / 60)} min`;
  if (s < 86400) return `hace ${Math.round(s / 3600)} h`;
  return `hace ${Math.round(s / 86400)} d`;
}
export function dt(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
export function signClass(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }

// ---------------------------------------------------------------- UI bits
const STATUS = {
  active: ["good", "ACTIVA", "✓"], observe: ["warning", "OBSERVAR", "!"], blocked: ["critical", "BLOQUEADA", "⛔"],
};
export function walletStatus(status) {
  const [cls, label, icon] = STATUS[status] || ["", status, ""];
  return h("span", { class: "badge", title: label }, h("span", { class: `dot ${cls}` }), `${icon} ${label}`);
}
const SEV = { info: ["accent", "Info", "ℹ"], warning: ["warning", "Aviso", "!"], critical: ["critical", "Crítico", "⛔"] };
export function severity(s) {
  const [cls, label, icon] = SEV[s] || ["", s, ""];
  return h("span", { class: "badge" }, h("span", { class: `dot ${cls}` }), `${icon} ${label}`);
}
const SIG = {
  executed: ["good", "EJECUTADA"], approved: ["accent", "PENDIENTE"], rejected: ["critical", "RECHAZADA"],
  expired: ["serious", "CADUCADA"], failed: ["critical", "FALLIDA"], alerted: ["accent", "ALERTA"],
  ignored: ["", "IGNORADA"], detected: ["warning", "EN PROCESO"],
};
export function signalStatus(s) {
  const [cls, label] = SIG[s] || ["", s];
  return h("span", { class: "badge" }, h("span", { class: `dot ${cls}` }), label);
}
export function copyButton(text) {
  return h("button", {
    class: "copy-btn", type: "button", title: "Copiar", "aria-label": "Copiar",
    onclick: (e) => { e.stopPropagation(); navigator.clipboard?.writeText(text); toast("Copiado"); },
  }, "⧉");
}
export function addr(address, label) {
  return h("span", { class: "nowrap", title: address }, label ? h("span", {}, label, " ") : null,
    h("span", { class: "mono muted" }, short(address)), copyButton(address));
}

let toastTimer = null;
export function toast(message, error = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = "toast" + (error ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, error ? 6000 : 2500);
}

export function modal(title, body, { narrow = false } = {}) {
  const root = document.getElementById("modal-root");
  const close = () => { clear(root); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  const box = h("div", { class: "modal" + (narrow ? " narrow" : ""), role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div", { class: "card-head" }, h("h2", {}, title), h("button", { class: "ghost", type: "button", onclick: close, "aria-label": "Cerrar" }, "✕")),
    body);
  const backdrop = h("div", { class: "modal-backdrop", onclick: (e) => { if (e.target === backdrop) close(); } }, box);
  clear(root).appendChild(backdrop);
  box.querySelector("input,button.primary")?.focus();
  return close;
}

// Ask for the password (re-authentication for sensitive actions).
export function askPassword(title, description) {
  return new Promise((resolve) => {
    const pw = h("input", { type: "password", autocomplete: "current-password", required: true });
    const totp = h("input", { type: "text", inputmode: "numeric", placeholder: "Código TOTP (si está activado)", autocomplete: "one-time-code" });
    let close;
    const form = h("form", { onsubmit: (e) => { e.preventDefault(); close(); resolve({ password: pw.value, totp: totp.value || null }); } },
      h("p", { class: "secondary" }, description),
      h("label", { class: "field" }, "Contraseña", pw),
      h("label", { class: "field section" }, "TOTP", totp),
      h("div", { class: "row section" }, h("span", { class: "spacer" }),
        h("button", { type: "button", onclick: () => { close(); resolve(null); } }, "Cancelar"),
        h("button", { class: "primary", type: "submit" }, "Confirmar")));
    close = modal(title, form, { narrow: true });
    pw.focus();
  });
}

export function table(columns, rows, { onRowClick = null, empty = "Sin datos" } = {}) {
  if (!rows.length) return h("div", { class: "empty" }, empty);
  return h("div", { class: "table-wrap" }, h("table", {},
    h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : "" }, c.label)))),
    h("tbody", {}, rows.map((r) => h("tr", {
      class: onRowClick ? "clickable" : "", onclick: onRowClick ? () => onRowClick(r) : null,
    }, columns.map((c) => h("td", { class: [c.num ? "num" : "", c.wrap ? "wrap" : ""].join(" ") }, c.render(r))))))));
}

export function tile(label, value, sub = null, cls = "") {
  return h("div", { class: "card tile" }, h("div", { class: "label" }, label),
    h("div", { class: `value ${cls}` }, value), sub ? h("div", { class: "sub" }, sub) : null);
}

export function meter(label, ratio, detail) {
  const r = Math.max(0, Math.min(1, ratio || 0));
  const fill = h("div", { class: "fill" + (r >= 0.9 ? " critical" : r >= 0.7 ? " warning" : "") });
  fill.style.width = `${(r * 100).toFixed(1)}%`;
  return h("div", { class: "meter" },
    h("div", { class: "meter-row" }, h("span", {}, label), h("span", {}, detail)),
    h("div", { class: "track", role: "meter", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(Math.round(r * 100)), "aria-label": label }, fill));
}
