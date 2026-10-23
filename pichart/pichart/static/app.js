"use strict";

const NOW_EVERY = 5000;
const HISTORY_EVERY = 60000;
const GB = 1024 ** 3;
const W = 1000;
const H = 160;

let latest = null;
let history = null;

const el = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const int = (n) => Math.round(n).toLocaleString("en-GB");

function bytes(n) {
  if (n == null) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function pct(x) {
  if (x == null) return "–";
  return `${x < 10 ? x.toFixed(1) : Math.round(x)} %`;
}

function duration(seconds) {
  const days = Math.floor(seconds / 86400);
  if (days >= 2) return `${days} days`;
  const hours = Math.floor(seconds / 3600);
  if (hours >= 1) return `${hours} h`;
  return `${Math.max(1, Math.floor(seconds / 60))} min`;
}

function when(ts) {
  return new Date(ts * 1000).toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function meter(id, fraction) {
  el(id).style.width = `${Math.max(0, Math.min(1, fraction || 0)) * 100}%`;
}

async function getJSON(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function refreshNow() {
  try {
    latest = await getJSON("api/now");
  } catch {
    el("status").innerHTML = '<span class="bad">The collector is not answering.</span>';
    return;
  }
  renderNow();
  renderServices();
}

async function refreshHistory() {
  try {
    history = await getJSON("api/history");
  } catch {
    return;
  }
  renderHistory();
  renderServices();
}

function renderNow() {
  if (latest.starting) {
    el("status").textContent = "Starting up, first sample in a few seconds";
    return;
  }
  const h = latest.host;
  const time = new Date(latest.time * 1000).toLocaleTimeString("en-GB");
  let status = `Last sample ${esc(time)} · refreshes every 5 s`;
  if (latest.docker_error) status += ` · <span class="bad">Docker not reachable</span>`;
  el("status").innerHTML = status;

  el("cpu-value").textContent = h.cpu == null ? "…" : pct(h.cpu);
  meter("cpu-meter", (h.cpu || 0) / 100);
  el("cpu-sub").textContent = `load ${h.load.map((v) => v.toFixed(2)).join(" · ")} on ${h.cores} cores`;

  el("mem-value").innerHTML = `${(h.mem_used / GB).toFixed(1)} <small>of ${(h.mem_total / GB).toFixed(1)} GB</small>`;
  meter("mem-meter", h.mem_used / h.mem_total);
  el("mem-sub").textContent = h.swap_total ? `swap ${bytes(h.swap_used)} of ${bytes(h.swap_total)}` : "no swap";

  el("temp-value").textContent = h.temp == null ? "n/a" : `${Math.round(h.temp)} °C`;
  meter("temp-meter", (h.temp || 0) / 85);

  const power = el("power-value");
  if (!h.power) {
    power.textContent = "?";
    power.className = "value";
    el("power-sub").textContent = "throttle flags not readable here";
  } else if (h.power.now.length) {
    power.textContent = "⚠ Now";
    power.className = "value bad";
    el("power-sub").textContent = h.power.now.join(", ");
  } else if (h.power.since_boot.length) {
    power.textContent = "! Earlier";
    power.className = "value warn";
    el("power-sub").textContent = `since boot: ${h.power.since_boot.join(", ")}`;
  } else {
    power.textContent = "✓ OK";
    power.className = "value ok";
    el("power-sub").textContent = "no undervoltage or throttling since boot";
  }
  el("uptime-sub").textContent = `up ${duration(h.uptime)}`;

  el("disk-value").innerHTML = `${esc(bytes(h.disk_free))} <small>free</small>`;
  meter("disk-meter", 1 - h.disk_free / h.disk_total);
  el("disk-sub").textContent = `of ${bytes(h.disk_total)}`;
  const d = h.docker_disk;
  el("docker-sub").textContent = d
    ? `Docker: images ${bytes(d.images)} · logs ${bytes(d.logs)} · volumes ${bytes(d.volumes)} · cache ${bytes(d.build_cache)}`
    : "";

  el("net-value").innerHTML = h.net_rx == null ? "…" : `↓ ${esc(bytes(h.net_rx))}<small>/s</small>`;
  el("net-sub").textContent = h.net_tx == null ? "" : `↑ ${bytes(h.net_tx)}/s`;
  if (history) el("net48-sub").textContent = `48 h: ↓ ${bytes(history.network.rx)} · ↑ ${bytes(history.network.tx)}`;
}

function sparkPath(values) {
  const max = Math.max(...values, 0) || 1;
  const step = 100 / values.length;
  return values
    .map((v, k) => {
      const height = v > 0 ? Math.max((v / max) * 22, 1.5) : 0;
      return height ? `M${(k * step).toFixed(2)} ${(24 - height).toFixed(2)}h${(step * 0.72).toFixed(2)}v${height.toFixed(2)}h-${(step * 0.72).toFixed(2)}Z` : "";
    })
    .join("");
}

function activity(s) {
  const total = history?.totals?.[s.key];
  const hourly = history?.hourly?.[s.key];
  switch (s.activity) {
    case "requests":
      return { text: total ? `${int(total)} requests` : "no requests yet", series: hourly };
    case "traffic":
      return { text: `${bytes(total || 0)} traffic`, series: hourly };
    case "connections":
      return { text: `${int(total || 0)} connections`, series: hourly };
    case "dns": {
      const a = latest.adguard;
      if (!a) return { text: "…" };
      if (a.error === "no credentials") return { text: "add AdGuard's login to .env to show DNS queries" };
      if (a.error) return { text: "AdGuard stats unavailable" };
      const blocked = a.queries ? Math.round((100 * a.blocked) / a.queries) : 0;
      const period = a.units === "hours" ? "24 h" : "stats period";
      return { text: `${int(a.queries)} DNS queries · ${blocked} % blocked (AdGuard's ${period})`, series: a.series };
    }
    default:
      return { text: s.key === "pichart" ? "itself" : "" };
  }
}

const STATUS = {
  up: ["● Up", "ok"],
  "no answer": ["● No answer", "warn"],
  missing: ["○ Not found", "warn"],
  unknown: ["? Unknown", "warn"],
};

function renderServices() {
  if (!latest || latest.starting) return;
  const services = latest.services;
  const rows = services.map((s) => {
    const [statusText, statusClass] = STATUS[s.status] || [`● ${s.status[0].toUpperCase()}${s.status.slice(1)}`, "bad"];
    const name = s.url ? `<a href="${esc(s.url)}">${esc(s.label)}</a>` : esc(s.label);
    const act = activity(s);
    const spark = act.series && act.series.some((v) => v > 0)
      ? `<svg viewBox="0 0 100 24" preserveAspectRatio="none" width="120" height="24" aria-hidden="true"><path d="${sparkPath(act.series)}" fill="#0000cc"></path></svg>`
      : `<svg viewBox="0 0 100 24" width="120" height="24" aria-hidden="true"></svg>`;
    const restarts = s.restarts == null ? "–" : `${s.restarts}${s.restarts > 0 ? ' <span class="flag">[check]</span>' : ""}`;
    return `<div class="row">
<span class="name">${name}${s.key === hungriestKey(services) ? ' <span class="flag">[hungriest]</span>' : ""}</span>
<span class="${statusClass}"><b>${statusText}</b></span>
<span>${s.started_at ? duration(latest.time - s.started_at) : "–"}</span>
<span>${restarts}</span>
<span>${pct(s.cpu)}</span>
<span>${bytes(s.mem)}</span>
<span class="activity">${spark}<span>${esc(act.text)}</span></span>
</div>`;
  });
  const table = el("services");
  table.innerHTML = table.firstElementChild.outerHTML + rows.join("");
  renderCallout(services);
}

function hungriestKey(services) {
  const running = services.filter((s) => s.cpu != null);
  if (!running.length) return null;
  return running.reduce((a, b) => (b.cpu > a.cpu ? b : a)).key;
}

function renderCallout(services) {
  const parts = [];
  const key = hungriestKey(services);
  if (key) {
    const s = services.find((x) => x.key === key);
    const withMem = services.filter((x) => x.mem != null);
    let text = `<b>Hungriest right now:</b> ${esc(s.label)}, ${pct(s.cpu)} CPU.`;
    if (withMem.length) {
      const ram = withMem.reduce((a, b) => (b.mem > a.mem ? b : a));
      text += ram.key === s.key ? ` It also uses the most RAM, ${bytes(ram.mem)}.` : ` Most RAM: ${esc(ram.label)}, ${bytes(ram.mem)}.`;
    }
    parts.push(text);
  }
  if (history?.peak?.cpu != null) {
    const label = services.find((x) => x.key === history.peak.service)?.label;
    parts.push(
      `<b>Busiest in 48 h:</b> CPU peaked at ${pct(history.peak.cpu)} on ${esc(when(history.peak.ts))}` +
        (label ? `, mostly ${esc(label)}.` : ".")
    );
  }
  el("callout").innerHTML = parts.join(" ");
}

function xAt(i, n) {
  return ((i / (n - 1)) * W).toFixed(1);
}

function linePath(values, y) {
  let d = "";
  let pen = false;
  values.forEach((v, i) => {
    if (v == null) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${xAt(i, values.length)} ${y(v)} `;
    if (!pen && (i === values.length - 1 || values[i + 1] == null)) d += "h3 ";
    pen = true;
  });
  return d;
}

function bandPath(low, high, y) {
  let d = "";
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      d += run.map((i, k) => `${k ? "L" : "M"}${xAt(i, high.length)} ${y(high[i])}`).join(" ");
      d += ` ${run.slice().reverse().map((i) => `L${xAt(i, low.length)} ${y(low[i])}`).join(" ")} Z `;
    }
    run = [];
  };
  high.forEach((v, i) => {
    if (v == null || low[i] == null) flush();
    else run.push(i);
  });
  flush();
  return d;
}

function ticks(start, bucket, n) {
  const span = (n - 1) * bucket;
  const out = [];
  for (let t = Math.ceil(start / 3600) * 3600; t <= start + span; t += 3600) {
    const date = new Date(t * 1000);
    if (date.getHours() % 12 === 0) {
      const label = date.toLocaleString("en-GB", { weekday: "short", hour: "2-digit", minute: "2-digit" });
      out.push({ label, left: ((t - start) / span) * 100 });
    }
  }
  return out;
}

function renderChart(id, { avg, max, lo, hi, grid, format, ref, label }) {
  const n = avg.length;
  const y = (v) => (H - ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * H).toFixed(1);
  const container = el(id);
  if (!avg.some((v) => v != null)) {
    container.innerHTML = '<p class="empty">No history yet. The first points appear a minute after Pi Chart starts.</p>';
    return;
  }
  const gridLines = grid
    .map((g) => `<line x1="0" y1="${y(g)}" x2="${W}" y2="${y(g)}" stroke="#d4d4d4" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"></line>`)
    .join("");
  const refLine = ref == null ? "" : `<line x1="0" y1="${y(ref)}" x2="${W}" y2="${y(ref)}" stroke="#404040" stroke-width="1.5" stroke-dasharray="8 5" vector-effect="non-scaling-stroke"></line>`;
  container.innerHTML = `<div class="chart">
<div class="yaxis">${grid.map((g) => `<span style="top:${(Number(y(g)) / H) * 100}%">${esc(format(g, true))}</span>`).join("")}</div>
<div class="plot">
<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(label)}">
${gridLines}${refLine}
<path d="${bandPath(avg, max, y)}" fill="#ffc4c4"></path>
<path d="${linePath(avg, y)}" fill="none" stroke="#cc0000" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"></path>
<line class="cursor" x1="0" y1="0" x2="0" y2="${H}" stroke="#000000" stroke-width="1" vector-effect="non-scaling-stroke" visibility="hidden"></line>
</svg>
${ref == null ? "" : `<span class="ref">throttles at ${ref} °C</span>`}
<div class="tip" hidden></div>
<div class="xaxis">${ticks(history.start, history.bucket, n).map((t) => `<span style="left:${t.left}%">${esc(t.label)}</span>`).join("")}</div>
</div>
</div>`;
  const plot = container.querySelector(".plot");
  const svg = plot.querySelector("svg");
  const cursor = svg.querySelector(".cursor");
  const tip = plot.querySelector(".tip");
  const hide = () => {
    tip.hidden = true;
    cursor.setAttribute("visibility", "hidden");
  };
  svg.addEventListener("pointermove", (event) => {
    const box = svg.getBoundingClientRect();
    const i = Math.round(((event.clientX - box.left) / box.width) * (n - 1));
    if (i < 0 || i >= n || avg[i] == null) return hide();
    const x = xAt(i, n);
    cursor.setAttribute("x1", x);
    cursor.setAttribute("x2", x);
    cursor.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.textContent = `${when(history.start + i * history.bucket)} · avg ${format(avg[i])} · peak ${format(max[i])}`;
    const left = Math.min(Math.max((i / (n - 1)) * 100, 18), 82);
    tip.style.left = `${left}%`;
  });
  svg.addEventListener("pointerleave", hide);
}

function renderHistory() {
  const s = history.series;
  renderChart("chart-cpu", {
    ...s.cpu,
    lo: 0,
    hi: 100,
    grid: [0, 25, 50, 75, 100],
    format: (v, axis) => (axis ? String(v) : pct(v)),
    label: "CPU over the last 48 hours, 15-minute averages with peaks",
  });
  renderChart("chart-temp", {
    ...s.temp,
    lo: 30,
    hi: 90,
    grid: [30, 50, 70, 90],
    ref: 80,
    format: (v, axis) => (axis ? String(v) : `${v.toFixed(1)} °C`),
    label: "Temperature over the last 48 hours, 15-minute averages with peaks",
  });
  const total = latest?.host?.mem_total ? latest.host.mem_total / GB : Math.max(...s.mem.max.filter((v) => v != null), GB) / GB;
  const top = Math.ceil(total);
  el("mem-caption").textContent = `GB used of ${total.toFixed(1)}`;
  renderChart("chart-mem", {
    avg: s.mem.avg.map((v) => (v == null ? null : v / GB)),
    max: s.mem.max.map((v) => (v == null ? null : v / GB)),
    lo: 0,
    hi: top,
    grid: [0, top / 2, top],
    format: (v, axis) => (axis ? String(+v.toFixed(1)) : `${v.toFixed(2)} GB`),
    label: "Memory used over the last 48 hours, 15-minute averages with peaks",
  });
  if (latest) renderNow();
}

refreshNow().then(refreshHistory);
setInterval(() => document.visibilityState === "visible" && refreshNow(), NOW_EVERY);
setInterval(() => document.visibilityState === "visible" && refreshHistory(), HISTORY_EVERY);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    refreshNow();
    refreshHistory();
  }
});
