"use strict";

(function () {
  const SETTINGS_KEY = "iftop-history-settings";
  // Refresh more often for short windows; the collector writes every 10 s.
  const REFRESH_MS = { minute: 10000, "5min": 10000, hour: 10000, day: 60000, week: 300000, month: 300000 };

  const root = document.querySelector(".history");
  const apiUrl = root.dataset.api;
  const loginUrl = root.dataset.login;
  const els = {
    tabs: [...root.querySelectorAll(".tabs button")],
    group: document.getElementById("group"),
    sort: document.getElementById("sort"),
    units: document.getElementById("units"),
    status: document.getElementById("status"),
    summary: document.getElementById("summary"),
    canvas: document.getElementById("chart"),
    chartMissing: document.getElementById("chart-missing"),
    head: document.getElementById("top-head"),
    body: document.getElementById("top-body"),
    foot: document.getElementById("top-foot"),
  };

  let current = "hour";
  let chart = null;
  let timer = null;
  let inflight = null;
  let lastData = null;

  // ---- settings (per browser, optional) ----

  function loadSettings() {
    let s = {};
    try { s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}; } catch (e) { s = {}; }
    if (s.window && els.tabs.some((t) => t.dataset.window === s.window)) current = s.window;
    for (const k of ["group", "sort", "units"]) {
      if (s[k] && [...els[k].options].some((o) => o.value === s[k])) els[k].value = s[k];
    }
  }

  function saveSettings() {
    const s = { window: current, group: els.group.value, sort: els.sort.value, units: els.units.value };
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) { /* ignore */ }
  }

  // ---- formatting ----

  const bits = () => els.units.value === "bits";
  const rate = (bytesPerSec) => IftopFormat.rate(bytesPerSec, bits()) + "/s";
  const bytes = IftopFormat.bytes;

  // Spacing of the chart's tick marks per window, in seconds of local time.
  const TICKS = { minute: 10, "5min": 60, hour: 600, day: 3 * 3600, week: 86400, month: 86400 };

  const withSeconds = (w) => w === "minute" || w === "5min";

  function dateTime(t, seconds) {
    const opts = { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" };
    if (seconds) opts.second = "2-digit";
    return new Date(t * 1000).toLocaleString(undefined, opts);
  }

  // Start of the step containing t, in local clock time (steps up to a day).
  function localAlign(t, step) {
    const offset = -new Date(t * 1000).getTimezoneOffset() * 60;
    return t - (((t + offset) % step) + step) % step;
  }

  function tickValues(from, to, w) {
    const step = TICKS[w];
    const ticks = [];
    if (step >= 86400) {
      // Local midnights, which are 23 to 25 hours apart around DST changes.
      const d = new Date(from * 1000);
      d.setHours(0, 0, 0, 0);
      for (let t = d.getTime() / 1000; t <= to; d.setDate(d.getDate() + 1), t = d.getTime() / 1000) {
        if (t >= from) ticks.push({ value: t });
      }
    } else {
      for (let t = localAlign(from, step); t <= to; t += step) {
        if (t >= from) ticks.push({ value: t });
      }
    }
    return ticks;
  }

  function tickLabel(t, w) {
    const d = new Date(t * 1000);
    switch (w) {
      case "minute":
        return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      case "week":
        return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric" });
      case "month":
        return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
      default:
        return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
    }
  }

  function duration(seconds) {
    if (seconds < 120) return Math.round(seconds) + " s";
    if (seconds < 7200) return Math.round(seconds / 60) + " min";
    if (seconds < 172800) return Math.round(seconds / 3600) + " h";
    return Math.round(seconds / 86400) + " days";
  }

  function endpoint(end) {
    const name = end.host || end.addr || "*";
    if (end.folded) return name + ":*";
    if (end.port == null) return name;
    const port = end.service && end.service !== String(end.port) ? end.service : String(end.port);
    return name + ":" + port;
  }

  function endpointTitle(end) {
    if (!end.addr) return "";
    const parts = [end.addr];
    if (end.folded) parts.push("client ports folded");
    else if (end.port != null) parts.push("port " + end.port);
    return parts.join(", ");
  }

  // ---- chart ----

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function renderChart(data) {
    if (typeof window.Chart === "undefined") {
      els.canvas.hidden = true;
      els.chartMissing.hidden = false;
      return;
    }
    const w = data.window;
    const points = data.chart.points;
    // NaN (not null) marks a point without data; Chart.js leaves a gap there.
    const toXY = (i) => points.map((p) => ({ x: p[0], y: p[i] == null ? NaN : p[i] }));

    // A new chart each time: the axis must span exactly the window, also
    // where there is no data, and options changed on a live chart are not
    // reliably applied to its scales.
    if (chart) chart.destroy();
    Chart.defaults.color = cssVar("--muted");
    Chart.defaults.borderColor = cssVar("--border");
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    chart = new Chart(els.canvas, {
      type: "line",
      data: {
        datasets: [
          { label: "sent", borderColor: cssVar("--sent"), backgroundColor: cssVar("--sent"), data: toXY(1) },
          { label: "received", borderColor: cssVar("--recv"), backgroundColor: cssVar("--recv"), data: toXY(2) },
        ],
      },
      options: {
        animation: false,
        maintainAspectRatio: false,
        parsing: false,
        spanGaps: false, // no data (collector not running) is a break, not zero
        elements: { point: { radius: 0, hitRadius: 6 }, line: { borderWidth: 1.5, tension: 0 } },
        interaction: { mode: "index", intersect: false },
        scales: {
          x: {
            type: "linear",
            min: data.from,
            max: data.now,
            afterBuildTicks: (axis) => { axis.ticks = tickValues(data.from, data.now, w); },
            ticks: { maxRotation: 0, autoSkipPadding: 16, callback: (v) => tickLabel(v, w) },
          },
          y: { beginAtZero: true, ticks: { callback: (v) => rate(v) } },
        },
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12 } },
          tooltip: {
            callbacks: {
              title: (items) => items.length ? dateTime(items[0].parsed.x, withSeconds(w)) : "",
              label: (item) => item.dataset.label + ": " +
                (Number.isNaN(item.parsed.y) ? "no data" : rate(item.parsed.y)),
            },
          },
        },
      },
    });
  }

  // ---- table ----

  function cell(tr, text, cls, title) {
    const td = document.createElement("td");
    td.textContent = text;
    if (cls) td.className = cls;
    if (title) td.title = title;
    tr.appendChild(td);
    return td;
  }

  function header(cols) {
    const tr = document.createElement("tr");
    for (const [label, cls] of cols) {
      const th = document.createElement("th");
      th.textContent = label;
      if (cls) th.className = cls;
      tr.appendChild(th);
    }
    els.head.replaceChildren(tr);
  }

  function numbers(tr, s, r, seconds) {
    cell(tr, bytes(s), "num sent");
    cell(tr, bytes(r), "num recv");
    cell(tr, bytes(s + r), "num");
    cell(tr, seconds > 0 ? rate((s + r) / seconds) : "", "num muted");
  }

  function renderTable(data) {
    const tuple = data.group === "tuple";
    const seconds = data.totals.covered_seconds;
    const numCols = [["Sent", "num"], ["Received", "num"], ["Total", "num"], ["Average", "num"]];
    header(tuple ? [["Source"], ["Destination"], ["Proto"], ...numCols]
                 : [[data.group === "src" ? "Source host" : "Destination host"], ...numCols]);

    const rows = document.createDocumentFragment();
    for (const item of data.items) {
      const tr = document.createElement("tr");
      if (tuple) {
        cell(tr, endpoint(item.src), "host", endpointTitle(item.src));
        cell(tr, endpoint(item.dst), "host", endpointTitle(item.dst));
        cell(tr, item.proto, "muted");
      } else {
        const h = item[data.group];
        cell(tr, h.host || h.addr || "*", "host", h.addr || "");
      }
      numbers(tr, item.bytes_sent, item.bytes_recv, seconds);
      rows.appendChild(tr);
    }
    if (!data.items.length) {
      const tr = document.createElement("tr");
      cell(tr, "No traffic in this window.", "empty").colSpan = tuple ? 7 : 5;
      rows.appendChild(tr);
    }
    els.body.replaceChildren(rows);

    const foot = document.createDocumentFragment();
    if (data.others.count > 0) {
      const tr = document.createElement("tr");
      tr.className = "others";
      const label = cell(tr, data.others.count + " more", "host");
      if (tuple) label.colSpan = 3;
      numbers(tr, data.others.bytes_sent, data.others.bytes_recv, seconds);
      foot.appendChild(tr);
    }
    els.foot.replaceChildren(foot);
  }

  function renderSummary(data) {
    const t = data.totals;
    const span = data.to - data.from;
    const parts = [
      dateTime(data.from, withSeconds(data.window)) + " – " + dateTime(data.to, withSeconds(data.window)) +
        " (" + duration(span) + ")",
      "sent " + bytes(t.bytes_sent),
      "received " + bytes(t.bytes_recv),
    ];
    if (t.covered_seconds > 0) parts.push("average " + rate((t.bytes_sent + t.bytes_recv) / t.covered_seconds));
    const coverage = span > 0 ? t.covered_seconds / span : 0;
    if (coverage < 0.98) parts.push("data for " + Math.round(Math.min(1, coverage) * 100) + "% of the time");
    els.summary.textContent = parts.join(" · ");
  }

  function render(data) {
    lastData = data;
    renderSummary(data);
    renderChart(data);
    renderTable(data);
    els.status.textContent = "";
    els.status.className = "status";
  }

  function setStatus(text) {
    els.status.textContent = text;
    els.status.className = "status error";
  }

  // ---- loading ----

  async function load() {
    clearTimeout(timer);
    timer = null;
    if (document.hidden) return;
    if (inflight) inflight.abort();
    const ctl = new AbortController();
    inflight = ctl;
    const params = new URLSearchParams({ window: current, group: els.group.value, sort: els.sort.value });
    try {
      const resp = await fetch(apiUrl + "?" + params, {
        credentials: "same-origin", cache: "no-store", signal: ctl.signal,
        headers: { Accept: "application/json" },
      });
      if (resp.status === 401) {
        window.location.assign(loginUrl);
        return;
      }
      const data = await resp.json().catch(() => null);
      if (!resp.ok || !data || data.error) setStatus((data && data.error) || "error " + resp.status);
      else render(data);
    } catch (e) {
      if (e.name === "AbortError") return;
      setStatus("cannot reach the server");
    } finally {
      if (inflight === ctl) inflight = null;
    }
    if (!document.hidden && timer === null) timer = setTimeout(load, REFRESH_MS[current] || 60000);
  }

  function selectTab(w) {
    current = w;
    for (const t of els.tabs) t.setAttribute("aria-selected", String(t.dataset.window === w));
  }

  for (const t of els.tabs) {
    t.addEventListener("click", () => { selectTab(t.dataset.window); saveSettings(); load(); });
  }
  for (const k of ["group", "sort"]) {
    els[k].addEventListener("change", () => { saveSettings(); load(); });
  }
  els.units.addEventListener("change", () => { saveSettings(); if (lastData) render(lastData); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
      if (inflight) inflight.abort();
    } else {
      load();
    }
  });

  loadSettings();
  selectTab(current);
  load();
})();
