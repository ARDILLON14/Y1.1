import { api } from "../api.js";
import { h, table, addr, usd, pct, frac, signClass, tile } from "../dom.js";

export const refreshSeconds = 60;
let mode = "paper";
let days = 30;

const HORIZON_LABELS = { 5: "5 min", 60: "1 h", 1440: "24 h" };
const TRIGGERS = {
  source_sell: "Venta de la wallet origen",
  source_exited: "El origen ya había vendido",
  stop_loss: "Stop loss",
  emergency_stop: "Stop de emergencia",
  trailing_stop: "Trailing stop",
  take_profit: "Take profit",
  max_hold: "Tiempo máximo",
  manual: "Cierre manual",
  kill_switch: "Kill switch",
  liquidity_drop: "Caída de liquidez",
  wallets_selling: "Venden varias wallets",
};

// Total entry cost vs the source's price: arriving late (signal → quote) compounded with execution (quote → fill).
function entryCost(c) {
  if (c.entry_late_pct === null && c.entry_execution_pct === null) return null;
  return ((1 + (c.entry_late_pct || 0) / 100) * (1 + (c.entry_execution_pct || 0) / 100) - 1) * 100;
}

function horizonLabel(k) { return HORIZON_LABELS[k] || (Number(k) >= 60 ? `${Number(k) / 60} h` : `${k} min`); }

function signedPct(v, digits = 2) { return h("span", { class: signClass(v) }, pct(v, digits, true)); }

function verdictBadge(v) {
  if (!v) return "—";
  const cls = v.startsWith("Protege") ? "good" : v.startsWith("Revisar") ? "warning" : "";
  return cls ? h("span", { class: "badge" }, h("span", { class: `dot ${cls}` }), v) : h("span", { class: "muted" }, v);
}

export async function render(root) {
  const r = await api.get(`/analytics?mode=${mode}&days=${days}`);
  const seg = (values, cur, set) => h("div", { class: "segmented" }, values.map(([v, l]) =>
    h("button", { type: "button", class: v === cur ? "active" : "", onclick: () => { set(v); render(root); } }, l)));
  const c = r.costs;
  root.replaceChildren(
    h("div", { class: "card-head" }, h("h1", {}, "Análisis de resultados"),
      h("div", { class: "row" },
        seg([["paper", "Paper"], ["live", "Real"]], mode, (v) => { mode = v; }),
        seg([[7, "7 d"], [30, "30 d"], [90, "90 d"]], days, (v) => { days = v; }))),
    h("div", { class: "grid cols-4" },
      tile("Resultado bruto", h("span", { class: signClass(c.gross_pnl_usd) }, usd(c.gross_pnl_usd)),
        c.positions === 1 ? "1 posición cerrada" : `${c.positions} posiciones cerradas`),
      tile("Comisiones de red", usd(c.fees_usd), c.fees_share_of_gross_pct === null ? "—" : `${pct(c.fees_share_of_gross_pct, 1)} del resultado bruto`),
      tile("Resultado neto", h("span", { class: signClass(c.net_pnl_usd) }, usd(c.net_pnl_usd))),
      tile("Coste de entrar", pct(entryCost(c), 2, true),
        `llegar tarde ${pct(c.entry_late_pct, 2, true)} (señal → cotización) · ejecución ${pct(c.entry_execution_pct, 2, true)}`)),
    filtersCard(r),
    h("div", { class: "card section" }, h("h2", {}, "Resultado por wallet copiada"),
      h("p", { class: "muted small" }, "Real (tus posiciones cerradas) frente a lo que estimaba el modelo de copia y a lo que gana la propia wallet."),
      table([
        { label: "Wallet", render: (w) => (w.wallet ? addr(w.wallet, w.label) : "—") },
        { label: "Posiciones", num: true, render: (w) => w.n },
        { label: "PnL", num: true, render: (w) => h("span", { class: signClass(w.pnl_usd) }, usd(w.pnl_usd)) },
        { label: "Win rate", num: true, render: (w) => frac(w.win_rate) },
        { label: "Retorno medio real", num: true, render: (w) => signedPct(w.avg_return_pct) },
        { label: "Estimado al copiar", num: true, render: (w) => signedPct(w.estimated_copy_pct) },
        { label: "Retorno de la wallet", num: true, render: (w) => signedPct(w.wallet_expectancy_pct) },
        { label: "Comisiones", num: true, render: (w) => usd(w.fees_usd) },
      ], r.wallets, { empty: "Sin posiciones cerradas en el periodo" })),
    h("div", { class: "grid cols-2 section" },
      h("div", { class: "card" }, h("h2", {}, "Por motivo de salida"),
        table([
          { label: "Motivo", render: (e) => TRIGGERS[e.trigger] || e.trigger },
          { label: "Salidas", num: true, render: (e) => e.n },
          { label: "PnL", num: true, render: (e) => h("span", { class: signClass(e.pnl_usd) }, usd(e.pnl_usd)) },
          { label: "Con beneficio", num: true, render: (e) => frac(e.win_rate) },
        ], r.exits, { empty: "Sin salidas en el periodo" })),
      h("div", { class: "card" }, h("h2", {}, "Por retraso de entrada"),
        h("p", { class: "muted small" }, "Desde la operación de la wallet hasta tu ejecución."),
        table([
          { label: "Retraso", render: (d) => d.bucket },
          { label: "Entradas", num: true, render: (d) => d.entries },
          { label: "Cerradas", num: true, render: (d) => d.closed },
          { label: "Retorno medio", num: true, render: (d) => signedPct(d.avg_return_pct) },
          { label: "Win rate", num: true, render: (d) => frac(d.win_rate) },
        ], r.delay))),
  );
}

function filtersCard(r) {
  const hs = r.horizons;
  const cols = [
    { label: "Motivo", wrap: true, render: (f) => (f.check === "executed" ? h("strong", {}, f.label) : f.label) },
    { label: "Señales", num: true, render: (f) => f.n },
    ...hs.map((k) => ({
      label: `Precio a ${horizonLabel(k)}`,
      num: true,
      render: (f) => {
        const s = f.horizons[k] || {};
        if (!s.n) return h("span", { class: "muted" }, "—");
        return h("span", { title: `${s.n} medidas · mediana ${pct(s.median_pct, 2, true)} · subieron ${frac(s.up_frac)}` },
          signedPct(s.mean_pct), h("span", { class: "muted small" }, ` · ${frac(s.up_frac)} ↑`));
      },
    })),
    { label: "Lectura", render: (f) => verdictBadge(f.verdict) },
  ];
  return h("div", { class: "card section" }, h("h2", {}, "¿Protegen los filtros o cuestan oportunidades?"),
    h("p", { class: "muted small" },
      "Qué hizo el precio del token tras cada decisión, frente al precio en el momento de decidir. Si lo que un filtro rechaza "
      + "sube más que lo que se ejecuta, ese filtro te está costando dinero; si baja, te protege. Retornos brutos del token, "
      + "sin comisiones ni reglas de salida."),
    table(cols, r.filters, { empty: "Aún no hay decisiones medidas: los primeros resultados aparecen 5 minutos después de cada señal." }));
}
