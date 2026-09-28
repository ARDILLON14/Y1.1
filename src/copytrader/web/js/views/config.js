import { api } from "../api.js";
import { h, table, dt, toast, askPassword } from "../dom.js";

let section = "risk";

const HELP = {
  risk: "Límites de riesgo. Los límites absolutos codificados no se pueden superar.",
  selection: "Cuántas wallets copiar (Top N), score mínimo e histéresis.",
  exits: "Stop loss, take profit escalonado, trailing, tiempo máximo y modo de salida.",
  latency: "Protección de latencia: retraso máximo, desviación de precio, TTL.",
  scoring: "Pesos y parámetros del scoring.",
  levels: "Topes del nivel 4 (capital pequeño).",
  notifications: "Qué eventos se notifican y con qué severidad mínima.",
};

export async function render(root) {
  const [cfg, history] = await Promise.all([api.get("/config"), api.get("/config/history")]);
  const sections = Object.keys(cfg.config);
  const mutable = new Set(cfg.mutable_sections);
  const editable = mutable.has(section);
  const inputs = [];
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      const patch = {};
      try {
        for (const inp of inputs) {
          const v = readInput(inp);
          if (JSON.stringify(v) !== JSON.stringify(inp._orig)) setPath(patch, inp._path, v);
        }
      } catch (ex) { toast(`JSON inválido en ${ex.path}: ${ex.message}`, true); return; }
      if (!Object.keys(patch).length) { toast("Sin cambios"); return; }
      try {
        await api.post("/config/preview", { patch });
        const cred = await askPassword("Confirmar cambio de configuración", "Los cambios afectan a límites de riesgo y se aplican al instante. Confirma con tu contraseña.");
        if (!cred) return;
        const r = await api.patch("/config", { patch, comment: comment.value, ...cred });
        toast(`Configuración guardada (v${r.version}). Se aplica inmediatamente.`);
        render(root);
      } catch (ex) { toast(ex.message, true); }
    },
  }, buildFields(cfg.config[section], [section], inputs, !editable));
  const comment = h("input", { type: "text", placeholder: "Comentario del cambio (opcional)", maxlength: "500" });
  root.replaceChildren(
    h("h1", {}, `Configuración central (versión ${cfg.version})`),
    h("p", { class: "secondary" }, "Los cambios se validan en conjunto (incluidos los límites absolutos) y se versionan. Las secciones de solo lectura requieren editar config/settings.yaml y reiniciar."),
    h("div", { class: "row", style: { marginBottom: "12px" } },
      h("select", { onchange: (e) => { section = e.target.value; render(root); } },
        sections.map((s) => h("option", { value: s, selected: s === section ? "selected" : null }, `${s}${mutable.has(s) ? "" : " (solo lectura)"}`)))),
    h("div", { class: "card" },
      HELP[section] ? h("p", { class: "secondary small" }, HELP[section]) : null,
      form,
      editable ? h("div", { class: "row section" }, comment, h("span", { class: "spacer" }),
        h("button", { class: "primary", type: "button", onclick: () => form.requestSubmit() }, "Validar y guardar")) : null),
    h("div", { class: "card section" }, h("h2", {}, "Historial de versiones"), table([
      { label: "Versión", num: true, render: (v) => v.version },
      { label: "Fecha", render: (v) => dt(v.created_at) },
      { label: "Autor", render: (v) => v.author },
      { label: "Comentario", wrap: true, render: (v) => v.comment || "—" },
      { label: "", render: (v) => h("button", { type: "button", onclick: async () => {
        const cred = await askPassword(`Volver a la versión ${v.version}`, "La configuración de esa versión se aplicará al instante.");
        if (!cred) return;
        try { await api.post(`/config/rollback/${v.version}`, cred); toast("Rollback aplicado"); render(root); } catch (ex) { toast(ex.message, true); }
      } }, "Restaurar") },
    ], history, { empty: "Sin cambios en caliente todavía" })));
}

function buildFields(obj, path, inputs, readonly) {
  const fields = [];
  const nested = [];
  for (const [k, v] of Object.entries(obj)) {
    const p = [...path, k];
    if (v !== null && typeof v === "object" && !Array.isArray(v) && Object.values(v).every((x) => typeof x !== "object" || x === null)
        && Object.keys(v).length && !k.startsWith("severity") && k !== "events") {
      nested.push(h("fieldset", {}, h("legend", {}, k), h("div", { class: "form-grid" }, buildFields(v, p, inputs, readonly))));
      continue;
    }
    let inp;
    if (typeof v === "boolean") inp = h("input", { type: "checkbox", checked: v, disabled: readonly });
    else if (typeof v === "number") inp = h("input", { type: "number", step: "any", value: String(v), disabled: readonly });
    else if (v === null || typeof v === "string") inp = h("input", { type: "text", value: v ?? "", placeholder: v === null ? "(vacío)" : "", disabled: readonly });
    else { inp = h("textarea", { disabled: readonly }, JSON.stringify(v, null, 2)); inp.style.minHeight = "80px"; inp._json = true; }
    inp._path = p; inp._orig = v; inp._nullable = v === null;
    inputs.push(inp);
    fields.push(h("label", { class: "field" + (inp._json ? " wide" : "") }, k, inp));
  }
  return [h("div", { class: "form-grid" }, fields), nested];
}

function readInput(inp) {
  if (inp.type === "checkbox") return inp.checked;
  if (inp.type === "number") return inp.value === "" ? null : Number(inp.value);
  if (inp._json) {
    try { return JSON.parse(inp.value); } catch (e) { e.path = inp._path.join("."); throw e; }
  }
  if (inp._nullable && inp.value === "") return null;
  if (typeof inp._orig === "number") return Number(inp.value);
  return inp.value;
}

function setPath(obj, path, value) {
  let node = obj;
  for (const key of path.slice(0, -1)) node = node[key] ??= {};
  node[path.at(-1)] = value;
}
