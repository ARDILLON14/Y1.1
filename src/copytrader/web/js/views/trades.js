import { api } from "../api.js";
import { h, table, addr, usd, num, dt, short, signClass, price } from "../dom.js";

export const refreshSeconds = 20;
let mode = "";

export async function render(root) {
  const rows = await api.get(`/trades?limit=300${mode ? `&mode=${mode}` : ""}`);
  root.replaceChildren(
    h("div", { class: "card-head" }, h("h1", {}, "Operaciones ejecutadas"),
      h("div", { class: "segmented" }, [["", "Todas"], ["paper", "Paper"], ["live", "Real"]].map(([v, l]) =>
        h("button", { type: "button", class: v === mode ? "active" : "", onclick: () => { mode = v; render(root); } }, l)))),
    h("p", { class: "secondary small" }, "Precio original = el de la wallet copiada · teórico = mercado al decidir · cotizado = el disponible para mi tamaño · ejecución = el obtenido. Slippage positivo = peor que el teórico."),
    h("div", { class: "card" }, table([
      { label: "Fecha", render: (r) => h("span", { class: "nowrap" }, dt(r.executed_at)) },
      { label: "Modo", render: (r) => r.mode.toUpperCase() },
      { label: "Tipo", render: (r) => (r.purpose === "entry" ? "Entrada (COMPRA)" : "Salida (VENTA)") },
      { label: "Wallet original", render: (r) => (r.source_wallet ? addr(r.source_wallet) : "—") },
      { label: "Token", render: (r) => r.token_symbol || h("span", { class: "mono" }, short(r.token_mint)) },
      { label: "Precio original", num: true, render: (r) => price(r.signal_price_usd) },
      { label: "Teórico", num: true, render: (r) => price(r.theoretical_price_usd) },
      { label: "Cotizado", num: true, render: (r) => price(r.quote_price_usd) },
      { label: "Ejecución", num: true, render: (r) => price(r.fill_price_usd) },
      { label: "Slippage", num: true, render: (r) => (r.slippage_bps === null ? "—" : `${num(r.slippage_bps / 100, 2)}%`) },
      { label: "Tamaño", num: true, render: (r) => usd(r.value_usd) },
      { label: "Fees", num: true, render: (r) => usd(r.fees_usd, 4) },
      { label: "PnL", num: true, render: (r) => (r.realized_pnl_usd === null ? "—" : h("span", { class: signClass(r.realized_pnl_usd) }, usd(r.realized_pnl_usd))) },
      { label: "Motivo", wrap: true, render: (r) => r.reason || (r.purpose === "entry" ? "Copia de señal" : r.trigger || "—") },
      { label: "Tx", render: (r) => (r.tx_signature && !r.tx_signature.startsWith("paper-")
        ? h("a", { href: `https://solscan.io/tx/${encodeURIComponent(r.tx_signature)}`, target: "_blank", rel: "noopener noreferrer" }, short(r.tx_signature, 5))
        : h("span", { class: "muted" }, "paper")) },
    ], rows, { empty: "Todavía no hay operaciones" })));
}
