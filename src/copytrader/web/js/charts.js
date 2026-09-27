// Minimal SVG charts following the data-viz rules: 2px lines, hairline recessive grid,
// one y-axis, crosshair + tooltip on hover, text in ink tokens (never series color).
import { h, clear } from "./dom.js";

const W = 800;

function niceTicks(min, max, count = 4) {
  if (min === max) { min -= 1; max += 1; }
  const span = max - min;
  const step0 = span / count;
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

// series: [{name, color: "var(--series-1)", points: [{x: Date, y: number}]}]
function autoXFormat(spanMs) {
  if (spanMs <= 36 * 3600e3) return (d) => d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit" });
  if (spanMs <= 120 * 86400e3) return (d) => d.toLocaleDateString("es-ES", { day: "2-digit", month: "2-digit" });
  return (d) => d.toLocaleDateString("es-ES", { month: "2-digit", year: "2-digit" });
}

export function lineChart(container, series, { height = 220, yFormat = (v) => v, xFormat = null, area = true } = {}) {
  clear(container);
  const all = series.flatMap((s) => s.points);
  if (all.length < 2) { container.appendChild(h("div", { class: "empty" }, "Todavía no hay suficientes datos")); return; }
  const pad = { l: 64, r: 16, t: 10, b: 26 };
  const xs = all.map((p) => +p.x), ys = all.map((p) => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  xFormat = xFormat || autoXFormat(x1 - x0);
  const ticks = niceTicks(Math.min(...ys), Math.max(...ys));
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (x) => pad.l + ((x - x0) / Math.max(1, x1 - x0)) * (W - pad.l - pad.r);
  const sy = (y) => pad.t + (1 - (y - y0) / Math.max(1e-12, y1 - y0)) * (height - pad.t - pad.b);
  const svg = h("svg", { viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": series.map((s) => s.name).join(", ") });
  for (const t of ticks) {
    svg.appendChild(h("line", { class: "gridline", x1: pad.l, x2: W - pad.r, y1: sy(t), y2: sy(t) }));
    svg.appendChild(h("text", { class: "tick", x: pad.l - 8, y: sy(t) + 4, "text-anchor": "end" }, yFormat(t, step)));
  }
  svg.appendChild(h("line", { class: "baseline", x1: pad.l, x2: W - pad.r, y1: height - pad.b, y2: height - pad.b }));
  const nX = 4;
  for (let i = 0; i <= nX; i++) {
    const x = x0 + ((x1 - x0) * i) / nX;
    svg.appendChild(h("text", { class: "tick", x: sx(x), y: height - 8, "text-anchor": i === 0 ? "start" : i === nX ? "end" : "middle" }, xFormat(new Date(x))));
  }
  for (const s of series) {
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${sx(+p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join("");
    if (area && series.length === 1) {
      const base = sy(Math.max(y0, Math.min(y1, y0)));
      const a = h("path", { d: `${d}L${sx(+s.points.at(-1).x)},${base}L${sx(+s.points[0].x)},${base}Z` });
      a.style.fill = s.color; a.style.opacity = "0.10";
      svg.appendChild(a);
    }
    const line = h("path", { class: "line", d });
    line.style.stroke = s.color;
    svg.appendChild(line);
    const last = s.points.at(-1);
    const end = h("circle", { class: "hover-dot", cx: sx(+last.x), cy: sy(last.y), r: 4 });
    end.style.fill = s.color;
    svg.appendChild(end);
  }
  // hover layer
  const cross = h("line", { class: "crosshair", y1: pad.t, y2: height - pad.b, visibility: "hidden" });
  const dots = series.map((s) => { const c = h("circle", { class: "hover-dot", r: 5, visibility: "hidden" }); c.style.fill = s.color; return c; });
  svg.append(cross, ...dots);
  const tip = h("div", { class: "tooltip", hidden: true });
  const overlay = h("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: height - pad.t - pad.b, fill: "transparent" });
  svg.appendChild(overlay);
  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const xv = x0 + ((px - pad.l) / (W - pad.l - pad.r)) * (x1 - x0);
    clear(tip);
    let anchorX = null, anchorY = null;
    series.forEach((s, i) => {
      let best = s.points[0];
      for (const p of s.points) if (Math.abs(+p.x - xv) < Math.abs(+best.x - xv)) best = p;
      dots[i].setAttribute("cx", sx(+best.x)); dots[i].setAttribute("cy", sy(best.y)); dots[i].setAttribute("visibility", "visible");
      if (anchorX === null) {
        anchorX = sx(+best.x); anchorY = sy(best.y);
        tip.appendChild(h("div", { class: "t-label" }, new Date(+best.x).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })));
      }
      const key = h("span", { class: "key" }); key.style.background = s.color;
      tip.appendChild(h("div", {}, series.length > 1 ? key : null, series.length > 1 ? `${s.name}: ` : "", h("strong", {}, yFormat(best.y))));
    });
    cross.setAttribute("x1", anchorX); cross.setAttribute("x2", anchorX); cross.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = `${(anchorX / W) * rect.width}px`;
    tip.style.top = `${(anchorY / height) * rect.height}px`;
  };
  const leave = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); };
  overlay.addEventListener("pointermove", move);
  overlay.addEventListener("pointerleave", leave);
  if (series.length > 1) {
    container.appendChild(h("div", { class: "legend" }, series.map((s) => {
      const k = h("span", { class: "key" }); k.style.background = s.color; return h("span", {}, k, s.name);
    })));
  }
  container.appendChild(h("div", { class: "chart" }, svg, tip));
}

// Diverging columns (e.g. daily PnL): positive = series-1, negative = --neg, 4px rounded data end.
export function barChart(container, items, { height = 180, yFormat = (v) => v, labelFormat = (l) => l } = {}) {
  clear(container);
  if (!items.length) { container.appendChild(h("div", { class: "empty" }, "Sin datos")); return; }
  const pad = { l: 64, r: 8, t: 10, b: 24 };
  const vals = items.map((i) => i.value);
  const ticks = niceTicks(Math.min(0, ...vals), Math.max(0, ...vals));
  const y0 = ticks[0], y1 = ticks.at(-1);
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;
  const sy = (y) => pad.t + (1 - (y - y0) / Math.max(1e-12, y1 - y0)) * (height - pad.t - pad.b);
  const band = (W - pad.l - pad.r) / items.length;
  const bw = Math.min(24, Math.max(2, band - 2));
  const svg = h("svg", { viewBox: `0 0 ${W} ${height}`, role: "img", "aria-label": "Gráfico de barras" });
  for (const t of ticks) {
    svg.appendChild(h("line", { class: t === 0 ? "baseline" : "gridline", x1: pad.l, x2: W - pad.r, y1: sy(t), y2: sy(t) }));
    svg.appendChild(h("text", { class: "tick", x: pad.l - 8, y: sy(t) + 4, "text-anchor": "end" }, yFormat(t, step)));
  }
  const tip = h("div", { class: "tooltip", hidden: true });
  items.forEach((it, i) => {
    const cx = pad.l + band * i + band / 2;
    const top = sy(Math.max(0, it.value)), bottom = sy(Math.min(0, it.value));
    const hgt = Math.max(1, bottom - top);
    const r = Math.min(4, hgt / 2, bw / 2);
    const x = cx - bw / 2;
    // rounded data-end, square at the baseline
    const d = it.value >= 0
      ? `M${x},${bottom}V${top + r}Q${x},${top} ${x + r},${top}H${x + bw - r}Q${x + bw},${top} ${x + bw},${top + r}V${bottom}Z`
      : `M${x},${top}V${bottom - r}Q${x},${bottom} ${x + r},${bottom}H${x + bw - r}Q${x + bw},${bottom} ${x + bw},${bottom - r}V${top}Z`;
    const bar = h("path", { d });
    bar.style.fill = it.value >= 0 ? "var(--series-1)" : "var(--neg)";
    const hit = h("rect", { x: pad.l + band * i, y: pad.t, width: band, height: height - pad.t - pad.b, fill: "transparent" });
    hit.addEventListener("pointerenter", () => {
      const rect = svg.getBoundingClientRect();
      clear(tip).append(h("div", { class: "t-label" }, labelFormat(it.label)), h("strong", {}, yFormat(it.value)), it.extra ? h("div", { class: "t-label" }, it.extra) : "");
      tip.hidden = false;
      tip.style.left = `${(cx / W) * rect.width}px`;
      tip.style.top = `${(top / height) * rect.height}px`;
    });
    hit.addEventListener("pointerleave", () => { tip.hidden = true; });
    svg.append(bar, hit);
  });
  const n = items.length;
  [0, Math.floor(n / 2), n - 1].forEach((i, k) => {
    svg.appendChild(h("text", { class: "tick", x: pad.l + band * i + band / 2, y: height - 6, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle" }, labelFormat(items[i].label)));
  });
  container.appendChild(h("div", { class: "chart" }, svg, tip));
}

// Horizontal magnitude bars (single hue) with value at the tip.
export function hbars(items, { max = 1, format = (v) => v } = {}) {
  return h("div", {}, items.map((it) => {
    const fill = h("div", { class: "fill" });
    fill.style.width = `${Math.max(0, Math.min(1, (it.value ?? 0) / max)) * 100}%`;
    return h("div", { class: "hbar", title: it.title || "" }, h("span", { class: "secondary" }, it.label),
      h("div", { class: "track" }, fill), h("span", { class: "num" }, it.value === null || it.value === undefined ? "—" : format(it.value)));
  }));
}
