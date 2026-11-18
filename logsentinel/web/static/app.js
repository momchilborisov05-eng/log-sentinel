/* Log Sentinel dashboard: vanilla JS + Chart.js (+ optional GSAP). */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const motion = typeof gsap !== "undefined" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  const METRIC_ORDER = ["memory_pct", "db_connections", "cpu_pct", "latency_p95_ms", "error_rate_pct"];

  const state = { hours: 48, filter: "open", sort: "recent", analysis: null, config: null, charts: {}, kpis: {}, openTpl: null,
                  seenAlerts: new Set(), firstLoad: true };

  // ------------------------------------------------------------------ helpers
  async function api(path, opts = {}) {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    let body = null;
    try { body = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      const msg = body && (typeof body.detail === "string" ? body.detail : body.error) || res.statusText;
      throw new Error(`${res.status} ${msg}`);
    }
    return body;
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function fmtVal(v, unit, decimals = 1) {
    if (v === null || v === undefined || Number.isNaN(v)) return "-";
    const n = Number(v).toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    return unit === "%" ? `${n}%` : unit ? `${n} ${unit}` : n;
  }

  function fmtTime(ts, withDay = false) {
    const d = new Date(ts * 1000);
    const t = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return withDay ? `${d.toLocaleDateString([], { weekday: "short" })} ${t}` : t;
  }

  function ago(ts, now) {
    const s = Math.max(0, now - ts);
    if (s < 90) return "just now";
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400 * 2) return `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m ago`;
    return `${Math.round(s / 86400)}d ago`;
  }

  function toast(msg, isError = false) {
    const el = $("#toast");
    el.textContent = msg;
    el.className = "toast" + (isError ? " error" : "");
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 3800);
    if (motion) gsap.fromTo(el, { y: 12, opacity: 0 }, { y: 0, opacity: 1, duration: 0.25, ease: "power2.out" });
  }

  function countUp(el, to, decimals = 0) {
    const from = state.kpis[el.id] ?? 0;
    state.kpis[el.id] = to;
    if (!motion || from === to) { el.textContent = Number(to).toFixed(decimals); return; }
    const o = { v: from };
    gsap.to(o, { v: to, duration: 0.9, ease: "power2.out", onUpdate: () => { el.textContent = o.v.toFixed(decimals); } });
  }

  // ------------------------------------------------------------------ theme
  function setTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("ls-theme", t); } catch (e) { /* private mode */ }
    Object.keys(state.charts).forEach((m) => state.charts[m]._data && drawChart(m, state.charts[m]._data));
  }
  $("#theme-toggle").addEventListener("click", () => {
    setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  });

  // ------------------------------------------------------------------ KPIs
  function renderKpis(a) {
    const k = a.kpis;
    countUp($("#kpi-health"), k.health);
    const bar = $("#kpi-health-bar");
    bar.style.width = `${k.health}%`;
    bar.style.background = k.health >= 80 ? css("--success") : k.health >= 50 ? css("--warning") : css("--danger");
    $("#kpi-health-sub").textContent = k.health >= 80 ? "Healthy" : k.health >= 50 ? "Degraded" : "At risk";
    countUp($("#kpi-open"), k.open_alerts);
    $("#kpi-open-sub").textContent = k.critical_open ? `${k.critical_open} critical` : "none critical";
    countUp($("#kpi-pred"), k.predicted_failures);
    $("#kpi-pred-sub").textContent = k.next_failure || "No metric trending to failure";
    $("#kpi-pred-sub").title = k.next_failure || "";
    countUp($("#kpi-err"), k.errors_last_hour);
    $("#kpi-err-sub").textContent = `${k.warnings_last_hour.toLocaleString()} warnings`;
    countUp($("#kpi-tpl"), k.templates);
    $("#kpi-tpl-sub").textContent = k.new_templates_24h ? `${k.new_templates_24h} new in 24h` : "no new patterns in 24h";
    $("#updated").textContent = `data until ${fmtTime(a.now, true)} · analysed in ${a.analysis_ms} ms`;
  }

  // ------------------------------------------------------------------ predictions
  function renderPredictions(a) {
    const box = $("#predictions");
    if (!a.predictions.length) {
      box.innerHTML = `<div class="card empty"><svg viewBox="0 0 24 24" width="26" height="26"><path d="M20 6 9 17l-5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
        <div><strong>No failure predicted</strong> within the next ${state.config ? state.config.forecast_horizon_hours : 48}h.</div>
        <div class="hint">A forecast appears when a metric shows a statistically clear upward trend.</div></div>`;
      return;
    }
    box.innerHTML = a.predictions.map((p) => {
      const pct = Math.max(2, Math.min(100, (p.current / p.threshold) * 100));
      const [num, ...rest] = p.eta_human.split(" ");
      return `<article class="card pred ${p.severity}" data-metric="${esc(p.metric)}">
        <div class="pred-top"><span class="pred-metric">${esc(p.label)}</span>
          <span class="pill ${p.confidence_label}" title="confidence score ${p.confidence}">${esc(p.confidence_label)} confidence</span></div>
        <div class="pred-eta">${esc(num)}${rest.length ? " " + esc(rest.join(" ")) : ""} <span>to failure</span></div>
        <div class="pred-head">${esc(p.headline)}</div>
        <div class="pred-band">80% band: ${esc(p.band_human)} · expected at ${fmtTime(p.crossing_ts, true)}</div>
        <div class="progress" role="img" aria-label="${pct.toFixed(0)}% of threshold"><span style="width:${pct}%"></span></div>
        <div class="pred-meta"><span>now ${fmtVal(p.current, p.unit, p.unit === "%" ? 1 : 0)}</span>
          <span>${p.slope_per_hour >= 0 ? "+" : ""}${p.slope_per_hour.toFixed(2)}${p.unit === "%" ? "%" : ""}/h</span>
          <span>limit ${fmtVal(p.threshold, p.unit, 0)}</span></div>
        ${p.runbook ? `<p class="pred-runbook">${esc(p.runbook)}</p>` : ""}
      </article>`;
    }).join("");
  }

  // ------------------------------------------------------------------ charts
  function ensureChartCard(m, label, wide) {
    let card = $(`#chart-${m}`);
    if (!card) {
      card = document.createElement("article");
      card.className = "card chart-card" + (wide ? " wide" : "");
      card.id = `chart-${m}`;
      card.innerHTML = `<div class="chart-head"><span class="chart-title">${esc(label)}</span><span class="chart-value"></span></div>
        <div class="chart-box"><canvas aria-label="${esc(label)} chart" role="img"></canvas></div>`;
      $("#charts").appendChild(card);
    }
    card.classList.toggle("wide", wide);
    return card;
  }

  function drawChart(m, d) {
    const card = ensureChartCard(m, d.label, !!d.forecast.length);
    const last = d.points[d.points.length - 1];
    const dec = d.decimals;
    card.querySelector(".chart-value").innerHTML = last
      ? `${fmtVal(last.value, d.unit, dec)}${d.events.length ? `<small>${d.events.length} anomal${d.events.length === 1 ? "y" : "ies"}</small>` : ""}`
      : "";
    const accent = css("--accent"), accent2 = css("--accent-2"), danger = css("--danger");
    const text3 = css("--text-3"), border = css("--border"), surface = css("--surface-strong"), text = css("--text");
    const ctx = card.querySelector("canvas").getContext("2d");
    const h = card.querySelector(".chart-box").clientHeight || 190;
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, accent2 + "40");
    grad.addColorStop(1, accent2 + "00");

    const xs = d.points.map((p) => p.ts);
    const fc = d.forecast;
    const xMin = xs[0], xMax = fc.length ? fc[fc.length - 1].ts : xs[xs.length - 1];
    const datasets = [
      { label: "value", data: d.points.map((p) => ({ x: p.ts, y: p.value })), borderColor: accent2, backgroundColor: grad,
        fill: true, borderWidth: 1.6, pointRadius: 0, tension: 0.25, order: 3 },
      { label: "expected", data: d.points.map((p) => ({ x: p.ts, y: p.expected })), borderColor: text3, borderDash: [3, 3],
        borderWidth: 1, pointRadius: 0, spanGaps: false, order: 4 },
      { label: "anomaly", type: "scatter", data: d.anomaly_points.map((p) => ({ x: p.ts, y: p.value })),
        backgroundColor: danger, borderColor: surface, borderWidth: 1, pointRadius: 3.2, pointHoverRadius: 5, order: 1 },
    ];
    if (fc.length) {
      const join = last ? [{ x: last.ts, y: last.value }] : [];
      datasets.push(
        { label: "band high", data: join.concat(fc.map((p) => ({ x: p.ts, y: p.hi }))), borderWidth: 0, pointRadius: 0,
          fill: "+1", backgroundColor: accent + "22", order: 6 },
        { label: "band low", data: join.concat(fc.map((p) => ({ x: p.ts, y: p.lo }))), borderWidth: 0, pointRadius: 0, fill: false, order: 6 },
        { label: "forecast", data: join.concat(fc.map((p) => ({ x: p.ts, y: p.value }))), borderColor: accent, borderDash: [6, 4],
          borderWidth: 2, pointRadius: 0, order: 2 },
      );
    }
    if (d.threshold !== null && d.threshold !== undefined) {
      const maxSeen = Math.max(...d.points.map((p) => p.value), ...fc.map((p) => p.hi));
      if (fc.length || maxSeen >= d.threshold * 0.6) {
        datasets.push({ label: "threshold", data: [{ x: xMin, y: d.threshold }, { x: xMax, y: d.threshold }], borderColor: danger,
          borderDash: [4, 4], borderWidth: 1.2, pointRadius: 0, order: 5 });
      }
    }

    const span = xMax - xMin;
    const options = {
      responsive: true, maintainAspectRatio: false, animation: motion ? { duration: 500 } : false, parsing: true, normalized: true,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: surface, titleColor: text, bodyColor: text, borderColor: css("--border-strong"), borderWidth: 1,
          padding: 10, cornerRadius: 10, displayColors: false,
          filter: (item) => !["band high", "band low"].includes(item.dataset.label) && item.raw.y !== null,
          callbacks: {
            title: (items) => items.length ? fmtTime(items[0].raw.x, true) : "",
            label: (item) => `${item.dataset.label}: ${fmtVal(item.raw.y, d.unit, dec)}`,
          },
        },
      },
      scales: {
        x: { type: "linear", min: xMin, max: xMax, grid: { display: false }, border: { color: border },
             ticks: { color: text3, maxTicksLimit: 6, maxRotation: 0, font: { size: 11 }, callback: (v) => fmtTime(v, span > 86400 * 1.5) } },
        y: { grid: { color: border }, border: { display: false }, ticks: { color: text3, maxTicksLimit: 5, font: { size: 11 } },
             suggestedMin: 0 },
      },
    };
    if (state.charts[m]) state.charts[m].destroy();
    state.charts[m] = new Chart(ctx, { type: "line", data: { datasets }, options });
    state.charts[m]._data = d;
  }

  async function loadCharts() {
    const { metrics } = await api("/api/metrics");
    const names = metrics.map((x) => x.metric).sort((a, b) => {
      const ia = METRIC_ORDER.indexOf(a), ib = METRIC_ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
    });
    // predicted metrics first so their wide cards lead
    names.sort((a, b) => Number(metrics.find((x) => x.metric === b).has_prediction) - Number(metrics.find((x) => x.metric === a).has_prediction));
    const series = await Promise.all(names.map((m) => api(`/api/series?metric=${encodeURIComponent(m)}&hours=${state.hours}`)));
    const box = $("#charts");
    names.forEach((m) => { const c = $(`#chart-${m}`); if (c) box.appendChild(c); });
    $$(".chart-card", box).forEach((c) => { if (!names.includes(c.id.slice(6))) { c.remove(); } });
    series.forEach((d, i) => drawChart(names[i], d));
    names.forEach((m) => box.appendChild($(`#chart-${m}`)));
  }

  // ------------------------------------------------------------------ alerts feed
  const KIND_LABEL = { anomaly: "anomaly", prediction: "forecast", log_new: "new pattern", log_spike: "log spike" };

  function renderFeed(a) {
    const list = state.filter === "open" ? a.alerts.filter((x) => x.active) : a.alerts.slice(0, 80);
    const feed = $("#feed");
    if (!list.length) {
      feed.innerHTML = `<li><span></span><span class="muted">No ${state.filter === "open" ? "open " : ""}alerts.</span></li>`;
      return;
    }
    const fresh = [];
    feed.innerHTML = list.map((x) => {
      const isNew = !state.seenAlerts.has(x.id);
      if (isNew) fresh.push(x.id);
      const mono = x.kind === "log_new" || x.kind === "log_spike";
      return `<li data-id="${esc(x.id)}" class="${isNew && !state.firstLoad ? "fresh" : ""}">
        <span class="dot ${x.severity}"></span>
        <div><div class="feed-title">${esc(x.title)}</div>
          <div class="feed-detail ${mono ? "mono" : ""}">${esc(x.detail)}</div>
          <div class="feed-meta"><span class="pill ${x.severity}">${esc(KIND_LABEL[x.kind] || x.kind)}</span>
            <span>${x.kind === "prediction" ? "now" : ago(x.ts, a.now)}</span>${x.active ? "" : '<span class="closed">· resolved</span>'}</div></div>
      </li>`;
    }).join("");
    fresh.forEach((id) => state.seenAlerts.add(id));
    if (motion && !state.firstLoad) gsap.from($$("li.fresh", feed).slice(0, 25), { opacity: 0, y: 8, stagger: 0.03, duration: 0.3 });
  }

  // ------------------------------------------------------------------ templates
  function tplHtml(t) {
    return esc(t).replace(/&lt;(\*|NUM|IP|HEX|UUID)&gt;/g, '<span class="var">&lt;$1&gt;</span>');
  }

  async function loadTemplates() {
    const { templates } = await api(`/api/templates?limit=20&sort=${state.sort}`);
    const body = $("#templates tbody");
    body.innerHTML = templates.map((t) => {
      const flags = t.flags.map((f) => `<span class="pill ${f === "spiking" ? "critical" : "warning"}">${esc(f)}</span>`).join(" ");
      const up = t.last_hour >= 5 && t.ratio >= 3;
      return `<tr class="row" tabindex="0" data-id="${t.id}" aria-expanded="false">
        <td class="id">${t.id}</td><td class="tpl">${tplHtml(t.template)}</td><td><span class="lvl ${esc(t.level)}">${esc(t.level)}</span></td>
        <td class="num ${up ? "ratio-up" : ""}">${t.last_hour}${up ? ` <small>x${t.ratio.toFixed(0)}</small>` : ""}</td>
        <td class="num">${t.baseline_per_hour.toFixed(1)}</td><td class="num">${t.count.toLocaleString()}</td><td>${flags}</td></tr>`;
    }).join("") || `<tr><td colspan="7" class="muted">No log lines yet.</td></tr>`;
    if (state.openTpl) {
      const row = $(`tr.row[data-id="${state.openTpl}"]`, body);
      if (row) toggleLines(row, true);
    }
  }

  async function toggleLines(row, forceOpen = false) {
    const next = row.nextElementSibling;
    if (next && next.classList.contains("lines")) {
      if (forceOpen) return;
      next.remove(); row.setAttribute("aria-expanded", "false"); state.openTpl = null; return;
    }
    $$("#templates tr.lines").forEach((r) => { r.previousElementSibling.setAttribute("aria-expanded", "false"); r.remove(); });
    const id = row.dataset.id;
    state.openTpl = id;
    row.setAttribute("aria-expanded", "true");
    const tr = document.createElement("tr");
    tr.className = "lines";
    tr.innerHTML = `<td></td><td colspan="6"><pre>loading...</pre></td>`;
    row.after(tr);
    try {
      const { logs } = await api(`/api/logs?template_id=${id}&limit=12`);
      tr.querySelector("pre").textContent = logs.map((l) => `${new Date(l.ts * 1000).toISOString().slice(5, 19).replace("T", " ")}  ${l.level.padEnd(5)}  ${l.message}`).join("\n") || "no lines";
      if (motion) gsap.from(tr, { opacity: 0, duration: 0.2 });
    } catch (e) { tr.querySelector("pre").textContent = e.message; }
  }

  $("#templates tbody").addEventListener("click", (e) => { const row = e.target.closest("tr.row"); if (row) toggleLines(row); });
  $("#templates tbody").addEventListener("keydown", (e) => {
    const row = e.target.closest("tr.row");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); toggleLines(row); }
  });

  // ------------------------------------------------------------------ summary
  function renderSummary(s) {
    const src = $("#sum-source");
    src.textContent = s.source === "claude" ? "Claude" : "rules";
    src.className = "pill " + (s.source === "claude" ? "claude" : "");
    const sev = s.severity === "ok" ? "ok" : s.severity;
    const html = `<h3><span class="pill ${sev}">${esc(s.severity)}</span>${esc(s.title)}</h3>
      <p>${esc(s.summary)}</p>
      ${s.actions && s.actions.length ? `<ol>${s.actions.map((x) => `<li>${esc(x)}</li>`).join("")}</ol>` : ""}
      ${s.fallback_reason ? `<div class="note">Claude unavailable (${esc(s.fallback_reason)}); showing the rule-based summary.</div>` : ""}
      ${s.source === "rules" && !s.fallback_reason && state.config && state.config.llm === "rules" ? `<div class="note">Deterministic summary. Set ANTHROPIC_API_KEY to have Claude write it.</div>` : ""}`;
    const body = $("#summary-body");
    body.innerHTML = html;
    if (motion) gsap.from(body.children, { opacity: 0, y: 6, stagger: 0.06, duration: 0.3, ease: "power2.out" });
  }

  $("#summarize").addEventListener("click", async () => {
    const btn = $("#summarize");
    btn.disabled = true; btn.textContent = "Summarizing...";
    $("#summary-body").innerHTML = '<div class="thinking" aria-label="working"></div>';
    try { renderSummary(await api("/api/summary", { method: "POST" })); }
    catch (e) { $("#summary-body").innerHTML = `<p class="muted">${esc(e.message)}</p>`; toast(e.message, true); }
    finally { btn.disabled = false; btn.textContent = "Summarize now"; }
  });

  // ------------------------------------------------------------------ controls
  $$(".seg [data-hours]").forEach((b) => b.addEventListener("click", () => {
    $$(".seg [data-hours]").forEach((x) => x.classList.toggle("on", x === b));
    state.hours = Number(b.dataset.hours);
    loadCharts().catch((e) => toast(e.message, true));
  }));
  $$(".seg [data-filter]").forEach((b) => b.addEventListener("click", () => {
    $$(".seg [data-filter]").forEach((x) => x.classList.toggle("on", x === b));
    state.filter = b.dataset.filter;
    if (state.analysis) renderFeed(state.analysis);
  }));
  $$(".seg [data-sort]").forEach((b) => b.addEventListener("click", () => {
    $$(".seg [data-sort]").forEach((x) => x.classList.toggle("on", x === b));
    state.sort = b.dataset.sort;
    loadTemplates().catch((e) => toast(e.message, true));
  }));
  $("#refresh").addEventListener("click", () => refresh(true));
  document.addEventListener("keydown", (e) => {
    if (e.key.toLowerCase() === "r" && !e.ctrlKey && !e.metaKey && !e.altKey && !/input|textarea/i.test(e.target.tagName)) refresh(true);
  });
  $("#reset-demo").addEventListener("click", async () => {
    const btn = $("#reset-demo");
    btn.disabled = true;
    try {
      const r = await api("/api/demo/reset", { method: "POST", body: JSON.stringify({ seed: Math.floor(Math.random() * 1000) }) });
      toast(`Generated ${r.metric_points.toLocaleString()} metric points and ${r.log_lines.toLocaleString()} log lines`);
      state.seenAlerts.clear(); state.firstLoad = true;
      await refresh();
    } catch (e) { toast(e.message, true); }
    finally { btn.disabled = false; }
  });

  // ------------------------------------------------------------------ main
  async function refresh(manual = false) {
    try {
      const a = await api("/api/analysis");
      state.analysis = a;
      renderKpis(a);
      renderPredictions(a);
      renderFeed(a);
      await Promise.all([loadCharts(), loadTemplates()]);
      if (state.firstLoad) {
        renderSummary(await api("/api/summary", { method: "POST" }).catch(() => ({ title: "Summary unavailable", severity: "info", summary: "", actions: [], source: "rules" })));
      }
      if (manual) toast("Refreshed");
      if (state.firstLoad && motion) {
        gsap.from("#topbar", { y: -12, opacity: 0, duration: 0.5, ease: "power3.out" });
        gsap.from(".hero, .kpi", { y: 16, opacity: 0, duration: 0.6, stagger: 0.06, ease: "power3.out" });
        gsap.from(".pred, .chart-card, .col-side .panel", { y: 16, opacity: 0, duration: 0.6, stagger: 0.05, delay: 0.15, ease: "power3.out" });
        gsap.to(".glow-a", { x: 80, y: 40, duration: 26, yoyo: true, repeat: -1, ease: "sine.inOut" });
        gsap.to(".glow-b", { x: -70, y: 30, duration: 30, yoyo: true, repeat: -1, ease: "sine.inOut" });
      }
      state.firstLoad = false;
    } catch (e) {
      toast(`Could not load data: ${e.message}`, true);
    }
  }

  (async () => {
    try {
      state.config = await api("/api/config");
      $("#version").textContent = "v" + state.config.version;
      $("#data-pill").hidden = !state.config.demo;
      $("#sum-source").textContent = state.config.llm === "claude" ? "Claude" : "rules";
    } catch (e) { /* refresh() reports errors */ }
    await refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 60_000);
  })();
})();
