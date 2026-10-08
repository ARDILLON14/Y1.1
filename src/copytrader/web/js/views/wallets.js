import { api } from "../api.js";
import { h, table, addr, walletStatus, usd, frac, num, pct, ago, modal, toast, signClass, fill } from "../dom.js";

export const refreshSeconds = 60;
const state = { status: "", list: "", q: "", onlySelected: false };

export async function render(root) {
  const wallets = await api.get("/wallets");
  const filtered = wallets.filter((w) =>
    (!state.status || w.status === state.status) && (!state.list || w.list_type === state.list)
    && (!state.onlySelected || w.selected)
    && (!state.q || w.address.toLowerCase().includes(state.q) || (w.label || "").toLowerCase().includes(state.q)));
  filtered.sort((a, b) => (b.score ?? -1) - (a.score ?? -1));
  const rerender = () => render(root);
  const search = h("input", { type: "text", placeholder: "Buscar dirección o etiqueta", value: state.q,
    oninput: (e) => { state.q = e.target.value.toLowerCase(); clearTimeout(search._t); search._t = setTimeout(rerender, 250); } });
  const pending = wallets.filter((w) => !w.backfilled).length;
  fill(root,
    h("div", { class: "card-head" }, h("h1", {}, `Wallets (${wallets.length})`),
      h("div", { class: "row" },
        h("button", { type: "button", onclick: () => addDialog(rerender) }, "+ Añadir"),
        h("button", { type: "button", onclick: () => importDialog(rerender) }, "Importar CSV"),
        h("button", { type: "button", onclick: async () => { await api.post("/system/evaluate"); toast("Reevaluación solicitada"); } }, "Reevaluar ahora"))),
    pending ? h("div", { class: "banner info" }, "ℹ ", h("div", {},
      h("strong", {}, `Descargando historial: ${wallets.length - pending} de ${wallets.length} wallets listas`),
      h("div", { class: "small" }, "Score, PnL y Ops aparecen cuando termina la descarga de todas y el análisis siguiente. "
        + "Con un RPC gratuito, unos 2 minutos por wallet (hasta 1.000 transacciones cada una). Esta página se actualiza sola."))) : null,
    h("div", { class: "row", style: { marginBottom: "12px" } }, search,
      select(["", "active", "observe", "blocked"], ["Todos los estados", "ACTIVA", "OBSERVAR", "BLOQUEADA"], state.status, (v) => { state.status = v; rerender(); }),
      select(["", "none", "whitelist", "watchlist", "blacklist"], ["Todas las listas", "Sin lista", "Whitelist", "Watchlist", "Blacklist"], state.list, (v) => { state.list = v; rerender(); }),
      h("label", { class: "row small" }, h("input", { type: "checkbox", checked: state.onlySelected, onchange: (e) => { state.onlySelected = e.target.checked; rerender(); } }), "Solo seleccionadas")),
    h("div", { class: "card" }, table([
      { label: "#", num: true, render: (w) => w.rank ?? "—" },
      { label: "Wallet", render: (w) => addr(w.address, w.label) },
      { label: "Estado", title: "ACTIVA: se puede copiar. OBSERVAR: se sigue pero no se copia. BLOQUEADA: nunca. Pasa el ratón por el estado de cada wallet para ver el motivo.", render: (w) => h("span", { title: (w.status_reasons || []).join("\n") }, walletStatus(w.status)) },
      { label: "Copia", title: "Sí = seleccionada: sus operaciones se copian (en papel o reales según el nivel).", render: (w) => (w.selected ? h("span", { class: "badge" }, h("span", { class: "dot good" }), "Sí") : h("span", { class: "muted" }, "—")) },
      { label: "Score", num: true, title: "0-100. Con pocas operaciones se acerca a 40 (neutro): falta muestra. Para copiar hace falta el mínimo configurado (55 por defecto).", render: (w) => (w.score === null ? "—" : num(w.score, 1)) },
      { label: "PnL", num: true, title: "Ganancia total en USD: operaciones cerradas + posiciones abiertas valoradas al precio actual (lo abierto aún no está ganado ni perdido).", render: (w) => h("span", { class: signClass(w.metrics?.total_pnl_usd) }, usd(w.metrics?.total_pnl_usd, 0)) },
      { label: "ROI", num: true, title: "Rentabilidad solo de las operaciones cerradas (ganancia realizada / lo invertido en ellas).", render: (w) => pct(w.metrics?.roi_pct, 1, true) },
      { label: "Win rate", num: true, title: "Porcentaje de operaciones cerradas con ganancia.", render: (w) => frac(w.metrics?.win_rate) },
      { label: "PF", num: true, title: "Profit factor: lo ganado en las operaciones ganadoras / lo perdido en las perdedoras. Menos de 1 = pierde dinero.", render: (w) => num(w.metrics?.profit_factor, 2) },
      { label: "Copia est./op", num: true, title: "Retorno medio estimado por operación si la copias tú (con tu retraso, tu tamaño y los costes). Una sola operación muy grande puede inflarlo.", render: (w) => {
        const m = w.metrics || {};
        const v = m.effective_copy_expectancy_pct ?? m.copy_expectancy_pct;
        const real = m.realized_copy_n ? ` Corregido con ${m.realized_copy_n} copias reales (estimación inicial ${pct(m.copy_expectancy_pct, 2, true)}).` : "";
        return h("span", {
          class: signClass(v),
          title: `Retorno medio por operación si la copias (tu latencia, tamaño y costes). Su retorno: ${pct(m.expectancy_pct, 2, true)}.${real}`,
        }, pct(v, 2, true), m.realized_copy_n ? " ✓" : "");
      } },
      { label: "Drawdown", num: true, title: "Peor caída desde un máximo de su resultado acumulado.", render: (w) => pct(w.metrics?.max_drawdown_pct, 1) },
      { label: "Ops", num: true, title: "Operaciones cerradas (compra y venta del mismo token) en el periodo analizado.", render: (w) => (w.backfilled ? w.metrics?.n_trades ?? "—"
        : h("span", { class: "muted", title: "Descargando su historial" }, "descargando…")) },
      { label: "Última actividad", render: (w) => h("span", { class: "nowrap" }, ago(w.last_activity_at)) },
      { label: "Lista", render: (w) => listSelect(w, rerender) },
    ], filtered, { onRowClick: (w) => { location.hash = `#/wallet/${encodeURIComponent(w.address)}`; }, empty: "No hay wallets. Añádelas o importa un CSV." })),
  );
}

function select(values, labels, current, onchange) {
  return h("select", { onchange: (e) => onchange(e.target.value) },
    values.map((v, i) => h("option", { value: v, selected: v === current ? "selected" : null }, labels[i])));
}

function listSelect(w, rerender) {
  return h("select", {
    onclick: (e) => e.stopPropagation(),
    onchange: async (e) => {
      try {
        await api.patch(`/wallets/${encodeURIComponent(w.address)}`, { list_type: e.target.value });
        toast("Lista actualizada"); rerender();
      } catch (ex) { toast(ex.message, true); }
    },
  }, ["none", "whitelist", "watchlist", "blacklist"].map((v) => h("option", { value: v, selected: v === w.list_type ? "selected" : null },
    { none: "—", whitelist: "Whitelist", watchlist: "Watchlist", blacklist: "Blacklist" }[v])));
}

function addDialog(done) {
  const address = h("input", { type: "text", required: true, minlength: "32", maxlength: "44", placeholder: "Dirección Solana" });
  const label = h("input", { type: "text", maxlength: "100", placeholder: "Etiqueta (opcional)" });
  const list = select(["", "whitelist", "watchlist", "blacklist"], ["Sin lista", "Whitelist", "Watchlist", "Blacklist"], "", () => {});
  let close;
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        const r = await api.post("/wallets", { address: address.value.trim(), label: label.value || null, list_type: list.value || null });
        toast(r.created ? "Wallet añadida: se descargará su historial y se analizará" : "Wallet actualizada");
        close(); done();
      } catch (ex) { toast(ex.message, true); }
    },
  }, h("div", { class: "form-grid" }, h("label", { class: "field" }, "Dirección", address), h("label", { class: "field" }, "Etiqueta", label), h("label", { class: "field" }, "Lista", list)),
  h("div", { class: "row section" }, h("span", { class: "spacer" }), h("button", { class: "primary", type: "submit" }, "Añadir")));
  close = modal("Añadir wallet", form);
}

function importDialog(done) {
  const text = h("textarea", { placeholder: "address,label,list,notes\n7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU,Trader A,whitelist," });
  const out = h("div", { class: "small section" });
  let close;
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        const r = await api.post("/wallets/import", { csv: text.value });
        fill(out, h("p", {}, `Añadidas ${r.added.length} · actualizadas ${r.updated.length} · errores ${r.errors.length}`),
          r.errors.length ? h("ul", { class: "reasons" }, r.errors.map((x) => h("li", {}, x))) : "");
        if (!r.errors.length) { close(); }
        done();
      } catch (ex) { toast(ex.message, true); }
    },
  }, h("p", { class: "secondary small" }, "Una wallet por línea: dirección, etiqueta, lista (whitelist/watchlist/blacklist), notas. Las líneas con # se ignoran."),
  text, out, h("div", { class: "row section" }, h("span", { class: "spacer" }), h("button", { class: "primary", type: "submit" }, "Importar")));
  close = modal("Importar wallets (CSV)", form);
}
