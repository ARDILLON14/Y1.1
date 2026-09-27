import { api } from "../api.js";
import { h, severity, dt, toast } from "../dom.js";

export const refreshSeconds = 15;
let sev = "";

export async function render(root) {
  const rows = await api.get(`/alerts?limit=200${sev ? `&severity=${sev}` : ""}`);
  root.replaceChildren(
    h("div", { class: "card-head" }, h("h1", {}, "Alertas"),
      h("div", { class: "row" },
        h("div", { class: "segmented" }, [["", "Todas"], ["info", "Info"], ["warning", "Avisos"], ["critical", "Críticas"]].map(([v, l]) =>
          h("button", { type: "button", class: v === sev ? "active" : "", onclick: () => { sev = v; render(root); } }, l))),
        h("button", { type: "button", onclick: async () => { await api.post("/alerts/ack", {}); toast("Todas marcadas como leídas"); render(root); } }, "Marcar todas como leídas"))),
    rows.length ? rows.map((a) => h("div", { class: "card", style: { marginBottom: "10px", opacity: a.acknowledged ? "0.7" : "1" } },
      h("div", { class: "card-head" }, h("div", { class: "row" }, severity(a.severity), h("strong", {}, a.title)),
        h("span", { class: "muted small" }, `${dt(a.ts)}${a.channels?.length ? ` · enviada a ${a.channels.join(", ")}` : ""}`)),
      h("pre", { class: "explain" }, a.body))) : h("div", { class: "empty card" }, "Sin alertas"));
}
