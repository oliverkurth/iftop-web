"use strict";

(function () {
  const POLL_MS = 2000;
  const WINDOW_INDEX = { 2: 0, 10: 1, 40: 2 };
  // Bar scale maxima in bits/s, as in iftop. The scale only grows.
  const SCALE_STEPS = [64e3, 128e3, 256e3, 1e6, 10e6, 100e6, 1e9, 10e9, 100e9];
  const SETTINGS_KEY = "iftop-live-settings";

  const root = document.querySelector(".live");
  const apiUrl = root.dataset.api;
  const loginUrl = root.dataset.login;
  const els = {
    window: document.getElementById("window"),
    sort: document.getElementById("sort"),
    scaleMode: document.getElementById("scale-mode"),
    units: document.getElementById("units"),
    filter: document.getElementById("filter"),
    status: document.getElementById("status"),
    totals: document.getElementById("totals"),
    ticks: document.getElementById("ticks"),
    conns: document.getElementById("conns"),
    footer: document.getElementById("footer"),
    template: document.getElementById("conn-template"),
  };

  let scaleIdx = 0;
  let timer = null;
  let inflight = null;
  let lastData = null;
  const rows = new Map(); // connection key -> row element
  let othersRow = null;

  // ---- settings (per browser, optional) ----

  function loadSettings() {
    let s = {};
    try { s = JSON.parse(localStorage.getItem(SETTINGS_KEY) || "{}") || {}; } catch (e) { s = {}; }
    for (const k of ["window", "sort", "scaleMode", "units"]) {
      if (s[k] && [...els[k].options].some((o) => o.value === s[k])) els[k].value = s[k];
    }
  }

  function saveSettings() {
    const s = {};
    for (const k of ["window", "sort", "scaleMode", "units"]) s[k] = els[k].value;
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (e) { /* ignore */ }
  }

  // ---- formatting ----

  function formatRate(bytesPerSec) {
    const bits = els.units.value === "bits";
    let v = bits ? bytesPerSec * 8 : bytesPerSec;
    const base = bits ? 1000 : 1024;
    const units = bits ? ["b", "Kb", "Mb", "Gb", "Tb"] : ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (v >= base && i < units.length - 1) { v /= base; i++; }
    const digits = v >= 100 || i === 0 ? 0 : v >= 10 ? 1 : 2;
    return v.toFixed(digits) + units[i];
  }

  function formatBytes(n) {
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let v = n, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return v.toFixed(i === 0 ? 0 : v >= 100 ? 0 : 1) + units[i];
  }

  // ---- bar scale ----

  function scaleMax() { return SCALE_STEPS[scaleIdx]; }

  function growScale(maxBytesPerSec) {
    const bits = maxBytesPerSec * 8;
    let changed = false;
    while (bits > scaleMax() && scaleIdx < SCALE_STEPS.length - 1) { scaleIdx++; changed = true; }
    return changed;
  }

  function barFraction(bytesPerSec) {
    const bits = bytesPerSec * 8;
    if (bits <= 0) return 0;
    let f;
    if (els.scaleMode.value === "log") {
      f = bits <= 1 ? 0 : Math.log(bits) / Math.log(scaleMax());
    } else {
      f = bits / scaleMax();
    }
    return Math.max(0, Math.min(1, f));
  }

  function drawTicks() {
    els.ticks.replaceChildren();
    const max = scaleMax();
    const values = [];
    if (els.scaleMode.value === "log") {
      for (let v = 1e3; v < max; v *= 10) values.push(v);
    } else {
      for (let k = 1; k < 5; k++) values.push((max * k) / 5);
    }
    values.push(max);
    for (const bits of values) {
      const t = document.createElement("span");
      t.className = "tick" + (bits === max ? " end" : "");
      t.textContent = formatRate(bits / 8);
      t.style.left = (barFraction(bits / 8) * 100) + "%";
      els.ticks.appendChild(t);
    }
  }

  // ---- rendering ----

  function connKey(c) {
    return [c.protocol, c.src.addr, c.src.port, c.dst.addr, c.dst.port].join("|");
  }

  function setEnd(el, end) {
    el.querySelector(".name").textContent = end.host || end.addr || "*";
    el.querySelector(".port").textContent =
      end.port == null ? "" : (end.service && end.service !== String(end.port) ? end.service : String(end.port));
    const tip = [end.addr, end.port == null ? null : "port " + end.port].filter(Boolean).join(", ");
    el.title = end.host ? end.host + " (" + tip + ")" : tip;
  }

  function newRow() {
    return els.template.content.firstElementChild.cloneNode(true);
  }

  function setRates(row, sent, recv, idx) {
    for (const [kind, rates] of [["sent", sent], ["recv", recv]]) {
      const spans = row.querySelectorAll(".rates ." + kind + " span");
      for (let i = 0; i < 3; i++) {
        spans[i].textContent = rates ? formatRate(rates[i]) : "";
        spans[i].classList.toggle("sel", i === idx);
      }
      row.querySelector(".bar." + kind).style.width =
        (barFraction(rates ? rates[idx] : 0) * 100) + "%";
    }
  }

  function renderConnections(data) {
    const idx = WINDOW_INDEX[data.window];
    const seen = new Set();
    const frag = document.createDocumentFragment();

    for (const c of data.connections) {
      const key = connKey(c);
      seen.add(key);
      let row = rows.get(key);
      if (!row) {
        row = newRow();
        rows.set(key, row);
      }
      setEnd(row.querySelector(".end.src"), c.src);
      setEnd(row.querySelector(".end.dst"), c.dst);
      row.querySelector(".proto").textContent = c.proto;
      setRates(row, c.sent_rate, c.recv_rate, idx);
      frag.appendChild(row);
    }
    for (const [key, row] of rows) {
      if (!seen.has(key)) rows.delete(key);
    }

    if (data.others.count > 0) {
      if (!othersRow) {
        othersRow = newRow();
        othersRow.classList.add("others");
        othersRow.querySelector(".end.dst").remove();
        othersRow.querySelector(".proto").remove();
      }
      othersRow.querySelector(".end.src .name").textContent =
        data.others.count + " other connection" + (data.others.count === 1 ? "" : "s");
      othersRow.querySelector(".end.src .port").textContent = "";
      const sent = [0, 0, 0], recv = [0, 0, 0];
      sent[idx] = data.others.sent;
      recv[idx] = data.others.recv;
      setRates(othersRow, sent, recv, idx);
      for (const kind of ["sent", "recv"]) {
        const spans = othersRow.querySelectorAll(".rates ." + kind + " span");
        spans.forEach((s, i) => { if (i !== idx) s.textContent = ""; });
      }
      frag.appendChild(othersRow);
    }

    if (data.connections.length === 0) {
      const p = document.createElement("p");
      p.className = "empty";
      p.textContent = data.count === 0 ? "No traffic." : "No connection matches the filter.";
      frag.appendChild(p);
    }
    els.conns.replaceChildren(frag);

    const filtered = data.matched !== data.count ? " (" + data.matched + " match the filter)" : "";
    els.footer.textContent =
      "Showing " + data.shown + " of " + data.count + " connections" + filtered +
      " · bar scale " + formatRate(scaleMax() / 8) + " (" + els.scaleMode.value + ")";
  }

  function renderTotals(t) {
    const sent = t.sent_rate || [0, 0, 0];
    const recv = t.recv_rate || [0, 0, 0];
    const rowsData = {
      sent: [...sent.map(formatRate), formatBytes(t.total_sent || 0), formatRate(t.peak_sent_rate || 0)],
      recv: [...recv.map(formatRate), formatBytes(t.total_recv || 0), formatRate(t.peak_recv_rate || 0)],
      total: [...sent.map((s, i) => formatRate(s + recv[i])),
              formatBytes((t.total_sent || 0) + (t.total_recv || 0)), formatRate(t.peak_total_rate || 0)],
    };
    for (const tr of els.totals.rows) {
      const cells = tr.querySelectorAll("td");
      rowsData[tr.dataset.kind].forEach((v, i) => { cells[i].textContent = v; });
    }
  }

  function setStatus(text, kind) {
    els.status.textContent = text;
    els.status.className = "status" + (kind ? " " + kind : "");
  }

  function render(data) {
    lastData = data;
    const idx = WINDOW_INDEX[data.window];
    let max = 0;
    for (const c of data.connections) max = Math.max(max, c.sent_rate[idx], c.recv_rate[idx]);
    growScale(max);
    drawTicks();
    renderTotals(data.totals);
    renderConnections(data);
    if (data.stale) {
      setStatus(data.interface + ": no update for " + data.age + " s — is the collector running?", "stale");
    } else {
      setStatus(data.interface + " · live", "");
    }
  }

  // ---- how many rows fit ----

  function rowHeight() {
    const row = els.conns.querySelector(".conn");
    if (row && row.offsetHeight > 0) return row.offsetHeight;
    const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--row-h"));
    return v > 0 ? v : 44;
  }

  function visibleLimit() {
    const top = els.conns.getBoundingClientRect().top + window.scrollY;
    const available = window.innerHeight - top - els.footer.offsetHeight - 16;
    // One row is kept free for the "others" row.
    return Math.max(5, Math.floor(available / rowHeight()) - 1);
  }

  // ---- polling ----

  async function poll() {
    clearTimeout(timer);
    timer = null;
    if (document.hidden) return;
    if (inflight) inflight.abort();
    const ctl = new AbortController();
    inflight = ctl;
    const params = new URLSearchParams({
      limit: visibleLimit(),
      window: els.window.value,
      sort: els.sort.value,
      filter: els.filter.value.trim(),
    });
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
      if (!resp.ok || !data || data.error) {
        setStatus((data && data.error) || "error " + resp.status, "error");
      } else {
        render(data);
      }
    } catch (e) {
      if (e.name === "AbortError") return;
      setStatus("cannot reach the server", "error");
    } finally {
      if (inflight === ctl) inflight = null;
    }
    if (!document.hidden && timer === null) timer = setTimeout(poll, POLL_MS);
  }

  // ---- wiring ----

  let filterTimer = null;
  els.filter.addEventListener("input", () => {
    clearTimeout(filterTimer);
    filterTimer = setTimeout(poll, 300);
  });
  for (const k of ["window", "sort"]) {
    els[k].addEventListener("change", () => { saveSettings(); poll(); });
  }
  for (const k of ["scaleMode", "units"]) {
    els[k].addEventListener("change", () => { saveSettings(); if (lastData) render(lastData); });
  }
  // Clicking the scale resets it to the smallest step that fits.
  els.ticks.parentElement.addEventListener("click", () => {
    scaleIdx = 0;
    if (lastData) render(lastData);
  });
  els.ticks.parentElement.title = "Click to reset the bar scale";

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(poll, 250);
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      clearTimeout(timer);
      timer = null;
      if (inflight) inflight.abort();
    } else {
      poll();
    }
  });

  loadSettings();
  poll();
})();
