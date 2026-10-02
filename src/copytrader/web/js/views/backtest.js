import { api } from "../api.js";
import { lineChart } from "../charts.js";
import { h, table, pct, num, usd, dt, toast, frac, axisUsd, clear, fill } from "../dom.js";

export const refreshSeconds = 10;
let selected = null;
let lastRoot = null;
let running = false;

// Only auto-refresh while a backtest is running (keeps the form intact otherwise).
export function refresh() { if (running && lastRoot) render(lastRoot); }

export async function render(root) {
  const runs = await api.get("/backtest");
  lastRoot = root;
  running = runs.some((r) => r.status === "running");
  const f = {
    train_days: h("input", { type: "number", min: "1", max: "365", placeholder: "30" }),
    test_days: h("input", { type: "number", min: "1", max: "90", placeholder: "7" }),
    top_n: h("input", { type: "number", min: "1", max: "100", placeholder: "config" }),
    latency_seconds: h("input", { type: "number", min: "0", step: "0.5", placeholder: "3" }),
    entry_slippage_pct: h("input", { type: "number", min: "0", step: "0.1", placeholder: "2" }),
    exit_mode: h("select", {}, h("option", { value: "" }, "Config"), h("option", { value: "mirror" }, "Espejo"), h("option", { value: "protected" }, "Protegido"), h("option", { value: "smart" }, "Inteligente")),
  };
  const variants = [1, 2].map((i) => ({
    name: h("input", { type: "text", maxlength: "40", placeholder: `Variante ${i}` }),
    patch: h("textarea", { class: "mono small", rows: "3", placeholder: i === 1
      ? '{"risk": {"max_slippage_pct": 5}}'
      : '{"exits": {"default_mode": "smart"}, "selection": {"top_n": 5}}' }),
  }));
  const start = async (e) => {
    e.preventDefault();
    const body = {};
    for (const [k, el] of Object.entries(f)) if (el.value !== "") body[k] = el.tagName === "SELECT" ? el.value : Number(el.value);
    const vs = [];
    for (const [i, v] of variants.entries()) {
      if (!v.patch.value.trim()) continue;
      try { vs.push({ name: v.name.value || `Variante ${i + 1}`, patch: JSON.parse(v.patch.value) }); }
      catch { toast(`JSON inválido en la variante ${i + 1}`, true); return; }
    }
    if (vs.length) body.variants = vs;
    try { await api.post("/backtest", body); toast("Backtest iniciado"); render(root); } catch (ex) { toast(ex.message, true); }
  };
  const detail = h("div");
  fill(root,
    h("h1", {}, "Backtesting walk-forward"),
    h("p", { class: "secondary" }, "Cada ventana selecciona wallets usando solo datos anteriores (entrenamiento) y simula la copia en la ventana siguiente (evaluación). Se compara con copiar todas las wallets y con elegirlas solo por PnL."),
    h("form", { class: "card", onsubmit: start },
      h("div", { class: "form-grid" },
        h("label", { class: "field" }, "Días de entrenamiento", f.train_days), h("label", { class: "field" }, "Días de evaluación", f.test_days),
        h("label", { class: "field" }, "Top N", f.top_n), h("label", { class: "field" }, "Latencia (s)", f.latency_seconds),
        h("label", { class: "field" }, "Slippage entrada %", f.entry_slippage_pct), h("label", { class: "field" }, "Modo de salida", f.exit_mode)),
      h("details", { class: "section" },
        h("summary", {}, "Comparar configuraciones (opcional)"),
        h("p", { class: "muted small" }, "Hasta 2 variantes: cambios de configuración en JSON, con las mismas secciones y límites que la página "
          + "Configuración. Cada una se ejecuta sobre los mismos datos que la configuración actual."),
        h("div", { class: "form-grid" }, variants.flatMap((v, i) => [
          h("label", { class: "field" }, `Nombre ${i + 1}`, v.name),
          h("label", { class: "field" }, `Cambios ${i + 1} (JSON)`, v.patch)]))),
      h("div", { class: "row section" }, h("span", { class: "spacer" }), h("button", { class: "primary", type: "submit" }, "Ejecutar backtest"))),
    h("div", { class: "card section" }, h("h2", {}, "Ejecuciones"), table([
      { label: "#", num: true, render: (r) => r.id },
      { label: "Fecha", render: (r) => dt(r.created_at) },
      { label: "Estado", wrap: true, render: (r) => (r.status === "running" && r.progress
        ? h("span", {}, "running", h("span", { class: "muted small" }, ` · ${r.progress}`)) : r.status) },
      { label: "Precios", render: (r) => (r.prices
        ? h("span", { title: "Tokens con velas históricas / tokens comprados en el periodo" }, `${r.prices.tokens_with_prices}/${r.prices.tokens_traded}`)
        : h("span", { class: "muted" }, "—")) },
      { label: "ROI estrategia", num: true, render: (r) => pct(r.summary?.strategy?.roi_pct, 2, true) },
      { label: "ROI copiar todo", num: true, render: (r) => pct(r.summary?.copy_all?.roi_pct, 2, true) },
      { label: "ROI top PnL", num: true, render: (r) => pct(r.summary?.top_pnl?.roi_pct, 2, true) },
      { label: "DD estrategia", num: true, render: (r) => pct(r.summary?.strategy?.max_drawdown_pct, 2) },
      { label: "Error", wrap: true, render: (r) => r.error || "" },
    ], runs, { onRowClick: (r) => { selected = r.id; showRun(detail, r.id); }, empty: "Sin backtests" })),
    detail);
  if (selected || runs.find((r) => r.status === "done")) showRun(detail, selected || runs.find((r) => r.status === "done").id);
}

// How much of the evaluated period had real price paths (stops/TPs between trades).
function pricesLine(p) {
  const traded = p.tokens_traded || 0;
  const share = traded ? Math.round((100 * (p.tokens_with_prices || 0)) / traded) : 0;
  return h("p", { class: "secondary small" },
    `Precios históricos: velas de ${p.candle_minutes} min para ${p.tokens_with_prices} de ${traded} tokens (${share} %)`,
    p.fetched_now ? ` · ${p.fetched_now} descargados ahora` : "",
    p.failed ? ` · ${p.failed} descargas fallidas (se reintentarán)` : "",
    share < 50 ? h("span", { class: "muted" }, " · con poca cobertura, los stops y take profits entre operaciones apenas se simulan") : null);
}

const NAMES = { strategy: "Estrategia (scoring + riesgo)", copy_all: "Copiar todas", top_pnl: "Top por PnL (ingenuo)" };
const COLORS = { strategy: "var(--series-1)", copy_all: "var(--series-2)", top_pnl: "var(--series-3)" };

async function showRun(el, id) {
  const run = await api.get(`/backtest/${id}`);
  if (!run.results?.results) { clear(el); return; }
  const res = run.results.results;
  const chart = h("div");
  const variants = run.results.variants || [];
  const vchart = h("div");
  fill(el, variants.length ? h("div", { class: "card section" },
    h("h2", {}, "Comparación de configuraciones"),
    h("p", { class: "muted small" }, "Misma historia, mismas ventanas; solo cambia la configuración. Una diferencia pequeña o en pocas "
      + "operaciones no es concluyente: confírmala con más periodo antes de aplicarla."),
    table([
      { label: "Configuración", render: (v) => v.name },
      { label: "Cambios", wrap: true, render: (v) => h("span", { class: "mono small" }, Object.keys(v.patch).length ? JSON.stringify(v.patch) : "—") },
      { label: "ROI", num: true, render: (v) => pct(v.summary.roi_pct, 2, true) },
      { label: "Drawdown máx.", num: true, render: (v) => pct(v.summary.max_drawdown_pct, 2) },
      { label: "Operaciones", num: true, render: (v) => v.summary.n_trades ?? "—" },
      { label: "Win rate", num: true, render: (v) => frac(v.summary.win_rate, 1) },
      { label: "Profit factor", num: true, render: (v) => num(v.summary.profit_factor, 2) },
      { label: "Comisiones", num: true, render: (v) => usd(v.summary.fees_usd) },
    ], variants), vchart) : "", h("div", { class: "card section" },
    h("h2", {}, `Backtest #${id} · ${run.results.period.start.slice(0, 10)} → ${run.results.period.end.slice(0, 10)}`),
    run.results.prices ? pricesLine(run.results.prices) : null,
    table([
      { label: "Estrategia", render: (r) => NAMES[r.k] },
      { label: "Capital final", num: true, render: (r) => usd(r.final_equity_usd) },
      { label: "ROI", num: true, render: (r) => pct(r.roi_pct, 2, true) },
      { label: "Drawdown máx.", num: true, render: (r) => pct(r.max_drawdown_pct, 2) },
      { label: "Operaciones", num: true, render: (r) => r.n_trades },
      { label: "Win rate", num: true, render: (r) => frac(r.win_rate, 1) },
      { label: "Profit factor", num: true, render: (r) => num(r.profit_factor, 2) },
      { label: "Comisiones", num: true, render: (r) => usd(r.fees_usd) },
    ], Object.entries(res).map(([k, v]) => ({ k, ...v }))),
    h("h3", { class: "section" }, "Curvas de capital"), chart,
    h("h3", { class: "section" }, "Wallets seleccionadas por ventana"),
    table([
      { label: "Ventana", render: (w) => `${w.start.slice(0, 10)} → ${w.end.slice(0, 10)}` },
      { label: "Seleccionadas", wrap: true, render: (w) => w.selected.map((s) => `${s.label || s.wallet.slice(0, 6)} (${s.score})`).join(", ") || "ninguna" },
      { label: "Operaciones", num: true, render: (w) => w.trades },
    ], run.results.windows),
    h("ul", { class: "reasons section small" }, run.results.notes.map((n) => h("li", {}, n)))));
  if (variants.length) {
    lineChart(vchart, variants.map((v, i) => ({
      name: v.name, color: `var(--series-${i + 1})`, points: (v.equity_curve || []).map((p) => ({ x: new Date(p.ts), y: p.equity })),
    })), { area: false, yFormat: axisUsd });
  }
  lineChart(chart, Object.entries(res).map(([k, v]) => ({
    name: NAMES[k], color: COLORS[k], points: v.equity_curve.map((p) => ({ x: new Date(p.ts), y: p.equity })),
  })), { area: false, yFormat: axisUsd });
}
