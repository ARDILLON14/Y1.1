import { api } from "../api.js";
import { h, table, dt, toast, askPassword, severity, num } from "../dom.js";

export const refreshSeconds = 30;

const LEVELS = [
  [1, "Solo análisis", "Analiza wallets, no ejecuta nada."],
  [2, "Alertas", "Detecta operaciones de las wallets seleccionadas y avisa."],
  [3, "Paper trading", "Simula automáticamente las operaciones sin capital real."],
  [4, "Capital pequeño", "Operaciones reales con límites extremadamente estrictos."],
  [5, "Ejecución normal", "Solo cuando todas las protecciones funcionan correctamente."],
];

export async function render(root) {
  const [st, audit] = await Promise.all([api.get("/system/status"), api.get("/audit")]);
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
  root.replaceChildren(
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
          pre.replaceChildren(h("p", { class: "muted" }, "Ejecutando…"));
          const r = await api.get("/system/preflight");
          pre.replaceChildren(h("p", {}, h("strong", {}, r.passed ? "✓ SUPERADO" : "✗ NO SUPERADO")),
            table([{ label: "", render: (c) => (c.passed ? "✓" : c.critical ? "✗" : "!") }, { label: "Comprobación", render: (c) => c.label },
              { label: "Detalle", wrap: true, render: (c) => c.message || "" }], r.checks));
        } }, "Ejecutar")), pre)),
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
