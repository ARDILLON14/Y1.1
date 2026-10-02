import { api } from "../api.js";
import { h, table, addr, signalStatus, usd, num, dt, short, modal, price, fill } from "../dom.js";

export const refreshSeconds = 10;
let status = "";
let action = "";

export async function render(root) {
  const q = [`limit=300`, status && `status=${status}`, action && `action=${action}`].filter(Boolean).join("&");
  const rows = await api.get(`/signals?${q}`);
  const seg = (values, cur, set) => h("div", { class: "segmented" }, values.map(([v, l]) =>
    h("button", { type: "button", class: v === cur ? "active" : "", onclick: () => { set(v); render(root); } }, l)));
  fill(root, 
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
    (s.events || []).length ? h("div", { class: "section" }, h("h3", {}, "Línea temporal"), table([
      { label: "Hora", render: (e) => h("span", { class: "nowrap mono small" }, timeOf(e.ts)) },
      { label: "Componente", render: (e) => e.component },
      { label: "Evento", render: (e) => EVENT_LABELS[e.event] || e.event },
      { label: "Detalle", wrap: true, render: (e) => eventDetail(e.data || {}) },
    ], s.events)) : null,
    h("p", { class: "small muted section" }, "Tx origen: ", h("span", { class: "mono" }, s.source_signature))));
}

const EVENT_LABELS = {
  signal_detected: "Señal detectada",
  decision_approved: "Decisión: aprobada",
  decision_rejected: "Decisión: rechazada",
  decision_expired: "Decisión: caducada",
  decision_failed: "Decisión: fallida",
  order_created: "Orden creada",
  order_quoted: "Cotizada",
  order_signed: "Firmada",
  order_submitted: "Enviada",
  order_confirmed: "Confirmada",
  order_failed: "Orden fallida",
  order_expired: "Orden caducada",
  fill_applied: "Fill aplicado",
  exit_triggered: "Salida disparada",
  position_closed: "Posición cerrada",
};

function timeOf(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${d.toLocaleTimeString("es-ES", { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
}

function eventDetail(d) {
  const parts = [];
  if (d.reason) parts.push(d.reason);
  if (d.trigger) parts.push(`disparador ${d.trigger}`);
  if (d.size_usd != null) parts.push(`tamaño ${usd(d.size_usd)}`);
  if (d.value_usd != null) parts.push(`valor ${usd(d.value_usd)}`);
  if (d.fill_price_usd != null) parts.push(`precio ${price(d.fill_price_usd)}`);
  if (d.slippage_bps != null) parts.push(`slippage ${num(d.slippage_bps / 100, 2)}%`);
  if (d.realized_pnl_usd != null) parts.push(`PnL ${usd(d.realized_pnl_usd)}`);
  if (d.detection_latency_ms != null) parts.push(`latencia ${num(d.detection_latency_ms / 1000, 2)} s`);
  if (d.ms != null) parts.push(`${num(d.ms, 0)} ms`);
  if (d.error) parts.push(`error: ${d.error}`);
  if (d.tx_signature) parts.push(`tx ${short(d.tx_signature, 6)}`);
  return parts.join(" · ") || "—";
}
