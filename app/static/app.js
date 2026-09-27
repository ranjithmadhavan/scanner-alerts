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
