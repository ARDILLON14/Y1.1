import { api } from "../api.js";
import { h, table, addr, signalStatus, usd, num, dt, short, modal, price } from "../dom.js";

export const refreshSeconds = 10;
let status = "";
let action = "";

export async function render(root) {
  const q = [`limit=300`, status && `status=${status}`, action && `action=${action}`].filter(Boolean).join("&");
  const rows = await api.get(`/signals?${q}`);
  const seg = (values, cur, set) => h("div", { class: "segmented" }, values.map(([v, l]) =>
    h("button", { type: "button", class: v === cur ? "active" : "", onclick: () => { set(v); render(root); } }, l)));
  root.replaceChildren(
    h("div", { class: "card-head" }, h("h1", {}, "Señales y decisiones"),
      h("div", { class: "row" },
        seg([["", "Todas"], ["copy", "Copia"], ["exit", "Salida"], ["alert", "Alerta"]], action, (v) => { action = v; }),
        seg([["", "Todos"], ["executed", "Ejecutadas"], ["rejected", "Rechazadas"], ["expired", "Caducadas"], ["failed", "Fallidas"]], status, (v) => { status = v; }))),
    h("div", { class: "card" }, table([
      { label: "Detectada", render: (r) => h("span", { class: "nowrap" }, dt(r.detected_at)) },
      { label: "Wallet", render: (r) => addr(r.wallet, r.wallet_label) },
      { label: "Token", render: (r) => r.token_symbol || h("span", { class: "mono" }, short(r.token_mint)) },
      { label: "Tipo", render: (r) => (r.side === "buy" ? "COMPRA" : "VENTA") },
      { label: "Acción", render: (r) => r.action },
      { label: "Resultado", render: (r) => signalStatus(r.status) },
      { label: "Precio", num: true, render: (r) => price(r.source_price_usd) },
      { label: "Valor origen", num: true, render: (r) => usd(r.source_value_usd, 0) },
      { label: "Latencia", num: true, render: (r) => (r.detection_latency_ms === null ? "—" : `${num(r.detection_latency_ms / 1000, 2)} s`) },
      { label: "Motivo", wrap: true, render: (r) => r.reason || "—" },
    ], rows, { onRowClick: (r) => showSignal(r.id), empty: "Sin señales todavía" })));
}

async function showSignal(id) {
  const s = await api.get(`/signals/${id}`);
  const checks = s.decision?.checks || [];
  const sizing = s.decision?.sizing?.steps || [];
  modal(`Señal #${s.id} · ${s.side === "buy" ? "COMPRA" : "VENTA"} ${s.token_symbol || short(s.token_mint)}`, h("div", {},
    h("div", { class: "row" }, signalStatus(s.status), h("span", { class: "muted small" }, `traza ${s.trace_id} · modo ${s.mode || "—"} · nivel ${s.operating_level}`)),
    s.explanation ? h("pre", { class: "explain section" }, s.explanation) : h("p", { class: "section" }, s.reason || ""),
    checks.length ? h("div", { class: "section" }, h("h3", {}, "Comprobaciones"), table([
      { label: "", render: (c) => (c.passed ? "✓" : c.critical ? "✗" : "!") },
      { label: "Comprobación", render: (c) => c.label },
      { label: "Valor", num: true, render: (c) => (c.value === null || c.value === undefined ? "—" : String(c.value)) },
      { label: "Límite", num: true, render: (c) => (c.limit === null || c.limit === undefined ? "—" : String(c.limit)) },
      { label: "Detalle", wrap: true, render: (c) => c.message || "" },
    ], checks)) : null,
    sizing.length ? h("div", { class: "section" }, h("h3", {}, "Cálculo del tamaño"), table([
      { label: "Paso", render: (st) => st.label },
      { label: "Factor", num: true, render: (st) => (st.factor === null ? "—" : num(st.factor, 3)) },
      { label: "Tamaño", num: true, render: (st) => usd(st.size_usd) },
    ], sizing)) : null,
    h("p", { class: "small muted section" }, "Tx origen: ", h("span", { class: "mono" }, s.source_signature))));
}
