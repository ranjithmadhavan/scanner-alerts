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
  }, kind === "error" ? 5000 : 2800);
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

// Alert form: timeframe only matters for "closes above/below".
document.addEventListener("change", (e) => {
  if (e.target.name !== "condition") return;
  const tf = document.getElementById("timeframe-field");
  if (tf) tf.hidden = !e.target.value.startsWith("close_");
});

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

// ---- Chart side panel ---------------------------------------------------------
const StockChart = (() => {
  const LIB = "https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js";
  const drawer = document.getElementById("chart-drawer");
  if (!drawer) return null;
  const box = document.getElementById("chart");
  const msg = document.getElementById("chart-msg");
  const levelNote = document.getElementById("chart-level");
  let chart, series, levelLine, lastFocus, request = 0;
  const state = { symbol: "", range: "1D", level: null };
  const rupees = (p) => "₹" + p.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

  function loadLib() {
    if (window.LightweightCharts) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = LIB; s.onload = resolve; s.onerror = () => reject(new Error("Couldn't load the chart library."));
      document.head.appendChild(s);
    });
  }

  function build() {
    const L = window.LightweightCharts;
    chart = L.createChart(box, {
      autoSize: true,
      layout: { background: { color: "#FFFFFF" }, textColor: "#5F7476", fontFamily: "Figtree, system-ui, sans-serif" },
      grid: { vertLines: { color: "#F4EFE8" }, horzLines: { color: "#F4EFE8" } },
      rightPriceScale: { borderColor: "#ECE6DC" },
      timeScale: { borderColor: "#ECE6DC" },
      crosshair: { mode: L.CrosshairMode.Normal },
      localization: { priceFormatter: rupees },
    });
    series = chart.addCandlestickSeries({
      upColor: "#2E8B57", downColor: "#C2475A", borderVisible: false,
      wickUpColor: "#2E8B57", wickDownColor: "#C2475A",
      // Stretch the price axis so the level line is always on screen, even far from price.
      autoscaleInfoProvider: (original) => {
        const res = original();
        if (res && state.level) {
          res.priceRange.minValue = Math.min(res.priceRange.minValue, state.level);
          res.priceRange.maxValue = Math.max(res.priceRange.maxValue, state.level);
        }
        return res;
      },
    });
  }

  function setLevel(level) {
    state.level = level > 0 ? level : null;
    if (levelLine && series) { series.removePriceLine(levelLine); levelLine = null; }
    levelNote.hidden = !state.level;
    if (!state.level) { chart?.priceScale("right").applyOptions({ autoScale: true }); return; }
    levelNote.lastElementChild.textContent = "Your level " + rupees(state.level);
    if (series) {
      levelLine = series.createPriceLine({
        price: state.level, color: "#E9A23B", lineWidth: 2, lineStyle: 2, axisLabelVisible: true, title: "Level",
      });
      chart.priceScale("right").applyOptions({ autoScale: true }); // re-fit to include the new level
    }
  }

  async function load() {
    const mine = ++request;
    drawer.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-selected", b.dataset.range === state.range));
    msg.textContent = "Loading " + state.symbol + "…";
    try {
      await loadLib();
      const r = await fetch(`/alerts/chart?symbol=${encodeURIComponent(state.symbol)}&range=${state.range}`);
      const data = await r.json();
      if (mine !== request) return; // a newer request superseded this one
      if (!r.ok) throw new Error(data.error || "Couldn't load the chart.");
      if (!chart) build();
      document.getElementById("chart-name").textContent = data.name || "";
      chart.applyOptions({ timeScale: { timeVisible: !data.daily, secondsVisible: false } });
      series.setData(data.candles);
      setLevel(state.level);
      chart.timeScale().fitContent();
      msg.textContent = data.candles.length ? "" : "Kite has no candles for this range.";
    } catch (e) {
      if (mine === request) msg.textContent = e.message;
    }
  }

  function open(symbol, level) {
    lastFocus = document.activeElement;
    state.symbol = symbol;
    state.level = level;
    document.getElementById("chart-title").textContent = symbol;
    document.getElementById("chart-name").textContent = "";
    if (series) series.setData([]);
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

  // Any [data-chart] button opens the panel; the level comes from data-level or a form field.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-chart]");
    if (!btn) return;
    const from = btn.dataset.levelFrom && document.querySelector(btn.dataset.levelFrom);
    open(btn.dataset.chart, parseFloat(btn.dataset.level || from?.value || "") || null);
  });

  // Typing a level in the form moves the line live.
  document.getElementById("level")?.addEventListener("input", (e) => {
    const formSymbol = document.getElementById("symbol")?.value.trim().toUpperCase();
    if (!drawer.hidden && formSymbol === state.symbol) setLevel(parseFloat(e.target.value) || null);
  });

  return { open, close };
})();
