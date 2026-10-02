import { api } from "../api.js";
import { h, table, usd, pct, num, dt, meter, toast, askPassword, short, severity, fill } from "../dom.js";

export const refreshSeconds = 20;

export async function render(root) {
  const r = await api.get("/risk");
  const b = r.book, l = r.limits;
  const ks = r.kill_switches;
  const cap = Math.min(l.capital_usd, b.equity_usd) || l.capital_usd;
  const reason = h("input", { type: "text", value: "manual", maxlength: "300" });
  const flatten = h("input", { type: "checkbox" });
  const scope = h("select", {}, h("option", { value: "global" }, "Global"), h("option", { value: "daily" }, "Diario"));
  const act = async (action, sc) => {
    let body = { action, scope: sc, reason: reason.value, flatten: flatten.checked };
    if (action === "deactivate") {
      const cred = await askPassword(`Desactivar kill switch ${sc === "global" ? "global" : "diario"}`, "Volverán a abrirse posiciones automáticamente. Confirma con tu contraseña.");
      if (!cred) return;
      body = { ...body, ...cred };
    }
    if (action === "activate" && flatten.checked && !confirm("Se cerrarán TODAS las posiciones abiertas a mercado. ¿Continuar?")) return;
    try { await api.post("/risk/kill-switch", body); toast(action === "activate" ? "Kill switch activado" : "Kill switch desactivado"); render(root); }
    catch (ex) { toast(ex.message, true); }
  };
  fill(root,
    h("h1", {}, `Gestión de riesgo · libro ${r.mode.toUpperCase()} · nivel ${l.level}`),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Uso de límites"),
        meter("Exposición total", b.exposure_usd / (cap * l.max_total_exposure_pct / 100), `${usd(b.exposure_usd, 0)} / ${usd(cap * l.max_total_exposure_pct / 100, 0)}`),
        meter("Posiciones abiertas", (b.open_positions || 0) / l.max_open_positions, `${b.open_positions} / ${l.max_open_positions}`),
        meter("Pérdida diaria", b.daily_loss_pct / l.max_daily_loss_pct, `${pct(b.daily_loss_pct, 2)} / ${pct(l.max_daily_loss_pct, 1)}`),
        meter("Pérdida semanal", b.weekly_loss_pct / l.max_weekly_loss_pct, `${pct(b.weekly_loss_pct, 2)} / ${pct(l.max_weekly_loss_pct, 1)}`),
        meter("Pérdida mensual", b.monthly_loss_pct / l.max_monthly_loss_pct, `${pct(b.monthly_loss_pct, 2)} / ${pct(l.max_monthly_loss_pct, 1)}`),
        h("dl", { class: "kv section" },
          h("dt", {}, "Capital configurado"), h("dd", {}, usd(l.capital_usd)),
          h("dt", {}, "Equity actual"), h("dd", {}, usd(b.equity_usd)),
          h("dt", {}, "Máx. por operación"), h("dd", {}, `${usd(l.max_trade_usd)} (tope absoluto ${usd(l.hard_max_trade_usd)})`),
          h("dt", {}, "Mín. por operación"), h("dd", {}, usd(l.min_trade_usd)),
          h("dt", {}, "Riesgo por operación"), h("dd", {}, pct(l.max_risk_per_trade_pct, 2)),
          h("dt", {}, "Riesgo por wallet"), h("dd", {}, pct(l.max_risk_per_wallet_pct, 2)),
          h("dt", {}, "Exposición por token"), h("dd", {}, pct(l.max_token_exposure_pct, 1)),
          h("dt", {}, "Exposición alto riesgo"), h("dd", {}, pct(l.max_high_risk_exposure_pct, 1)),
          h("dt", {}, "Slippage máximo"), h("dd", {}, pct(l.max_slippage_pct, 2)))),
      h("div", { class: "card" }, h("h2", {}, "Kill switches"),
        ["global", "daily"].map((sc) => h("div", { class: "row", style: { marginBottom: "10px" } },
          severity(ks[sc].active ? "critical" : "info"),
          h("strong", {}, sc === "global" ? "Global" : "Diario"),
          h("span", { class: "secondary spacer" }, ks[sc].active ? `ACTIVO: ${ks[sc].reason} (${ks[sc].actor}, ${dt(ks[sc].at)})` : "inactivo"),
          ks[sc].active ? h("button", { type: "button", onclick: () => act("deactivate", sc) }, "Desactivar") : null)),
        h("div", { class: "form-grid section" }, h("label", { class: "field" }, "Ámbito", scope), h("label", { class: "field" }, "Motivo", reason)),
        h("label", { class: "row small section" }, flatten, "Cerrar además todas las posiciones (flatten)"),
        h("div", { class: "row section" }, h("button", { class: "danger", type: "button", onclick: () => act("activate", scope.value) }, "⛔ Activar kill switch")),
        h("p", { class: "muted small" }, "El kill switch bloquea nuevas entradas; las salidas (stop loss, take profit, espejo) siguen funcionando."))),
    h("div", { class: "grid cols-2 section" },
      h("div", { class: "card" }, h("h2", {}, "Gestión de salida (config)"),
        h("dl", { class: "kv" },
          h("dt", {}, "Modo por defecto"), h("dd", {}, r.exits.default_mode),
          h("dt", {}, "Cerrar al vender la wallet"), h("dd", {}, r.exits.close_on_source_sell ? "Sí" : "No (proporcional en espejo/protegido)"),
          h("dt", {}, "Stop loss"), h("dd", {}, pct(r.exits.stop_loss_pct, 0)),
          h("dt", {}, "Stop de emergencia"), h("dd", {}, pct(r.exits.emergency_stop_loss_pct, 0)),
          h("dt", {}, "Take profit"), h("dd", {}, r.exits.take_profit_levels.map((t) => `+${t.gain_pct}% → ${Math.round(t.sell_fraction * 100)}%`).join(" · ")),
          h("dt", {}, "Trailing stop"), h("dd", {}, r.exits.trailing_stop_pct ? `${r.exits.trailing_stop_pct}% (activa en +${r.exits.trailing_activation_pct}%)` : "No"),
          h("dt", {}, "Permanencia máxima"), h("dd", {}, r.exits.max_hold_minutes ? `${num(r.exits.max_hold_minutes, 0)} min` : "Sin límite"),
          h("dt", {}, "Stop según volatilidad"), h("dd", {}, r.exits.volatility_stop
            ? `Sí: ${r.exits.volatility_stop_sigmas}× el movimiento esperado, entre ${pct(r.exits.volatility_stop_min_pct, 0)} y ${pct(r.exits.volatility_stop_max_pct, 0)}` : "No"),
          h("dt", {}, "Perfil de la wallet"), h("dd", {}, r.exits.wallet_exit_profile
            ? `Sí: tiempo máx ${r.exits.profile_hold_multiple}× su holding mediano${r.exits.profile_take_profit ? ", TP según su ganancia mediana" : ""}` : "No"),
          h("dt", {}, "Salida por caída de liquidez"), h("dd", {}, r.exits.liquidity_drop_exit_pct ? `−${r.exits.liquidity_drop_exit_pct}% desde la entrada` : "No"),
          h("dt", {}, "Salida si venden varias wallets"), h("dd", {}, r.exits.wallet_sells_exit_min
            ? `${r.exits.wallet_sells_exit_min} wallets fiables venden ≥${Math.round(r.exits.wallet_sells_min_fraction * 100)}% → vender ${Math.round(r.exits.wallet_sells_exit_fraction * 100)}%` : "No"),
          h("dt", {}, "Retraso máximo de copia"), h("dd", {}, `${r.latency.max_signal_age_seconds} s`),
          h("dt", {}, "Desviación máxima de precio"), h("dd", {}, pct(r.latency.max_price_deviation_pct, 1)),
          h("dt", {}, "TTL de señal"), h("dd", {}, `${r.latency.signal_ttl_seconds} s`))),
      h("div", { class: "card" }, h("h2", {}, "Exposición actual"), table([
        { label: "Token", render: (e) => h("span", { class: "mono" }, short(e.token_mint)) },
        { label: "Nocional", num: true, render: (e) => usd(e.notional_usd) },
        { label: "En riesgo", num: true, render: (e) => usd(e.at_risk_usd) },
        { label: "Categoría", render: (e) => e.category || "—" },
        { label: "Alto riesgo", render: (e) => (e.is_high_risk ? "Sí" : "—") },
        { label: "Tipo", render: (e) => (e.position_id ? `posición #${e.position_id}` : "orden en vuelo / reserva") },
      ], r.exposures, { empty: "Sin exposición" }))),
    h("div", { class: "card section" }, h("h2", {}, "Eventos de riesgo"), table([
      { label: "Fecha", render: (e) => dt(e.ts) }, { label: "Tipo", render: (e) => e.type },
      { label: "Severidad", render: (e) => severity(e.severity) }, { label: "Detalle", wrap: true, render: (e) => e.message },
    ], r.events, { empty: "Sin eventos" })));
}
