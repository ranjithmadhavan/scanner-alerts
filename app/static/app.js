// Toasts triggered by the server via the HX-Trigger header: {"toast": {message, kind}}
document.body.addEventListener("toast", (e) => {
  const { message, kind } = e.detail;
  const el = document.createElement("div");
  el.className =
    "toast-in pointer-events-auto flex items-center gap-3 rounded-2xl px-4 py-3 text-[15px] font-medium shadow-soft " +
    (kind === "error" ? "bg-fall-50 text-fall border border-fall/20" : "bg-ink text-white");
  el.textContent = message;
  document.getElementById("toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .3s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 300);
  }, Math.max(kind === "error" ? 5000 : 2800, message.length * 60)); // long messages stay up long enough to read
});

// Mobile sidebar
const sidebar = document.getElementById("sidebar");
const scrim = document.getElementById("scrim");
function setMenu(open) {
  sidebar?.classList.toggle("-translate-x-full", !open);
  scrim?.classList.toggle("hidden", !open);
}
document.getElementById("menu")?.addEventListener("click", () => setMenu(true));
scrim?.addEventListener("click", () => setMenu(false));

// Level forms (New alert, and the Edit panel loaded later): rows of condition + price.
// The candle timeframe only matters when some level uses a "closes" condition.
(() => {
  function sync(form) {
    const extras = form.querySelector("[data-extra-levels]");
    const tf = form.querySelector("[data-timeframe-field]");
    const addBtn = form.querySelector("[data-add-level]");
    const chosen = [...form.querySelectorAll('[name="condition"]:checked:enabled, [name="extra_condition"]:enabled')].map((el) => el.value);
    if (tf) tf.hidden = !chosen.some((c) => c.startsWith("close_"));
    if (addBtn) addBtn.hidden = extras.children.length >= Number(addBtn.dataset.max);
  }
  const syncAll = () => document.querySelectorAll(".level-form").forEach(sync);

  // New alert form: price levels or fractals. The other kind's fields are hidden and switched off,
  // so they are neither required nor sent.
  function setKind(form) {
    const kind = form.querySelector('[name="kind"]:checked')?.value;
    if (!kind) return;
    form.querySelectorAll("[data-kind-section]").forEach((section) => {
      const off = section.dataset.kindSection !== kind;
      section.classList.toggle("section-in", !off && section.hidden && form.dataset.kindSet === "1");
      section.hidden = off;
      section.querySelectorAll("input, select, textarea").forEach((el) => { el.disabled = off; });
    });
    form.querySelectorAll("[data-label-price]").forEach((b) => { b.textContent = b.dataset[kind === "fractal" ? "labelFractal" : "labelPrice"]; });
    form.dataset.kindSet = "1";
    sync(form);
  }

  document.addEventListener("change", (e) => {
    const form = e.target.closest(".level-form");
    if (form && e.target.name === "kind") setKind(form);
    else if (form && (e.target.name === "condition" || e.target.name === "extra_condition")) sync(form);
  });
  document.addEventListener("levels:changed", (e) => sync(e.target)); // the form was cleared after saving

  document.addEventListener("click", (e) => {
    const form = e.target.closest(".level-form");
    if (!form) return;
    const extras = form.querySelector("[data-extra-levels]");
    if (e.target.closest("[data-add-level]")) {
      const row = document.getElementById("extra-level-row").content.firstElementChild.cloneNode(true);
      // Start from the condition already in use: the form's main choice, or the last row's.
      const last = [...form.querySelectorAll('[name="condition"]:checked, select[name="extra_condition"]')].pop();
      if (last) row.querySelector("select").value = last.value;
      extras.appendChild(row);
      row.querySelector("input.level-input").focus();
    } else if (e.target.closest("[data-remove-level]")) {
      e.target.closest(".extra-level").remove();
      form.dispatchEvent(new CustomEvent("levels:input")); // redraw the chart lines without this level
    } else return;
    sync(form);
  });
  document.addEventListener("htmx:afterSwap", syncAll); // the Edit panel arrives by htmx

  // Fractal alerts: the trigger candle has to be shorter than the fractal candles and divide into them.
  function syncTrigger(form) {
    const fractal = form.querySelector('[name="fractal_timeframe"]');
    const trigger = form.querySelector('[name="confirm_timeframe"]');
    if (!fractal || !trigger) return;
    const whole = Number(fractal.selectedOptions[0].dataset.min);
    [...trigger.options].forEach((o) => {
      const part = Number(o.dataset.min);
      if (o.value) o.disabled = !(part < whole && (fractal.value === "1d" || whole % part === 0));
    });
    if (trigger.selectedOptions[0].disabled) trigger.value = "";
  }
  const syncTriggers = () => document.querySelectorAll("form").forEach(syncTrigger);
  document.addEventListener("change", (e) => { if (e.target.name === "fractal_timeframe") syncTrigger(e.target.form); });
  document.addEventListener("htmx:afterSwap", syncTriggers);
  syncTriggers();
  const editor = document.getElementById("alert-editor");
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && editor?.children.length) editor.innerHTML = "";
  });
  // The editor is a dialog: focus moves into it when it opens and back to its button when it closes.
  if (editor) {
    let opener = null;
    document.addEventListener("click", (e) => { opener = e.target.closest('[hx-target="#alert-editor"]')?.getAttribute("hx-get") || opener; });
    new MutationObserver(() => {
      if (editor.children.length) editor.querySelector("select, input:not([type=hidden]), button")?.focus();
      else document.querySelector(`.layout-list [hx-get="${opener}"], [hx-get="${opener}"]`)?.focus();
    }).observe(editor, { childList: true });
  }
  document.querySelectorAll(".level-form").forEach(setKind);
  syncAll();
})();

// Copy-to-clipboard buttons
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-copy]");
  if (!btn) return;
  await navigator.clipboard.writeText(btn.dataset.copy);
  document.body.dispatchEvent(new CustomEvent("toast", { detail: { message: "Copied", kind: "success" } }));
});

// Stock search combobox: arrow keys move, Enter picks, Esc closes, click picks.
(() => {
  const input = document.getElementById("symbol");
  const results = document.getElementById("symbol-results");
  if (!input || !results) return;
  let active = -1;

  const options = () => [...results.querySelectorAll('[role="option"]')];
  const isOpen = () => results.children.length > 0;

  function setActive(i) {
    const opts = options();
    if (!opts.length) return;
    active = (i + opts.length) % opts.length;
    opts.forEach((o, n) => o.setAttribute("aria-selected", n === active ? "true" : "false"));
    opts[active].scrollIntoView({ block: "nearest" });
    input.setAttribute("aria-activedescendant", opts[active].id);
  }

  function close() {
    results.innerHTML = "";
    active = -1;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
  }

  function pick(opt) {
    input.value = opt.dataset.symbol;
    close();
    document.dispatchEvent(new CustomEvent("symbol:picked"));
    document.getElementById("level")?.focus();
  }

  // A late response after the user already picked (focus moved on) shouldn't reopen the list.
  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target === results && document.activeElement !== input) e.detail.shouldSwap = false;
  });

  // New results arrived: open the list and highlight the first match.
  document.addEventListener("htmx:afterSwap", (e) => {
    if (e.detail.target !== results) return;
    active = -1;
    input.setAttribute("aria-expanded", isOpen() ? "true" : "false");
    if (options().length) setActive(0);
  });

  input.addEventListener("keydown", (e) => {
    if (!isOpen()) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(active + 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(active - 1); }
    else if (e.key === "Enter") {
      const opt = options()[active];
      if (opt) { e.preventDefault(); pick(opt); }
    } else if (e.key === "Escape") { e.preventDefault(); close(); }
  });

  // mousedown (not click) so it fires before the input loses focus
  results.addEventListener("mousedown", (e) => {
    const opt = e.target.closest('[role="option"]');
    if (opt) { e.preventDefault(); pick(opt); }
  });

  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("input", () => { if (!input.value.trim()) close(); });
})();

// ---- Price under the Stock box ----------------------------------------------
(() => {
  const input = document.getElementById("symbol");
  const quote = document.getElementById("quote");
  if (!input || !quote || !window.htmx) return;
  let shown = "";
  function load() {
    const symbol = input.value.trim().toUpperCase();
    if (symbol === shown) return;
    shown = symbol;
    if (!symbol) { quote.innerHTML = ""; return; }
    htmx.ajax("GET", "/alerts/quote?symbol=" + encodeURIComponent(symbol), { target: "#quote", swap: "innerHTML" });
  }
  // Picked from the list (value set by script) or typed and left the box.
  document.addEventListener("symbol:picked", load);
  input.addEventListener("change", load);
  input.addEventListener("input", () => { if (!input.value.trim()) load(); });
})();

// ---- Charts (TradingView Lightweight Charts, loaded on first use) ---------------------
const CHART_LIB = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js";
function loadChartLib() {
  if (window.LightweightCharts) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = CHART_LIB; s.onload = resolve; s.onerror = () => reject(new Error("Couldn't load the chart library."));
    document.head.appendChild(s);
  });
}
const chartRupees = (p) => "₹" + p.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const chartLook = (L) => ({
  autoSize: true,
  layout: { background: { color: "#FFFFFF" }, textColor: "#5F7476", fontFamily: "Figtree, system-ui, sans-serif" },
  grid: { vertLines: { color: "#F4EFE8" }, horzLines: { color: "#F4EFE8" } },
  rightPriceScale: { borderColor: "#ECE6DC" },
  timeScale: { borderColor: "#ECE6DC" },
  crosshair: { mode: L.CrosshairMode.Normal },
  localization: { priceFormatter: chartRupees },
});
const candleLook = { upColor: "#2E8B57", downColor: "#C2475A", borderVisible: false, wickUpColor: "#2E8B57", wickDownColor: "#C2475A" };

// ---- Fractal backtest: trigger candles with every signal marked ------------------------
(() => {
  let chart, below, series, data, lines = [], position = new Map();
  const RSI_PERIOD = 14;

  // Wilder's RSI on the candles' closes: the first value needs RSI_PERIOD changes, then it is smoothed.
  function rsi(candles) {
    const out = [];
    let gain = 0, loss = 0;
    for (let i = 1; i < candles.length; i++) {
      const change = candles[i].close - candles[i - 1].close;
      const up = Math.max(change, 0), down = Math.max(-change, 0);
      if (i <= RSI_PERIOD) {
        gain += up / RSI_PERIOD; loss += down / RSI_PERIOD;
        if (i < RSI_PERIOD) continue;
      } else {
        gain = (gain * (RSI_PERIOD - 1) + up) / RSI_PERIOD;
        loss = (loss * (RSI_PERIOD - 1) + down) / RSI_PERIOD;
      }
      out.push({ time: candles[i].time, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) });
    }
    return out;
  }

  function focus(n) {
    const signal = data?.signals[n];
    if (!signal || !series) return;
    lines.forEach((line) => series.removePriceLine(line));
    lines = [series.createPriceLine({ price: signal.level, color: "#E9A23B", lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: "Fractal " + signal.side })];
    if (signal.target !== null) {
      lines.push(series.createPriceLine({ price: signal.target, color: "#0F5E5C", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Target" }));
    }
    lines.push(series.createPriceLine({ price: signal.stop, color: "#C2475A", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "SL" }));
    const at = position.get(String(signal.time));
    chart.timeScale().setVisibleLogicalRange({ from: at - 60, to: at + 40 });
    document.querySelectorAll("[data-bt-signal]").forEach((row) => row.toggleAttribute("data-selected", Number(row.dataset.btSignal) === n));
  }

  async function build(root) {
    const source = root.querySelector("#bt-data");
    const box = root.querySelector("#bt-chart");
    if (!source || !box) return;
    data = JSON.parse(source.textContent);
    try { await loadChartLib(); } catch (e) { root.querySelector("#bt-chart-note").textContent = e.message; return; }
    const L = window.LightweightCharts;
    chart?.remove();
    chart = L.createChart(box, chartLook(L));
    chart.applyOptions({ timeScale: { timeVisible: !data.daily, secondsVisible: false } });
    series = chart.addCandlestickSeries(candleLook);
    series.setData(data.candles);

    // RSI in its own small chart underneath, so it has a proper 0-100 axis. The two share one time axis
    // (drawn under the RSI) and scroll and zoom together.
    const gutter = { rightPriceScale: { borderColor: "#ECE6DC", minimumWidth: box.clientWidth < 520 ? 78 : 96 } };  // same width, so bars line up
    chart.applyOptions({ ...gutter, timeScale: { visible: false } });
    below?.remove();
    below = L.createChart(root.querySelector("#bt-rsi-chart"), chartLook(L));
    const marked = (v) => (Math.abs(v - 70) < 0.01 || Math.abs(v - 30) < 0.01 ? v.toFixed(0) : "");  // label only the two lines
    below.applyOptions({ ...gutter, localization: { priceFormatter: marked }, layout: { attributionLogo: false },
      crosshair: { horzLine: { labelVisible: false } },
      timeScale: { timeVisible: !data.daily, secondsVisible: false } });
    const strength = below.addLineSeries({
      color: "#0F5E5C", lineWidth: 2, lastValueVisible: false, priceLineVisible: false, crosshairMarkerRadius: 3,
      autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    });
    below.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.06 } });  // 0 to 100 fills the panel
    const values = rsi(data.candles);
    // Candles before the first RSI value still get an (empty) point, so both charts count bars the same way.
    strength.setData([...data.candles.slice(0, data.candles.length - values.length).map((c) => ({ time: c.time })), ...values]);
    [70, 30].forEach((price) => strength.createPriceLine({ price, color: "#B8AFA2", lineWidth: 1, lineStyle: 2, axisLabelVisible: true }));
    // Long price labels can make the candle axis wider than the minimum; give the RSI axis the same width
    // so the two plots stay bar-for-bar aligned.
    requestAnimationFrame(() => {
      const width = Math.max(chart.priceScale("right").width(), below.priceScale("right").width());
      [chart, below].forEach((c) => c.applyOptions({ rightPriceScale: { minimumWidth: width } }));
    });
    const follow = (from, to) => from.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (range) to.timeScale().setVisibleLogicalRange(range);
    });
    follow(chart, below);
    follow(below, chart);
    const byTime = new Map(values.map((v) => [String(v.time), v.value]));
    const readout = root.querySelector("#bt-rsi");
    const show = (v) => { readout.textContent = `RSI ${RSI_PERIOD}` + (v === undefined ? "" : `: ${v.toFixed(1)}`); };
    const onMove = (move) => show(move.time === undefined ? values.at(-1)?.value : byTime.get(String(move.time)));
    show(values.at(-1)?.value);
    chart.subscribeCrosshairMove(onMove);
    below.subscribeCrosshairMove(onMove);
    series.setMarkers(data.signals.map((s) => ({
      time: s.time, text: s.label,
      position: s.signal === "sell" ? "aboveBar" : "belowBar",
      shape: s.signal === "sell" ? "arrowDown" : "arrowUp",
      color: s.signal === "sell" ? "#C2475A" : "#2E8B57",
    })));
    position = new Map(data.candles.map((c, i) => [String(c.time), i]));
    lines = [];
    if (data.signals.length) focus(data.signals.length - 1); else chart.timeScale().fitContent();
  }

  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-bt-signal]");
    if (!row) return;
    focus(Number(row.dataset.btSignal));
    document.getElementById("bt-chart")?.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  document.addEventListener("keydown", (e) => {
    const row = e.target.matches?.("[data-bt-signal]") ? e.target : null;
    if (!row || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    focus(Number(row.dataset.btSignal));
  });
  document.addEventListener("htmx:afterSwap", (e) => { if (e.detail.target.id === "sim-result") build(e.detail.target); });
})();

// ---- Nifty OI: the day's change in call and put OI, with the index ----------------------------
(() => {
  const source = document.getElementById("oi-data");
  const box = document.getElementById("oi-chart");
  if (!source || !box) return;
  const data = JSON.parse(source.textContent);
  if (!data.points.length) return;
  loadChartLib().then(() => {
    const L = window.LightweightCharts;
    const chart = L.createChart(box, chartLook(L));
    chart.applyOptions({ localization: { priceFormatter: (v) => v.toFixed(1) }, leftPriceScale: { visible: true, borderColor: "#ECE6DC" },
      timeScale: { timeVisible: true, secondsVisible: false } });
    const line = (color, scale, title) => chart.addLineSeries({ color, lineWidth: 2, priceScaleId: scale, title,
      priceFormat: scale === "right" ? { type: "custom", formatter: (v) => v.toLocaleString("en-IN", { maximumFractionDigits: 0 }) } : { type: "custom", formatter: (v) => v.toFixed(1) + "L" } });
    const calls = line("#C2475A", "left", "Calls");
    const puts = line("#2E8B57", "left", "Puts");
    const spot = line("#16373A", "right", "Nifty");
    calls.setData(data.points.map((p) => ({ time: p.time, value: p.ce })));
    puts.setData(data.points.map((p) => ({ time: p.time, value: p.pe })));
    spot.setData(data.points.map((p) => ({ time: p.time, value: p.spot })));
    const chosen = data.points.find((p) => p.slot === data.chosen);
    if (chosen) spot.setMarkers([{ time: chosen.time, position: "aboveBar", shape: "circle", color: "#E9A23B", text: chosen.label }]);
    chart.timeScale().fitContent();
    // Clicking a moment on the chart opens that snapshot.
    chart.subscribeClick((e) => {
      if (e.time === undefined) return;
      const p = data.points.reduce((best, q) => Math.abs(q.time - e.time) < Math.abs(best.time - e.time) ? q : best);
      location.href = `/oi?day=${encodeURIComponent(data.day)}&at=${encodeURIComponent(p.slot)}`;
    });
  }).catch(() => {});
})();

// ---- Chart side panel ---------------------------------------------------------
const StockChart = (() => {
  const drawer = document.getElementById("chart-drawer");
  if (!drawer) return null;
  const box = document.getElementById("chart");
  const msg = document.getElementById("chart-msg");
  const levelNote = document.getElementById("chart-level");
  const hitsBox = document.getElementById("chart-hits");
  let chart, series, levelLines = [], lastFocus, request = 0;
  // levels: [{price, title}]. Only the ones nearest price stretch the axis, so twenty fractal levels
  // don't flatten the candles; the rest are drawn when they come into view.
  const state = { symbol: "", range: "5D", interval: "", levels: [], framed: [], last: null };
  const intervalPick = document.getElementById("chart-interval");
  const rupees = chartRupees;
  const loadLib = loadChartLib;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function build() {
    const L = window.LightweightCharts;
    chart = L.createChart(box, chartLook(L));
    series = chart.addCandlestickSeries({
      ...candleLook,
      autoscaleInfoProvider: (original) => {
        const res = original();
        if (res && state.framed.length) {
          res.priceRange.minValue = Math.min(res.priceRange.minValue, ...state.framed);
          res.priceRange.maxValue = Math.max(res.priceRange.maxValue, ...state.framed);
        }
        return res;
      },
    });
  }

  const parseLevels = (values) => [...new Set(values.map(parseFloat).filter((v) => v > 0))];
  const nearest = (levels, last) => ({
    above: levels.filter((l) => l.price > last).sort((a, b) => a.price - b.price)[0],
    below: levels.filter((l) => l.price < last).sort((a, b) => b.price - a.price)[0],
  });

  function frame() {
    // A handful of levels are all kept in view; with more, just the nearest one each side of price.
    const levels = state.levels;
    if (levels.length <= 4 || state.last === null) { state.framed = levels.map((l) => l.price); return; }
    const { above, below } = nearest(levels, state.last);
    state.framed = [above, below].filter(Boolean).map((l) => l.price);
  }

  function setLevels(levels) {
    state.levels = levels;
    frame();
    if (series) levelLines.forEach((line) => series.removePriceLine(line));
    levelLines = [];
    levelNote.hidden = !levels.length;
    if (!levels.length) { chart?.priceScale("right").applyOptions({ autoScale: true }); return; }
    levelNote.lastElementChild.textContent = levels.length > 3 ? `${levels.length} levels on watch`
      : (levels.length > 1 ? "Your levels " : "Your level ") + levels.map((l) => rupees(l.price)).join(", ");
    if (series) {
      levelLines = levels.map((l) => series.createPriceLine({
        price: l.price, color: "#E9A23B", lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: l.title,
      }));
      chart.priceScale("right").applyOptions({ autoScale: true }); // re-fit to include the levels
    }
  }

  function showHits(hits) {
    series.setMarkers(hits.map((h) => ({
      time: h.time, text: h.label, position: h.signal === "sell" ? "aboveBar" : "belowBar",
      shape: h.signal === "sell" ? "arrowDown" : "arrowUp", color: h.signal === "sell" ? "#C2475A" : "#2E8B57",
    })));
    const parts = [];
    if (hits.length) {
      parts.push(`<p class="mb-1.5 font-semibold text-ink">Recent hits</p><ul class="space-y-1">` + hits.slice().reverse().map((h) =>
        `<li class="flex flex-wrap items-baseline gap-x-2"><span class="num w-36 shrink-0 text-ink-soft">${esc(h.at)}</span>` +
        `<span class="rounded-full px-1.5 text-xs font-semibold ${h.signal === "sell" ? "bg-fall-50 text-fall" : "bg-rise-50 text-rise"}">Potential ${h.signal}</span>` +
        `<span class="min-w-0">${esc(h.summary)} <span class="num text-ink-soft">at ${rupees(h.price)}</span></span></li>`).join("") + "</ul>");
    } else if (state.levels.length) {
      parts.push(`<p class="text-ink-soft">Nothing has fired here in this range.</p>`);
    }
    if (state.levels.length && state.last !== null) {
      const { above, below } = nearest(state.levels, state.last);
      const say = (l, side) => l ? `${esc(l.title)} ${rupees(l.price)} ${side}, ${(Math.abs(l.price - state.last) / state.last * 100).toFixed(2)}% away` : "";
      const next = [say(above, "above"), say(below, "below")].filter(Boolean).join(" · ");
      if (next) parts.push(`<p class="mt-2 text-ink-soft"><span class="font-semibold text-ink">Next:</span> ${next}</p>`);
    }
    hitsBox.innerHTML = parts.join("");
    hitsBox.hidden = !parts.length;
  }

  async function load() {
    const mine = ++request;
    drawer.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-selected", b.dataset.range === state.range));
    msg.textContent = "Loading " + state.symbol + "…";
    try {
      await loadLib();
      const r = await fetch(`/alerts/chart?symbol=${encodeURIComponent(state.symbol)}&range=${state.range}&interval=${state.interval}`);
      const data = await r.json();
      if (mine !== request) return; // a newer request superseded this one
      if (!r.ok) throw new Error(data.error || "Couldn't load the chart.");
      if (!chart) build();
      document.getElementById("chart-title").textContent = data.symbol;
      document.getElementById("chart-name").textContent = data.name || "";
      chart.applyOptions({ timeScale: { timeVisible: !data.daily, secondsVisible: false } });
      series.setData(data.candles);
      state.last = data.candles.length ? data.candles[data.candles.length - 1].close : null;
      setLevels(state.levels);
      showHits(data.hits || []);
      chart.timeScale().fitContent();
      msg.textContent = data.candles.length ? "" : "Kite has no candles for this range.";
      if (data.trimmed && data.candles.length) {
        hitsBox.insertAdjacentHTML("afterbegin", `<p class="mb-2 text-[13px] text-marigold-dark">Kite serves only the last few weeks of ${esc(intervalPick.selectedOptions[0].textContent.toLowerCase())} candles, so this shows the most recent part of the range.</p>`);
        hitsBox.hidden = false;
      }
    } catch (e) {
      if (mine === request) msg.textContent = e.message;
    }
  }

  function open(symbol, levels, interval = "") {
    lastFocus = document.activeElement;
    state.symbol = symbol;
    state.interval = interval;
    if (intervalPick) intervalPick.value = interval;
    state.levels = levels;
    state.last = null;
    document.getElementById("chart-title").textContent = symbol;
    document.getElementById("chart-name").textContent = "";
    hitsBox.hidden = true;
    if (series) { series.setData([]); series.setMarkers([]); }
    drawer.hidden = false;
    requestAnimationFrame(() => drawer.classList.add("open"));
    document.body.style.overflow = "hidden";
    drawer.querySelector("[data-close].rounded-full")?.focus();
    load();
  }

  function close() {
    drawer.classList.remove("open");
    document.body.style.overflow = "";
    setTimeout(() => { drawer.hidden = true; }, 300);
    lastFocus?.focus();
  }

  drawer.addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) close();
    const tab = e.target.closest("[data-range]");
    if (tab && tab.dataset.range !== state.range) { state.range = tab.dataset.range; load(); }
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !drawer.hidden) close(); });
  intervalPick?.addEventListener("change", () => { state.interval = intervalPick.value; load(); });

  // Any [data-chart] button opens the panel. Lines come from data-lines (an alert row: what is on watch,
  // named), data-levels (plain prices) or the form's level fields while an alert is being set up.
  const asLines = (prices) => prices.map((price) => ({ price, title: "Level" }));
  const fieldLevels = (selector) => asLines(parseLevels([...document.querySelectorAll(selector)].map((el) => el.value)));
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-chart]");
    if (!btn) return;
    let lines = [];
    try { lines = btn.dataset.lines ? JSON.parse(btn.dataset.lines) : []; } catch {}
    if (!lines.length && btn.dataset.levels) lines = asLines(parseLevels(btn.dataset.levels.split(",")));
    if (!lines.length && btn.dataset.levelsFrom) lines = fieldLevels(btn.dataset.levelsFrom);
    open(btn.dataset.chart, lines, btn.dataset.interval || "");
  });

// Typing a level in the form (or removing a row) moves the lines live.
  const form = document.getElementById("new-alert");
  function fromForm() {
    const formSymbol = document.getElementById("symbol")?.value.trim().toUpperCase();
    if (!drawer.hidden && formSymbol === state.symbol) setLevels(fieldLevels("#new-alert .level-input:enabled"));
  }
  form?.addEventListener("input", (e) => { if (e.target.classList.contains("level-input")) fromForm(); });
  form?.addEventListener("levels:input", fromForm);

  return { open, close };
})();

// ---- Alert list: show what changed between one refresh and the next -------------------------
// The list is replaced wholesale every 30 seconds, so nothing would ever appear to move. Remember
// each alert's state just before the swap and, just after, let the difference show: the price dot
// glides, a changed price flashes, and a level that was hit sweeps its row.
(() => {
  let before = null;
  const rows = () => [...document.querySelectorAll("#alert-list .alert-row")];
  const counts = () => Object.fromEntries([...document.querySelectorAll("#alert-list [data-count]")]
    .map((el) => [el.dataset.count, Number(el.textContent)]));

  document.addEventListener("htmx:beforeSwap", (e) => {
    if (e.detail.target.id !== "alert-list") return;
    before = { counts: counts(), alerts: new Map(), prices: new Map() };
    document.querySelectorAll("#alert-list .price-group").forEach((g) => before.prices.set(g.dataset.group, g.dataset.price));
    rows().forEach((row) => before.alerts.set(row.dataset.alert, {
      ...row.dataset, dot: row.querySelector(".rail-dot")?.style.left }));
  });

  document.addEventListener("htmx:afterSettle", () => {
    if (!before || !document.getElementById("alert-list")) return;
    const was = before;
    before = null;
    rows().forEach((row) => {
      const old = was.alerts.get(row.dataset.alert);
      if (!old) return;
      const now = row.dataset;
      const hit = Number(now.hits) > Number(old.hits) || (now.last && now.last !== old.last)
        || (old.status === "active" && now.status === "triggered");
      if (hit) row.classList.add("just-hit");
      const dot = row.querySelector(".rail-dot");
      if (dot && old.dot && old.dot !== dot.style.left) {
        const to = dot.style.left;
        dot.style.transition = "none";
        dot.style.left = old.dot;
        dot.getBoundingClientRect();  // settle at the old spot, then let the transition carry it
        dot.style.transition = "";
        dot.style.left = to;
      }
    });
    document.querySelectorAll("#alert-list .price-group").forEach((g) => {
      const old = was.prices.get(g.dataset.group), now = g.dataset.price, price = g.querySelector(".price-now");
      if (price && old && now && Number(now) !== Number(old)) price.classList.add(Number(now) > Number(old) ? "tick-up" : "tick-down");
    });
    document.querySelectorAll('#alert-list [data-count="triggered"]').forEach((badge) => {
      if (Number(badge.textContent) > (was.counts.triggered ?? Infinity)) badge.classList.add("count-pulse");
    });
  });
})();

// ---- New alert form: folded away once there are alerts, opened from the page header ---------
(() => {
  const panel = document.getElementById("new-alert-panel");
  if (!panel) return;
  document.addEventListener("click", (e) => {
    if (!e.target.closest("[data-open-new-alert]")) return;
    panel.open = true;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
    document.getElementById("symbol")?.focus({ preventScroll: true });
  });
})();

// ---- Alerts layout: list or cards, remembered in this browser only -----------------
(() => {
  const KEY = "alertLayout";
  const root = document.documentElement;
  function syncButtons() {
    document.querySelectorAll("[data-layout-set]").forEach((b) =>
      b.setAttribute("aria-pressed", b.dataset.layoutSet === root.dataset.alertLayout ? "true" : "false"));
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-layout-set]");
    if (!btn) return;
    root.dataset.alertLayout = btn.dataset.layoutSet;
    try { localStorage.setItem(KEY, btn.dataset.layoutSet); } catch {}
    syncButtons();
  });
  syncButtons();
  document.addEventListener("htmx:afterSwap", syncButtons); // the toolbar is re-rendered on every refresh
})();
