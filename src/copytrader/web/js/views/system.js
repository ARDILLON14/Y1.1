import { api } from "../api.js";
import { h, table, dt, toast, askPassword, severity, num, pct, usd, tile, fill } from "../dom.js";

export const refreshSeconds = 30;

const LEVELS = [
  [1, "Solo análisis", "Analiza wallets, no ejecuta nada."],
  [2, "Alertas", "Detecta operaciones de las wallets seleccionadas y avisa."],
  [3, "Paper trading", "Simula automáticamente las operaciones sin capital real."],
  [4, "Capital pequeño", "Operaciones reales con límites extremadamente estrictos."],
  [5, "Ejecución normal", "Solo cuando todas las protecciones funcionan correctamente."],
];

export async function render(root) {
  const [st, audit, speed] = await Promise.all([api.get("/system/status"), api.get("/audit"), api.get("/system/speed")]);
  const m = st.mode;
  const setLevel = async (lvl) => {
    const cred = await askPassword(`Cambiar a nivel ${lvl}`, lvl >= 4
      ? "Niveles 4-5 operan con dinero real una vez armados. Confirma con tu contraseña."
      : "Confirma el cambio de nivel operativo.");
    if (!cred) return;
    try { await api.post("/system/level", { level: lvl, ...cred }); toast(`Nivel ${lvl} activo`); render(root); } catch (ex) { toast(ex.message, true); }
  };
  const arm = async () => {
    const cred = await askPassword("Armar trading REAL", "Se ejecutará el preflight. Si lo supera, las próximas señales se ejecutarán con dinero real.");
    if (!cred) return;
    try { await api.post("/system/arm", cred); toast("Trading real ARMADO"); render(root); } catch (ex) { toast(ex.message, true); }
  };
  const disarm = async () => { await api.post("/system/disarm"); toast("Trading real desarmado"); render(root); };
  const pre = h("div");
  fill(root, 
    h("h1", {}, "Sistema"),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h2", {}, "Nivel operativo"),
        h("p", { class: "secondary small" }, `Máximo permitido por configuración: nivel ${m.ceiling}. Modo actual de entradas: ${m.trade_mode ? m.trade_mode.toUpperCase() : "sin ejecución"}.`),
        LEVELS.map(([n, name, desc]) => h("div", { class: "row", style: { marginBottom: "8px" } },
          h("span", { class: "badge" }, h("span", { class: `dot ${n === m.level ? "good" : ""}` }), `${n} · ${name}`),
          h("span", { class: "muted small spacer" }, desc),
          n === m.level ? h("span", { class: "small" }, "actual")
            : h("button", { type: "button", disabled: n > m.ceiling, title: n > m.ceiling ? "Sube app.operating_level en el YAML" : "", onclick: () => setLevel(n) }, "Activar"))),
        m.level >= 4 ? h("div", { class: "section row" },
          m.armed ? h("button", { class: "danger", type: "button", onclick: disarm }, "Desarmar trading real")
            : h("button", { class: "primary", type: "button", onclick: arm }, "Armar trading real"),
          h("span", { class: "secondary small" }, m.live_block_reason || "Trading real ARMADO")) : null),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", {}, "Preflight (requisito para dinero real)"),
        h("button", { type: "button", onclick: async () => {
          fill(pre, h("p", { class: "muted" }, "Ejecutando…"));
          const r = await api.get("/system/preflight");
          fill(pre, h("p", {}, h("strong", {}, r.passed ? "✓ SUPERADO" : "✗ NO SUPERADO")),
            table([{ label: "", render: (c) => (c.passed ? "✓" : c.critical ? "✗" : "!") }, { label: "Comprobación", render: (c) => c.label },
              { label: "Detalle", wrap: true, render: (c) => c.message || "" }], r.checks));
        } }, "Ejecutar")), pre)),
    speedCard(speed),
    h("div", { class: "grid cols-2 section" },
      h("div", { class: "card" }, h("h2", {}, `Componentes · estado ${st.overall}`), table([
        { label: "Componente", render: (c) => c.name }, { label: "Tipo", render: (c) => c.kind },
        { label: "Estado", render: (c) => severity(c.status === "ok" ? "info" : c.status === "down" ? "critical" : "warning") },
        { label: "Detalle", wrap: true, render: (c) => c.detail || (c.last_ok_at ? `OK ${dt(c.last_ok_at)}` : "") },
      ], st.health, { empty: "Sin datos de salud todavía" }),
      h("p", { class: "small muted section" }, `Notificaciones: ${st.notifications.length ? st.notifications.join(", ") : "ninguna configurada"}`),
      st.last_cycle ? h("p", { class: "small muted" }, `Último ciclo de análisis: ${st.last_cycle.evaluated} wallets en ${num(st.last_cycle.duration_seconds, 2)} s`) : null,
      h("button", { type: "button", onclick: async () => { await api.post("/system/evaluate"); toast("Reevaluación solicitada"); } }, "Reevaluar wallets ahora")),
      h("div", { class: "card" }, h("h2", {}, "Auditoría"), table([
        { label: "Fecha", render: (a) => h("span", { class: "nowrap" }, dt(a.ts)) }, { label: "Actor", render: (a) => a.actor },
        { label: "Acción", render: (a) => a.action }, { label: "Objetivo", render: (a) => a.target || "—" }, { label: "IP", render: (a) => a.ip || "—" },
      ], audit.slice(0, 100), { empty: "Sin registros" }))));
}

const STREAM_NAMES = {
  logs_subscribe: "logsSubscribe",
  helius_transaction_subscribe: "Helius transactionSubscribe",
  backup_logs_subscribe: "Respaldo · logsSubscribe",
  backup_helius_transaction_subscribe: "Respaldo · Helius transactionSubscribe",
  simulated: "Simulado",
};
const ROUTE_NAMES = { rpc: "RPC principal", jito: "Jito block engine" };

function seconds(s) { return s && s.p50 !== null ? `${num(s.p50, 2)} s` : "—"; }
function spread(s, unit = "s", digits = 2) {
  return s && s.n ? `p90 ${num(s.p90, digits)} ${unit} · ${s.n} medidas` : "sin medidas todavía";
}
function lamports(v) { return v === null || v === undefined ? "—" : `${num(v / 1e9, 6)} SOL`; }
function routeName(r) { return ROUTE_NAMES[r] || r.replace(/^rpc_extra_(\d+)$/, "RPC extra $1").replace(/^jito_(\d+)$/, "Jito $1"); }

function speedCard(sp) {
  const f = sp.fees;
  const detections = Object.entries(sp.detection);
  const best = detections.filter(([, s]) => s.p50 !== null).sort((a, b) => a[1].p50 - b[1].p50)[0];
  const feeSource = f.observed_lamports !== null
    ? `real: mediana de ${f.observed_samples} operaciones`
    : `estimada · ${f.observed_samples}/${f.min_samples} operaciones reales para usar la real`;
  const policy = f.kind === "jito"
    ? `Propina Jito ${f.tip_percentile ? `(percentil ${f.tip_percentile} del mercado)` : "fija"}, máx ${lamports(f.cap_lamports)}`
    : `Priority fee: entradas ${f.entry_level}, salidas rutinarias ${f.exit_level}, protección veryHigh; máx ${lamports(f.cap_lamports)}`;
  const tips = f.tip_floor && Object.keys(f.tip_floor.percentiles).length
    ? Object.entries(f.tip_floor.percentiles).map(([p, v]) => `p${p} ${lamports(v)}`).join(" · ")
    : null;
  return h("div", { class: "card section" },
    h("h2", {}, "Velocidad y comisiones"),
    h("p", { class: "muted small" }, "Cuánto tardas en copiar y cuánto pagas por ir rápido. Los datos de streams y rutas son de esta sesión (se reinician al arrancar)."),
    h("div", { class: "grid cols-3" },
      tile("Retraso de copia", seconds(sp.copy_delay), `de la operación de la wallet a tu ejecución · ${spread(sp.copy_delay)}`),
      tile("Detección", best ? seconds(best[1]) : "—", best ? `${STREAM_NAMES[best[0]] || best[0]} · ${spread(best[1])}` : "sin operaciones detectadas todavía"),
      tile("Comisión por operación", f.expected_usd === null ? lamports(f.expected_lamports) : usd(f.expected_usd, 3),
        `${f.expected_pct_of_trade === null ? "" : `${pct(f.expected_pct_of_trade, 2)} de ${usd(f.typical_size_usd, 0)} · `}${feeSource}`)),
    h("p", { class: "small secondary section" }, policy,
      f.max_trade_pct ? ` · como mucho ${pct(f.max_trade_pct, 2)} del tamaño salvo salidas de protección` : "",
      tips ? ` · propinas recientes: ${tips}` : ""),
    h("div", { class: "grid cols-2 section" },
      h("div", {}, h("h3", {}, "Streams"),
        table([
          { label: "Stream", render: (s) => STREAM_NAMES[s.stream] || s.stream },
          { label: "Avisos", num: true, render: (s) => s.notices },
          { label: "Llegó primero", num: true, render: (s) => (s.first_share === null ? s.first : `${s.first} · ${pct(s.first_share * 100, 0)}`) },
          { label: "Ventaja (mediana)", num: true, render: (s) => (s.lead_ms.n ? `${num(s.lead_ms.p50, 0)} ms` : "—") },
        ], sp.streams, { empty: "Sin avisos todavía (en modo simulado no hay streams)" })),
      h("div", {}, h("h3", {}, "Rutas de envío"),
        sp.routes ? h("p", { class: "muted small" }, `Con propina: ${sp.routes.tipped.map(routeName).join(", ")} · sin propina: ${sp.routes.untipped.map(routeName).join(", ")}`) : null,
        table([
          { label: "Ruta", render: (r) => routeName(r.route) },
          { label: "Aceptadas", num: true, render: (r) => r.accepted },
          { label: "Fallidas", num: true, render: (r) => r.failed },
          { label: "Respuesta (mediana)", num: true, render: (r) => (r.ms.n ? `${num(r.ms.p50, 0)} ms` : "—") },
        ], sp.send_routes, { empty: "Sin envíos reales todavía" }))));
}
