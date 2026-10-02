import { api } from "../api.js";
import { lineChart, barChart, hbars } from "../charts.js";
import { h, table, addr, walletStatus, severity, usd, frac, num, pct, dt, ago, short, toast, signClass, price, tile, fill } from "../dom.js";

export async function render(root, [address]) {
  const d = await api.get(`/wallets/${encodeURIComponent(address)}`);
  const w = d.wallet;
  const all = d.metrics.all || {};
  const rerender = () => render(root, [address]);
  fill(root, 
    h("p", {}, h("a", { href: "#/wallets" }, "← Wallets")),
    header(w, rerender),
    h("div", { class: "grid cols-2 section" }, scoreCard(d.score, w), historyCard(d.score_history)),
    copyCard(all, d.signal_age_limit, d.probation),
    flagsCard(d.flags),
    metricsCard(d.metrics),
    h("div", { class: "grid cols-2 section" }, dailyCard(all), breakdownCard(all)),
    h("div", { class: "card section" }, h("div", { class: "card-head" }, h("h2", {}, `Últimas transacciones (${d.n_transactions} guardadas)`)),
      table([
        { label: "Fecha", render: (t) => dt(t.block_time) },
        { label: "Tipo", render: (t) => (t.side === "buy" ? "COMPRA" : "VENTA") },
        { label: "Token", render: (t) => h("span", { class: "mono" }, short(t.token_mint, 5)) },
        { label: "Cantidad", num: true, render: (t) => num(t.token_amount, 2) },
        { label: "Precio", num: true, render: (t) => price(t.price_usd) },
        { label: "Valor", num: true, render: (t) => usd(t.value_usd) },
        { label: "DEX", render: (t) => t.dex },
        { label: "Origen", render: (t) => t.source },
        { label: "Latencia", num: true, render: (t) => (t.detection_latency_ms ? `${num(t.detection_latency_ms / 1000, 2)} s` : "—") },
      ], d.transactions)),
    h("div", { class: "card section" }, h("div", { class: "card-head" }, h("h2", {}, "Mis posiciones copiadas de esta wallet")),
      table([
        { label: "Token", render: (p) => p.token_symbol || short(p.token_mint) },
        { label: "Modo", render: (p) => p.mode.toUpperCase() },
        { label: "Estado", render: (p) => p.status },
        { label: "Coste", num: true, render: (p) => usd(p.initial_cost_usd) },
        { label: "PnL realizado", num: true, render: (p) => h("span", { class: signClass(p.realized_pnl_usd) }, usd(p.realized_pnl_usd)) },
        { label: "Apertura", render: (p) => dt(p.opened_at) },
        { label: "Motivo cierre", wrap: true, render: (p) => p.close_reason || "—" },
      ], d.positions, { empty: "Ninguna" })),
  );
}

function header(w, rerender) {
  const exitSel = h("select", {
    onchange: async (e) => {
      try { await api.patch(`/wallets/${encodeURIComponent(w.address)}`, { exit_mode_override: e.target.value || null }); toast("Modo de salida actualizado"); }
      catch (ex) { toast(ex.message, true); }
    },
  }, [["", "Por defecto (config)"], ["mirror", "Espejo"], ["protected", "Protegido"], ["smart", "Inteligente"]].map(([v, l]) =>
    h("option", { value: v, selected: (w.exit_mode_override || "") === v ? "selected" : null }, l)));
  const notes = h("textarea", { value: w.notes || "", placeholder: "Notas privadas" });
  notes.style.minHeight = "60px";
  return h("div", { class: "card" },
    h("div", { class: "card-head" },
      h("div", {}, h("h1", {}, w.label || "Wallet"), addr(w.address)),
      h("div", { class: "row" }, walletStatus(w.status), w.selected ? h("span", { class: "pill" }, h("span", { class: "dot good" }), `Copiando · rank #${w.rank}`) : null,
        w.list_type !== "none" ? h("span", { class: "pill" }, w.list_type) : null)),
    h("ul", { class: "reasons" }, (w.status_reasons || []).map((r) => h("li", {}, r))),
    h("div", { class: "form-grid section" },
      h("label", { class: "field" }, "Modo de salida para esta wallet", exitSel),
      h("label", { class: "field" }, "Notas", notes)),
    h("div", { class: "row section" },
      h("button", { type: "button", onclick: async () => { await api.patch(`/wallets/${encodeURIComponent(w.address)}`, { notes: notes.value }); toast("Notas guardadas"); } }, "Guardar notas"),
      h("span", { class: "spacer" }),
      h("button", { class: "danger", type: "button", onclick: async () => {
        if (!confirm("¿Dejar de seguir esta wallet? Su historial se conserva.")) return;
        await api.del(`/wallets/${encodeURIComponent(w.address)}`); location.hash = "#/wallets";
      } }, "Dejar de seguir")));
}

function scoreCard(score, w) {
  if (!score) return h("div", { class: "card" }, h("h2", {}, "Score"), h("div", { class: "empty" }, "Pendiente de análisis"));
  const comps = Object.values(score.components || {}).filter((c) => c.weight > 0);
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Score"), h("span", { class: "muted small" }, `calculado ${ago(score.computed_at)}`)),
    h("div", { class: "row" }, h("div", { class: "hero" }, h("div", { class: "value" }, num(score.score, 1))),
      h("dl", { class: "kv", style: { marginLeft: "16px" } },
        h("dt", {}, "Histórico (ponderado)"), h("dd", {}, num(score.score_hist, 1)),
        h("dt", {}, "Reciente"), h("dd", {}, num(score.score_recent, 1)),
        h("dt", {}, "Confianza por muestra"), h("dd", {}, frac(score.confidence)))),
    h("h3", { class: "section" }, "Componentes (0–100)"),
    hbars(comps.map((c) => ({ label: `${c.label} ·${Math.round(c.weight * 100)}%`, value: c.value === null ? null : c.value * 100,
      title: JSON.stringify(c.input) })), { max: 100, format: (v) => num(v, 0) }),
    score.penalties?.length ? h("div", { class: "section" }, h("h3", {}, "Penalizaciones"),
      h("ul", { class: "reasons" }, score.penalties.map((p) => h("li", {}, `−${num(p.points, 1)} · ${p.reason}`)))) : null);
}

const LATENCY_SOURCE = { measured: "medida en tus copias", config: "configurada", default: "por defecto, aún sin copias medidas", backtest: "backtest" };

function probationText(p) {
  if (!p || !p.enabled) return "desactivado";
  const avg = p.mean_pct === null ? "sin datos" : `media ${pct(p.mean_pct, 1, true)}`;
  return p.graduated
    ? `superado (${p.closed_paper} copias paper, ${avg}): puede operar con dinero real`
    : `en prueba: ${p.closed_paper}/${p.required} copias paper cerradas (${avg}); con trading real se copia en paper`;
}

function copyCard(m, ageLimit, probation) {
  const r = m.replication || {};
  const fmtHold = (min) => (min === null || min === undefined ? "—" : min < 2 ? `${num(min * 60, 0)} s` : min < 120 ? `${num(min, 0)} min` : `${num(min / 60, 1)} h`);
  return h("div", { class: "card section" },
    h("div", { class: "card-head" }, h("h2", {}, "Si la copias (estimación)"),
      h("span", { class: "muted small" }, `${m.copy_n ?? 0} operaciones evaluadas`)),
    m.copy_expectancy_pct === null || m.copy_expectancy_pct === undefined
      ? h("div", { class: "empty" }, "Pendiente de análisis")
      : h("div", {},
        h("div", { class: "grid cols-4" },
          tile("Retorno por operación copiado", h("span", { class: signClass(m.copy_expectancy_pct) }, pct(m.copy_expectancy_pct, 2, true)),
            `el suyo: ${pct(m.expectancy_pct, 2, true)}`),
          tile("Coste de copiarla", pct(m.copy_cost_pct, 2), "retraso, impacto, slippage y comisiones"),
          tile("Win rate copiado", frac(m.copy_win_rate, 1), `el suyo: ${frac(m.win_rate, 1)}`),
          tile("Profit factor copiado", num(m.copy_profit_factor, 2), `el suyo: ${num(m.profit_factor, 2)}`)),
        h("dl", { class: "kv section" },
          h("dt", {}, "Tu latencia"), h("dd", {}, `${num(r.latency_seconds, 1)} s (${LATENCY_SOURCE[r.latency_source] || r.latency_source || "—"})`),
          h("dt", {}, "Tamaño supuesto"), h("dd", {}, usd(r.size_usd)),
          h("dt", {}, "Comisiones fijas / slippage"), h("dd", {}, `${pct(r.fixed_cost_pct, 2)} ida y vuelta · ${pct(r.slippage_pct, 2)} por lado`),
          h("dt", {}, "Holding mediano de la wallet"), h("dd", {}, fmtHold(m.median_holding_minutes)),
          h("dt", {}, "Retraso máximo de sus señales"), h("dd", {}, ageLimit ? `${num(ageLimit.seconds, 1)} s (${ageLimit.reason})` : "—"),
          h("dt", {}, "Copias reales cerradas"), h("dd", {}, m.realized_copy_n
            ? `${m.realized_copy_n} · media ${pct(m.realized_copy_mean_pct, 2, true)} `
              + (m.realized_copy_ub_pct === null || m.realized_copy_ub_pct === undefined
                ? "(muestra aún pequeña)" : `(escenario optimista ${pct(m.realized_copy_ub_pct, 2, true)})`)
            : "aún ninguna"),
          h("dt", {}, "Ventaja copiable efectiva"), h("dd", {}, h("span", { class: signClass(m.effective_copy_expectancy_pct) },
            pct(m.effective_copy_expectancy_pct, 2, true)), m.realized_copy_n ? " (estimación corregida con tus copias reales)" : " (solo estimación)"),
          h("dt", {}, "Periodo de prueba"), h("dd", {}, probationText(probation))),
        h("p", { class: "muted small section" },
          "Estima qué habrías ganado copiando cada operación cerrada: llegas tarde, la compra de la wallet ya movió el precio, pagas tu impacto, "
          + "slippage y comisiones. Si sale negativo con muestra suficiente, la wallet pasa a OBSERVAR.")));
}

function historyCard(history) {
  const chart = h("div");
  queueMicrotask(() => lineChart(chart, [{ name: "Score", color: "var(--series-1)", points: history.map((p) => ({ x: new Date(p.ts), y: p.score })) }],
    { yFormat: (v) => num(v, 1) }));
  return h("div", { class: "card" }, h("h2", {}, "Evolución del score"), chart);
}

function flagsCard(flags) {
  return h("div", { class: "card section" }, h("h2", {}, "Detección de comportamiento"),
    flags.length ? table([
      { label: "Severidad", render: (f) => severity(f.severity) },
      { label: "Código", render: (f) => h("span", { class: "mono" }, f.code) },
      { label: "Detalle", wrap: true, render: (f) => f.message },
      { label: "Desde", render: (f) => ago(f.first_seen_at) },
    ], flags) : h("div", { class: "empty" }, "Sin comportamientos sospechosos detectados"));
}

const ROWS = [
  ["Operaciones cerradas", "n_closed_trades", (v) => v ?? "—"],
  ["PnL realizado", "realized_pnl_usd", (v) => usd(v)],
  ["PnL no realizado", "unrealized_pnl_usd", (v) => usd(v)],
  ["ROI", "roi_pct", (v) => pct(v, 2, true)],
  ["Win rate", "win_rate", (v) => frac(v, 1)],
  ["Win rate (límite inferior)", "win_rate_lb", (v) => frac(v, 1)],
  ["Ganancia media", "avg_win_pct", (v) => pct(v, 1)],
  ["Pérdida media", "avg_loss_pct", (v) => pct(v, 1)],
  ["Profit factor", "profit_factor", (v) => num(v, 2)],
  ["Profit factor (contraído)", "profit_factor_shrunk", (v) => num(v, 2)],
  ["Expectativa por operación", "expectancy_pct", (v) => pct(v, 2, true)],
  ["Expectativa (límite inferior)", "expectancy_lb_pct", (v) => pct(v, 2, true)],
  ["Mediana de retorno", "median_return_pct", (v) => pct(v, 2, true)],
  ["Drawdown máximo", "max_drawdown_pct", (v) => pct(v, 1)],
  ["Permanencia media", "avg_holding_minutes", (v) => (v === null || v === undefined ? "—" : `${num(v, 0)} min`)],
  ["Operaciones por día", "trades_per_day", (v) => num(v, 2)],
  ["Tamaño medio", "avg_trade_size_usd", (v) => usd(v, 0)],
  ["Pérdidas consecutivas máx.", "max_consecutive_losses", (v) => v ?? "—"],
  ["Días rentables", "profitable_days_frac", (v) => frac(v)],
  ["Semanas rentables", "profitable_weeks_frac", (v) => frac(v)],
  ["Replicables (duración suficiente)", "replicable_frac", (v) => frac(v)],
];

function metricsCard(m) {
  const cols = [["all", "Total"], ["decayed", "Ponderado por tiempo"], ["recent", "Últimas operaciones"]];
  return h("div", { class: "card section" }, h("h2", {}, "Métricas"),
    table([{ label: "Métrica", render: (r) => r[0] }, ...cols.map(([k, l]) => ({ label: l, num: true, render: (r) => r[2](m[k]?.[r[1]]) }))], ROWS));
}

function dailyCard(all) {
  const chart = h("div");
  const daily = all.period_pnl?.daily || [];
  queueMicrotask(() => barChart(chart, daily.map((p) => ({ label: p.period, value: p.pnl_usd, extra: `${p.n} operaciones` })),
    { yFormat: (v, step) => (step === undefined ? usd(v) : usd(v, 0)), labelFormat: (l) => l.slice(5) }));
  return h("div", { class: "card" }, h("h2", {}, "PnL diario (operaciones cerradas)"), chart);
}

function bucketTable(obj, label) {
  const rows = Object.entries(obj || {}).map(([k, v]) => ({ k, ...v }));
  return table([
    { label, render: (r) => r.k },
    { label: "Ops", num: true, render: (r) => r.n },
    { label: "Win rate", num: true, render: (r) => frac(r.win_rate) },
    { label: "Retorno medio", num: true, render: (r) => pct(r.avg_return_pct, 1, true) },
    { label: "PnL", num: true, render: (r) => h("span", { class: signClass(r.pnl_usd) }, usd(r.pnl_usd, 0)) },
  ], rows);
}

function breakdownCard(all) {
  const fw = Object.entries(all.forward_win_rates || {});
  const conc = all.concentration || {};
  const out = all.outliers || {};
  return h("div", { class: "card" }, h("h2", {}, "Rendimiento por segmentos"),
    h("h3", {}, "Por categoría de token"), bucketTable(all.by_category, "Categoría"),
    h("h3", { class: "section" }, "Rápidas vs largas"), bucketTable(all.fast_vs_slow, "Tipo"),
    h("h3", { class: "section" }, "Por duración"), bucketTable(all.by_holding, "Duración"),
    h("h3", { class: "section" }, "Por condición de mercado (SOL 24 h)"), bucketTable(all.by_regime, "Régimen"),
    h("h3", { class: "section" }, "Concentración y dependencia"),
    h("dl", { class: "kv" },
      h("dt", {}, "Mejor operación / beneficio"), h("dd", {}, frac(conc.top_trade_share)),
      h("dt", {}, "Top 3 / beneficio"), h("dd", {}, frac(conc.top3_share)),
      h("dt", {}, "HHI por token"), h("dd", {}, num(conc.token_hhi, 3)),
      h("dt", {}, "PnL sin la mejor operación"), h("dd", {}, usd(out.pnl_without_top_trade)),
      h("dt", {}, "Beneficio de operaciones ≥10x"), h("dd", {}, frac(out.outlier_pnl_share))),
    fw.length ? h("div", { class: "section" }, h("h3", {}, "% compras con precio más alto después de…"),
      h("dl", { class: "kv" }, fw.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, frac(v))]))) : null);
}
