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

// ---- Fractal bias: bullish vs bearish signals through the day ----------------------
(() => {
  const source = document.getElementById("bias-data");
  const box = document.getElementById("bias-chart");
  if (!source || !box) return;
  const data = JSON.parse(source.textContent);
  if (!data.points.length) return;
  loadChartLib().then(() => {
    const chart = window.LightweightCharts.createChart(box, chartLook(window.LightweightCharts));
    chart.applyOptions({ localization: { priceFormatter: (v) => v.toFixed(0) }, timeScale: { timeVisible: true, secondsVisible: false } });
    const line = (color, title) => chart.addLineSeries({ color, lineWidth: 2, title, priceFormat: { type: "custom", minMove: 1, formatter: (v) => v.toFixed(0) } });
    const bull = line("#2E8B57", "Bullish");
    const bear = line("#C2475A", "Bearish");
    bull.setData(data.points.map((p) => ({ time: p.time, value: p.bull })));
    bear.setData(data.points.map((p) => ({ time: p.time, value: p.bear })));
    const chosen = data.points.find((p) => p.slot === data.chosen);
    if (chosen) bull.setMarkers([{ time: chosen.time, position: "aboveBar", shape: "circle", color: "#E9A23B", text: chosen.label }]);
    chart.timeScale().fitContent();
    chart.subscribeClick((e) => {
      if (e.time === undefined) return;
      const p = data.points.reduce((best, q) => Math.abs(q.time - e.time) < Math.abs(best.time - e.time) ? q : best);
      location.href = `/bias?day=${encodeURIComponent(data.day)}&at=${encodeURIComponent(p.slot)}`;
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
  const state = { symbol: "", range: "5D", interval: "", levels: [], framed: [], last: null, landed: Promise.resolve() };
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
    // data-named-lines: the lines are a signal's own (fractal, stop, target), not levels the user set.
    levelNote.lastElementChild.textContent = "namedLines" in drawer.dataset ? levels.map((l) => `${l.title} ${rupees(l.price)}`).join(" · ")
      : levels.length > 3 ? `${levels.length} levels on watch`
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
      parts.push(`<p class="text-ink-soft">${"namedLines" in drawer.dataset ? "No signal on this stock in this range." : "Nothing has fired here in this range."}</p>`);
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
      const source = drawer.dataset.source || "/alerts/chart";
      const r = await fetch(`${source}${source.includes("?") ? "&" : "?"}symbol=${encodeURIComponent(state.symbol)}&range=${state.range}&interval=${state.interval}`);
      const data = await r.json().catch(() => ({ error: `The server couldn't draw this chart (HTTP ${r.status}). Try again in a moment.` }));
      await state.landed;
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
    clearTimeout(hideTimer);
    drawer.hidden = false;
    void drawer.offsetWidth; // start from the closed position, so the panel slides rather than appears
    drawer.classList.add("open");
    document.documentElement.classList.add("drawer-lock");
    // Drawing the chart while the panel is still moving makes the slide stutter: let it land first.
    state.landed = new Promise((done) => setTimeout(done, matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : 380));
    drawer.querySelector("[data-close].rounded-full")?.focus({ preventScroll: true });
    load();
  }

  let hideTimer;
  function close() {
    drawer.classList.remove("open");
    document.documentElement.classList.remove("drawer-lock");
    hideTimer = setTimeout(() => { drawer.hidden = true; }, 260);
    lastFocus?.focus({ preventScroll: true });
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

// ---- List or cards (alerts, fractal bias), remembered per page in this browser only -----------------
(() => {
  const root = document.documentElement;
  const key = (btn) => (btn.dataset.layoutFor || "alert") + "Layout"; // <html data-alert-layout>, data-bias-layout
  function syncButtons() {
    document.querySelectorAll("[data-layout-set]").forEach((b) =>
      b.setAttribute("aria-pressed", b.dataset.layoutSet === root.dataset[key(b)] ? "true" : "false"));
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-layout-set]");
    if (!btn) return;
    root.dataset[key(btn)] = btn.dataset.layoutSet;
    try { localStorage.setItem(key(btn), btn.dataset.layoutSet); } catch {}
    syncButtons();
  });
  syncButtons();
  document.addEventListener("htmx:afterSwap", syncButtons); // the toolbar is re-rendered on every refresh
})();

// ---- Sidebar: the selected block slides to the item clicked ----------------------------
// Pages are full loads, so without this the block would only jump once the next page arrives.
// Moving it on click (FLIP: re-parent it, then animate from where it was) makes the menu answer at
// once; the page change then matches it to the new page's block (view-transition-name: nav-pill).
document.addEventListener("click", (e) => {
  const link = e.target.closest(".nav-link");
  if (!link || link.hasAttribute("aria-current") || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const pill = document.querySelector(".nav-pill");
  if (!pill) return;
  const from = pill.getBoundingClientRect();
  pill.parentElement.removeAttribute("aria-current");
  link.setAttribute("aria-current", "page");
  link.prepend(pill);
  if (matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const to = pill.getBoundingClientRect();
  pill.animate([{ transform: `translate(${from.left - to.left}px, ${from.top - to.top}px)` }, { transform: "none" }],
    { duration: 320, easing: "cubic-bezier(.16,1,.3,1)" });
});

// ---- Strips of times (Nifty OI, Fractal bias): keep the chosen one in view ------------------
// A day of 5-minute counts is a long strip; open it scrolled to the moment being shown.
document.querySelectorAll('nav.overflow-x-auto [aria-current="true"]').forEach((pick) => {
  const strip = pick.parentElement;
  strip.scrollLeft = pick.offsetLeft - strip.clientWidth / 2 + pick.offsetWidth / 2;
});

// The strips hide their scrollbar, so a plain mouse could not move them: turn the wheel into a
// sideways scroll, and let the strip be dragged. Touch and trackpads already scroll them natively.
document.querySelectorAll("nav.overflow-x-auto").forEach((strip) => {
  strip.addEventListener("wheel", (e) => {
    if (e.ctrlKey || Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
    const max = strip.scrollWidth - strip.clientWidth;
    // At either end, leave the wheel to the page.
    if ((e.deltaY < 0 && strip.scrollLeft <= 0) || (e.deltaY > 0 && strip.scrollLeft >= max - 1)) return;
    e.preventDefault();
    strip.scrollLeft += e.deltaY;
  }, { passive: false });

  let startX = 0, startLeft = 0, dragging = false, moved = false;
  strip.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    dragging = true; moved = false;
    startX = e.clientX; startLeft = strip.scrollLeft;
  });
  strip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    if (!moved && Math.abs(dx) < 5) return;
    if (!moved) { moved = true; strip.setPointerCapture(e.pointerId); }
    strip.scrollLeft = startLeft - dx;
  });
  const stop = () => { dragging = false; };
  strip.addEventListener("pointerup", stop);
  strip.addEventListener("pointercancel", stop);
  // A drag that ends over a time shouldn't also open it.
  strip.addEventListener("click", (e) => { if (moved) { e.preventDefault(); moved = false; } }, true);
  strip.addEventListener("dragstart", (e) => e.preventDefault());
});

// ---- Loading bar --------------------------------------------------------------------------
// Pages here are mostly full loads, so between a click and the next page there was nothing to see.
// The bar shows for page changes (links, GET forms such as the day picker, script navigations) and for
// htmx requests that take longer than a moment; the 30-second background refreshes don't show it.
(() => {
  const bar = document.getElementById("page-progress");
  if (!bar) return;
  let busy = 0, timer;
  function start(delay = 0) {
    clearTimeout(timer);
    timer = setTimeout(() => {
      bar.classList.remove("done");
      void bar.offsetWidth; // restart the run from the left
      bar.classList.add("loading");
      document.documentElement.classList.add("page-loading");
    }, delay);
  }
  function finish() {
    clearTimeout(timer);
    document.documentElement.classList.remove("page-loading");
    if (!bar.classList.contains("loading")) return;
    bar.classList.remove("loading");
    bar.classList.add("done");
  }
  // Leaving the page: a plain click on an internal link, a GET form, or a navigation from script.
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target && a.target !== "_self" || a.hasAttribute("download") || a.origin !== location.origin) return;
    if (a.pathname === location.pathname && a.search === location.search && a.hash) return; // same-page anchor
    start();
  });
  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!e.defaultPrevented && !form.hasAttribute("hx-post") && !form.hasAttribute("hx-get")) start();
  });
  window.addEventListener("beforeunload", () => start());
  // Back/forward restores a page from memory with the bar still running: clear it.
  window.addEventListener("pageshow", () => { busy = 0; finish(); });
  // htmx: only for requests someone asked for, and only once they take a little while.
  const polling = (elt) => /every\s/.test(elt?.getAttribute?.("hx-trigger") || "");
  document.addEventListener("htmx:beforeRequest", (e) => {
    if (polling(e.detail.elt)) return;
    busy += 1;
    start(150);
  });
  document.addEventListener("htmx:afterRequest", (e) => {
    if (polling(e.detail.elt)) return;
    busy = Math.max(0, busy - 1);
    // A redirect (HX-Redirect / HX-Refresh) keeps loading; the next page clears the bar.
    const h = e.detail.xhr;
    if (!busy && !(h && (h.getResponseHeader("HX-Redirect") || h.getResponseHeader("HX-Refresh")))) finish();
  });
})();

// ---- MA crossover: form toggles, the running total, and each trade on its stock's chart ----------
(() => {
  // Index or typed list; the square-off only applies to intraday candles; a new candle size resets the
  // backtest's start to that size's usual period, until the date has been picked by hand.
  function sync(form) {
    const kind = form.querySelector("[data-universe-kind]:checked")?.value;
    form.querySelectorAll("[data-universe-section]").forEach((el) => { el.hidden = el.dataset.universeSection !== kind; });
    const tf = form.querySelector("[data-ma-tf]")?.value;
    const intraday = form.querySelector("[data-ma-intraday]");
    if (intraday) intraday.hidden = tf === "1d";
    const second = form.querySelector("[data-ma-second]");  // a second chance only makes sense with one entry per crossover
    if (second) second.disabled = !form.querySelector("[data-ma-once]").checked;
  }
  document.querySelectorAll(".ma-form").forEach((form) => {
    sync(form);
    form.addEventListener("change", (e) => {
      sync(form);
      const start = form.querySelector("[data-days]");
      if (e.target === start) start.dataset.picked = "1";
      if (e.target.matches("[data-ma-tf]") && start && !start.dataset.picked) {
        const days = JSON.parse(start.dataset.days)[e.target.value];
        const d = new Date(Date.now() - days * 864e5);
        start.value = d.toISOString().slice(0, 10);
      }
    });
  });

  let data, chart, series, chartSymbol = "", lines = [], index = new Map();
  const box = () => document.getElementById("ma-run");

  function curve(root) {
    const el = root.querySelector("#ma-curve");
    if (!el || !data.curve.length) return;
    const L = window.LightweightCharts;
    const c = L.createChart(el, chartLook(L));
    c.applyOptions({ localization: { priceFormatter: (v) => (v > 0 ? "+" : "") + v.toFixed(1) + "R" },
      timeScale: { timeVisible: !data.daily, secondsVisible: false } });
    const line = c.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2,
      topLineColor: "#2E8B57", topFillColor1: "rgba(46,139,87,.18)", topFillColor2: "rgba(46,139,87,.02)",
      bottomLineColor: "#C2475A", bottomFillColor1: "rgba(194,71,90,.02)", bottomFillColor2: "rgba(194,71,90,.18)" });
    line.setData(data.curve);
    c.timeScale().fitContent();
  }

  async function show(n) {
    const t = data.trades[n];
    if (!t) return;
    const panel = document.getElementById("ma-trade");
    const note = document.getElementById("ma-trade-note");
    panel.hidden = false;
    document.querySelectorAll("[data-ma-trade]").forEach((row) => row.toggleAttribute("data-selected", Number(row.dataset.maTrade) === n));
    if (chartSymbol !== t.symbol) {
      document.getElementById("ma-trade-title").textContent = t.symbol + " · loading…";
      const r = await fetch("/ma/backtest/chart?symbol=" + encodeURIComponent(t.symbol));
      const got = await r.json();
      if (!r.ok) { document.getElementById("ma-trade-title").textContent = t.symbol; note.textContent = got.error; return; }
      const L = window.LightweightCharts;
      chart?.remove();
      chart = L.createChart(document.getElementById("ma-trade-chart"), chartLook(L));
      chart.applyOptions({ timeScale: { timeVisible: !got.daily, secondsVisible: false } });
      series = chart.addCandlestickSeries(candleLook);
      series.setData(got.candles);
      const ma = (color, points) => chart.addLineSeries({ color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false,
        crosshairMarkerVisible: false }).setData(points);
      ma("#E9A23B", got.fast);
      ma("#0F5E5C", got.slow);
      document.getElementById("ma-fast-label").textContent = got.fast_label;
      document.getElementById("ma-slow-label").textContent = got.slow_label;
      index = new Map(got.candles.map((c, i) => [String(c.time), i]));
      // Every trade on this stock: an arrow at its entry, a dot at each exit.
      const marks = [];
      data.trades.filter((x) => x.symbol === t.symbol && x.price).forEach((x) => {
        const buy = x.side === "buy";
        marks.push({ time: x.entry, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
          color: buy ? "#2E8B57" : "#C2475A", text: (buy ? "Buy " : "Sell ") + x.price.toFixed(2) });
        x.exits.forEach((f) => marks.push({ time: f.time, position: buy ? "aboveBar" : "belowBar", shape: "circle",
          color: "#16373A", text: f.price.toFixed(2) }));
      });
      marks.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
      series.setMarkers(marks.filter((m) => index.has(String(m.time))));
      chartSymbol = t.symbol;
      lines = [];
    }
    document.getElementById("ma-trade-title").textContent = `${t.symbol} · ${t.side} ${t.price ? "trade" : "setup, not taken"}`;
    lines.forEach((l) => series.removePriceLine(l));
    lines = [];
    if (t.price) {
      lines.push(series.createPriceLine({ price: t.price, color: "#16373A", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Entry" }));
      lines.push(series.createPriceLine({ price: t.stop, color: "#C2475A", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "SL" }));
      if (t.target !== null) lines.push(series.createPriceLine({ price: t.target, color: "#2E8B57", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "Target" }));
    }
    const at = index.get(String(t.entry));
    if (at !== undefined) {
      const out = t.exits.length ? index.get(String(t.exits.at(-1).time)) ?? at : at;
      chart.timeScale().setVisibleLogicalRange({ from: at - 50, to: Math.max(out, at) + 30 });
    } else chart.timeScale().fitContent();
    panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  async function build(root) {
    const source = root.querySelector("#ma-data");
    if (!source) return;
    data = JSON.parse(source.textContent);
    chart = null; chartSymbol = "";
    try { await loadChartLib(); } catch (e) { return; }
    curve(root);
  }

  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-ma-trade]");
    if (row && data) show(Number(row.dataset.maTrade));
  });
  document.addEventListener("keydown", (e) => {
    const row = e.target.matches?.("[data-ma-trade]") ? e.target : null;
    if (!row || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    show(Number(row.dataset.maTrade));
  });
  document.addEventListener("htmx:load", (e) => { if (e.detail.elt.id === "ma-run") build(e.detail.elt); });
  if (box()) build(box());
})();

// ---- MA crossover: stock picker (type, pick with Enter or a click, removable pills) ------------------
(() => {
  const MAX = 500;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  document.querySelectorAll("[data-symbol-picker]").forEach((root) => {
    const box = root.querySelector("[data-picker-box]");
    const pills = root.querySelector("[data-picker-pills]");
    const input = root.querySelector("[data-picker-input]");
    const list = root.querySelector("[data-picker-list]");
    const value = root.querySelector("[data-picker-value]");
    const section = root.closest("[data-universe-section]");
    const count = section.querySelector("[data-picker-count]");
    const clear = section.querySelector("[data-picker-clear]");
    let picked = value.value.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean);
    let matches = [], active = -1, timer, request;

    function save() {
      picked = [...new Set(picked)].slice(0, MAX);
      value.value = picked.join(", ");
      count.textContent = picked.length;
      clear.hidden = !picked.length;
      pills.innerHTML = picked.map((s) => `
        <span class="inline-flex items-center gap-1 rounded-full bg-peacock-50 py-1 pl-3 pr-1 text-sm font-semibold text-peacock">${esc(s)}
          <button type="button" data-remove="${esc(s)}" class="grid h-5 w-5 place-items-center rounded-full text-peacock/70 transition hover:bg-white hover:text-fall"
                  aria-label="Remove ${esc(s)}" title="Remove ${esc(s)}">
            <svg class="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button></span>`).join("");
      input.placeholder = picked.length ? "Add another" : "Type a stock, e.g. INFY or hdfc";
    }

    function close() {
      list.hidden = true; list.innerHTML = ""; matches = []; active = -1;
      input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant");
    }

    function highlight(i) {
      const opts = [...list.querySelectorAll('[role="option"]')];
      if (!opts.length) return;
      active = (i + opts.length) % opts.length;
      opts.forEach((o, n) => o.setAttribute("aria-selected", n === active ? "true" : "false"));
      opts[active].scrollIntoView({ block: "nearest" });
      input.setAttribute("aria-activedescendant", opts[active].id);
    }

    function show(found, query) {
      matches = found;
      if (!found.length) {
        list.innerHTML = `<p class="px-3 py-2.5 text-sm text-ink-soft">No NSE stock matches “${esc(query)}”.</p>`;
      } else {
        list.innerHTML = found.map((m, n) => {
          const have = picked.includes(m.symbol);
          return `<button type="button" role="option" id="${input.id}-opt-${n}" data-pick="${n}" aria-selected="false" tabindex="-1"
            class="flex w-full items-baseline gap-3 rounded-xl px-3 py-2 text-left transition hover:bg-peacock-50 aria-selected:bg-peacock-50">
            <span class="font-display font-bold">${esc(m.symbol)}</span>
            <span class="min-w-0 flex-1 truncate text-sm text-ink-soft">${esc(m.name)}</span>
            ${m.index ? '<span class="rounded-full bg-marigold-50 px-2 py-0.5 text-xs font-medium text-marigold-dark">Index</span>' : ""}
            ${have ? '<span class="text-xs font-medium text-peacock">added</span>' : ""}</button>`;
        }).join("");
      }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      if (found.length) highlight(0);
    }

    async function search() {
      const query = input.value.trim();
      if (!query) return close();
      request?.abort();
      request = new AbortController();
      try {
        const r = await fetch("/ma/symbols?q=" + encodeURIComponent(query), { signal: request.signal });
        const got = await r.json();
        if (document.activeElement !== input || input.value.trim() !== query) return;  // picked or typed on meanwhile
        if (!r.ok) { list.innerHTML = `<p class="px-3 py-2.5 text-sm text-fall">${esc(got.error)}</p>`; list.hidden = false; return; }
        show(got, query);
      } catch (e) { /* aborted by a newer search */ }
    }

    function add(symbols) {
      const before = picked.length;
      picked.push(...symbols);
      save();
      if (picked.length >= MAX && before + symbols.length > MAX) {
        document.body.dispatchEvent(new CustomEvent("toast", { detail: { message: `A list can have up to ${MAX} stocks.`, kind: "error" } }));
      }
      input.value = "";
      close();
      input.focus();
    }

    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(search, 150); });
    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown" && !list.hidden) { e.preventDefault(); highlight(active + 1); }
      else if (e.key === "ArrowUp" && !list.hidden) { e.preventDefault(); highlight(active - 1); }
      else if (e.key === "Enter") {
        e.preventDefault();  // never submits the form from here
        if (matches[active]) add([matches[active].symbol]);
      } else if (e.key === "Escape") { close(); }
      else if (e.key === "Backspace" && !input.value && picked.length) { picked.pop(); save(); }
    });
    // A pasted list (INFY, TCS or one per line) adds every symbol; the server checks them when the form is sent.
    input.addEventListener("paste", (e) => {
      const text = e.clipboardData.getData("text");
      if (!/[,\n]/.test(text)) return;
      e.preventDefault();
      add(text.split(/[,\n]/).map((s) => s.trim().toUpperCase().replace(/^NSE:/, "")).filter(Boolean));
    });
    list.addEventListener("mousedown", (e) => {  // mousedown, so it lands before the input loses focus
      const opt = e.target.closest("[data-pick]");
      if (opt) { e.preventDefault(); add([matches[Number(opt.dataset.pick)].symbol]); }
    });
    pills.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-remove]");
      if (!btn) return;
      picked = picked.filter((s) => s !== btn.dataset.remove);
      save();
      input.focus();
    });
    box.addEventListener("click", (e) => { if (e.target === box) input.focus(); });
    clear.addEventListener("click", () => { picked = []; save(); input.focus(); });
    input.addEventListener("blur", () => setTimeout(close, 120));
    save();
  });
})();

// ---- MA crossover "Scan now": a stock's chart with both averages and its setup ------------------------
(() => {
  let chart, series;
  async function show(symbol) {
    const box = document.getElementById("ma-screen-chart-box");
    if (!box) return;
    box.hidden = false;
    document.querySelectorAll("[data-screen-symbol]").forEach((row) => row.toggleAttribute("data-selected", row.dataset.screenSymbol === symbol));
    const title = document.getElementById("ma-screen-title");
    const note = document.getElementById("ma-screen-note");
    title.textContent = symbol + " · loading…";
    const r = await fetch("/ma/screen/chart?symbol=" + encodeURIComponent(symbol));
    const got = await r.json();
    if (!r.ok) { title.textContent = symbol; note.textContent = got.error; return; }
    try { await loadChartLib(); } catch (e) { note.textContent = e.message; return; }
    const L = window.LightweightCharts;
    chart?.remove();
    chart = L.createChart(document.getElementById("ma-screen-chart"), chartLook(L));
    chart.applyOptions({ timeScale: { timeVisible: !got.daily, secondsVisible: false } });
    series = chart.addCandlestickSeries(candleLook);
    series.setData(got.candles);
    const ma = (color, points) => chart.addLineSeries({ color, lineWidth: 2, lastValueVisible: false, priceLineVisible: false,
      crosshairMarkerVisible: false }).setData(points);
    ma("#E9A23B", got.fast);
    ma("#0F5E5C", got.slow);
    document.getElementById("ma-screen-fast").textContent = got.fast_label;
    document.getElementById("ma-screen-slow").textContent = got.slow_label;
    const row = got.row;
    title.textContent = row ? `${symbol} · ${row.side} ${row.at ? "triggered" : "setup"}` : symbol;
    if (row) {
      const buy = row.side === "buy";
      const marks = [{ time: got.trigger, position: buy ? "aboveBar" : "belowBar", shape: "circle", color: "#E9A23B", text: "Trigger" }];
      if (got.entry) marks.push({ time: got.entry, position: buy ? "belowBar" : "aboveBar", shape: buy ? "arrowUp" : "arrowDown",
        color: buy ? "#2E8B57" : "#C2475A", text: (buy ? "Buy " : "Sell ") + row.price.toFixed(2) });
      series.setMarkers(marks.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0)));
      const line = (price, color, text) => price !== null && series.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: text });
      line(row.level, "#16373A", buy ? "Enter above" : "Enter below");
      line(row.stop, "#C2475A", "SL");
      line(row.target, "#2E8B57", "Target");
    }
    // The last 60 candles, where the setup is; the rest is a scroll away.
    chart.timeScale().setVisibleLogicalRange({ from: got.candles.length - 60, to: got.candles.length + 3 });
    box.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  document.addEventListener("click", (e) => {
    const row = e.target.closest("[data-screen-symbol]");
    if (row) show(row.dataset.screenSymbol);
  });
  document.addEventListener("keydown", (e) => {
    const row = e.target.matches?.("[data-screen-symbol]") ? e.target : null;
    if (!row || (e.key !== "Enter" && e.key !== " ")) return;
    e.preventDefault();
    show(row.dataset.screenSymbol);
  });
})();
