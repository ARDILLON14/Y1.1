import { api } from "../api.js";
import { h, table, addr, usd, pct, dt, short, toast, signClass, price } from "../dom.js";

export const refreshSeconds = 10;
let status = "open";

export async function render(root) {
  const rows = await api.get(`/positions?status=${status}&limit=300`);
  const totalUnreal = rows.reduce((a, p) => a + (p.unrealized_pnl_usd || 0), 0);
  root.replaceChildren(
    h("div", { class: "card-head" }, h("h1", {}, "Posiciones"),
      h("div", { class: "segmented" }, [["open", "Abiertas"], ["closed", "Cerradas"]].map(([v, l]) =>
        h("button", { type: "button", class: v === status ? "active" : "", onclick: () => { status = v; render(root); } }, l)))),
    status === "open" ? h("p", { class: "secondary" }, `${rows.length} abiertas · PnL no realizado `, h("span", { class: signClass(totalUnreal) }, usd(totalUnreal))) : null,
    h("div", { class: "card" }, table([
      { label: "Token", render: (p) => h("span", { title: p.token_mint }, p.token_symbol || short(p.token_mint)) },
      { label: "Modo", render: (p) => p.mode.toUpperCase() },
      { label: "Wallet origen", render: (p) => (p.source_wallet ? addr(p.source_wallet, p.source_wallet_label) : "—") },
      { label: "Salida", render: exitCell },
      { label: "Entrada", num: true, render: (p) => price(p.entry_price_usd) },
      { label: "Actual", num: true, render: (p) => price(p.last_price_usd) },
      { label: "Cambio", num: true, render: (p) => h("span", { class: signClass(p.change_pct) }, pct(p.change_pct, 1, true)) },
      { label: "Coste", num: true, render: (p) => usd(p.cost_usd) },
      { label: "Valor", num: true, render: (p) => usd(p.value_usd) },
      { label: "No realizado", num: true, render: (p) => h("span", { class: signClass(p.unrealized_pnl_usd) }, usd(p.unrealized_pnl_usd)) },
      { label: "Realizado", num: true, render: (p) => h("span", { class: signClass(p.realized_pnl_usd) }, usd(p.realized_pnl_usd)) },
      { label: "TP", render: (p) => (p.tp_levels_hit?.length ? p.tp_levels_hit.map((i) => `TP${i + 1}`).join(", ") : "—") },
      { label: "Apertura", render: (p) => h("span", { class: "nowrap" }, dt(p.opened_at)) },
      status === "open"
        ? { label: "", render: (p) => h("button", { class: "danger", type: "button", disabled: p.status !== "open",
          onclick: async (e) => {
            e.stopPropagation();
            if (!confirm(`¿Cerrar la posición en ${p.token_symbol || short(p.token_mint)} (${p.mode})?`)) return;
            try { const r = await api.post(`/positions/${p.id}/close`, { reason: "Cierre manual" }); toast(r.success ? "Posición cerrada" : `Error: ${r.error}`, !r.success); render(root); }
            catch (ex) { toast(ex.message, true); }
          } }, p.status === "closing" ? "Cerrando…" : "Cerrar") }
        : { label: "Motivo de cierre", wrap: true, render: (p) => `${p.close_reason || "—"} · ${dt(p.closed_at)}` },
    ], rows, { empty: status === "open" ? "Sin posiciones abiertas" : "Sin posiciones cerradas" })));
}

// Exit mode + the stop this position was sized for; an adapted profile explains itself on hover.
function exitCell(p) {
  const notes = p.exit_profile?.notes || [];
  const stop = p.exit_mode === "mirror" || p.stop_loss_pct == null ? "" : ` · SL ${pct(p.stop_loss_pct, 1)}`;
  return h("span", { class: "nowrap", title: notes.join("\n") || null },
    `${p.exit_mode_label}${stop}`, notes.length ? h("span", { class: "muted small" }, " · adaptada") : null);
}
