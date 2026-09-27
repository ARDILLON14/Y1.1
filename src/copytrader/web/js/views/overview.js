import { api } from "../api.js";
import { lineChart } from "../charts.js";
import { h, usd, pct, compactUsd, tile, meter, signClass, ago, severity, num, axisUsd, signalStatus } from "../dom.js";

export const refreshSeconds = 15;
let range = 30;

export async function render(root) {
  const [ov, eq, alerts] = await Promise.all([
    api.get("/overview"), api.get(`/equity?days=${range}`), api.get("/alerts?limit=6"),
  ]);
  const b = ov.book;
  const m = ov.mode;
  root.replaceChildren();
  if (ov.kill_switches.global.active || ov.kill_switches.daily.active) {
    const k = ov.kill_switches.global.active ? ov.kill_switches.global : ov.kill_switches.daily;
    root.append(h("div", { class: "banner critical" }, "⛔ ", h("div", {},
      h("strong", {}, `Kill switch ${ov.kill_switches.global.active ? "GLOBAL" : "DIARIO"} activo`),
      h("div", { class: "secondary" }, `${k.reason || ""} · no se abren nuevas posiciones (las salidas siguen funcionando).`))));
  }
  if (m.level >= 4 && !m.live_allowed) {
    root.append(h("div", { class: "banner warning" }, "! ", h("div", {}, h("strong", {}, "Nivel real sin armar"),
      h("div", { class: "secondary" }, `${m.live_block_reason}. Las entradas se simulan en PAPER.`))));
  }
  if (ov.providers_mode === "simulated") {
    root.append(h("div", { class: "banner info" }, "ℹ ", h("div", {}, h("strong", {}, "Modo demostración"),
      h("div", { class: "secondary" }, "Todos los datos provienen del mercado simulado. Configura providers.mode = live para datos reales."))));
  }

  const dayPnl = b.daily_pnl_usd;
  root.append(h("div", { class: "grid cols-2" },
    h("div", { class: "card hero" },
      h("div", { class: "label secondary" }, `Capital (${b.mode.toUpperCase()})`),
      h("div", { class: "value" }, usd(b.equity_usd)),
      h("div", { class: `delta ${signClass(b.equity_usd - b.capital_usd)}` },
        `${usd(b.equity_usd - b.capital_usd)} (${pct(b.roi_pct, 2, true)}) desde el inicio · hoy ${usd(dayPnl)}`),
      h("div", { class: "section" },
        meter("Exposición utilizada", ov.risk_used.exposure, `${usd(b.exposure_usd, 0)} / ${pct(ov.limits.max_total_exposure_pct, 0)}`),
        meter("Posiciones", ov.risk_used.positions, `${b.open_positions} / ${ov.limits.max_open_positions}`),
        meter("Pérdida diaria", ov.risk_used.daily_loss, `${pct(b.daily_loss_pct, 2)} / ${pct(ov.limits.max_daily_loss_pct, 1)}`))),
    equityCard(eq)));

  root.append(h("div", { class: "tiles section" },
    tile("PnL realizado", usd(b.realized_pnl_usd), null, signClass(b.realized_pnl_usd)),
    tile("PnL no realizado", usd(b.unrealized_pnl_usd), null, signClass(b.unrealized_pnl_usd)),
    tile("ROI", pct(b.roi_pct, 2, true), `sobre ${compactUsd(b.capital_usd)}`, signClass(b.roi_pct)),
    tile("Drawdown", pct(b.drawdown_pct, 2), "desde el máximo"),
    tile("Pérdida semanal", pct(b.weekly_loss_pct, 2), `mensual ${pct(b.monthly_loss_pct, 2)}`),
    tile("Pérdidas seguidas", String(b.consecutive_losses)),
  ));

  const ws = ov.wallets;
  const s24 = ov.signals_24h || {};
  root.append(h("div", { class: "grid cols-3 section" },
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Wallets"), h("a", { href: "#/wallets" }, "Ver todas →")),
      h("dl", { class: "kv" },
        h("dt", {}, "Seguidas"), h("dd", {}, String(ws.total)),
        h("dt", {}, "Seleccionadas (copia)"), h("dd", {}, String(ws.selected)),
        h("dt", {}, "✓ Activas"), h("dd", {}, String(ws.by_status.active || 0)),
        h("dt", {}, "! En observación"), h("dd", {}, String(ws.by_status.observe || 0)),
        h("dt", {}, "⛔ Bloqueadas"), h("dd", {}, String(ws.by_status.blocked || 0))),
      ov.last_cycle ? h("p", { class: "muted small" }, `Último análisis: ${num(ov.last_cycle.duration_seconds, 1)} s`) : null),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Señales 24 h"), h("a", { href: "#/signals" }, "Ver →")),
      h("dl", { class: "kv" }, Object.keys(s24).length ? Object.entries(s24).flatMap(([k, v]) => [h("dt", {}, signalStatus(k)), h("dd", {}, String(v))])
        : [h("dt", {}, "Sin señales"), h("dd", {}, "")])),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Alertas recientes"), h("a", { href: "#/alerts" }, "Ver →")),
      alerts.length ? alerts.map((a) => h("div", { class: "row small", style: { marginBottom: "8px" } },
        severity(a.severity), h("span", { class: "spacer" }, a.title), h("span", { class: "muted nowrap" }, ago(a.ts))))
        : h("div", { class: "empty" }, "Sin alertas"))));
}

function equityCard(eq) {
  const chart = h("div");
  const card = h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Curva de capital"),
      h("div", { class: "segmented" }, [1, 7, 30, 90].map((d) => h("button", {
        type: "button", class: d === range ? "active" : "",
        onclick: () => { range = d; window.dispatchEvent(new HashChangeEvent("hashchange")); },
      }, d === 1 ? "24h" : `${d}d`)))),
    chart);
  queueMicrotask(() => lineChart(chart, [{
    name: "Capital", color: "var(--series-1)",
    points: eq.points.map((p) => ({ x: new Date(p.ts), y: p.equity })),
  }], { yFormat: axisUsd }));
  return card;
}
