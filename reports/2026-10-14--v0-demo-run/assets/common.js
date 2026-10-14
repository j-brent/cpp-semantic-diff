// Shared helpers for the hand-built playground pages.
const D = window.RUN_DATA;

const el = (tag, attrs = {}, ...kids) => {
  const n = tag.startsWith("svg:")
    ? document.createElementNS("http://www.w3.org/2000/svg", tag.slice(4))
    : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null) continue;
    if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null) n.append(k.nodeType ? k : document.createTextNode(k));
  return n;
};

const fmtN = (n) => n.toLocaleString("en-US");
const fmtS = (s) => (s >= 10 ? s.toFixed(1) : s >= 1 ? s.toFixed(1) : s.toFixed(2)) + " s";
const pct = (a, b) => ((100 * a) / b).toFixed(1) + "%";

// One tooltip per page; marks call tip.show(evt, html-node) / tip.hide().
const tip = (() => {
  const box = el("div", { class: "tip", role: "tooltip" });
  document.addEventListener("DOMContentLoaded", () => document.body.append(box));
  return {
    show(evt, content) {
      box.replaceChildren(content);
      box.classList.add("on");
      const pad = 14, w = box.offsetWidth, h = box.offsetHeight;
      let x = evt.clientX + pad, y = evt.clientY + pad;
      if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
      if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
      box.style.left = Math.max(8, x) + "px";
      box.style.top = Math.max(8, y) + "px";
    },
    hide() { box.classList.remove("on"); },
  };
})();

// Theme toggle: cycles OS -> light -> dark; remembered per viewer when storage allows.
function themeButton() {
  const order = ["auto", "light", "dark"];
  let cur = "auto";
  try { cur = localStorage.getItem("pgb-theme") || "auto"; } catch {}
  const apply = () => {
    if (cur === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", cur);
    btn.textContent = "Theme: " + cur;
  };
  const btn = el("button", { class: "btn", type: "button", onclick: () => {
    cur = order[(order.indexOf(cur) + 1) % order.length];
    try { localStorage.setItem("pgb-theme", cur); } catch {}
    apply();
  } });
  apply();
  return btn;
}

function segmented(options, initial, onChange) {
  const wrap = el("div", { class: "seg", role: "group" });
  const buttons = options.map(([value, label]) => el("button", {
    type: "button", "aria-pressed": String(value === initial), text: label,
    onclick: (e) => {
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === e.currentTarget)));
      onChange(value);
    },
  }));
  wrap.append(...buttons);
  return wrap;
}

const statusChip = (level, label) => el("span", { class: `status ${level}` },
  el("span", { class: "dot", "aria-hidden": "true", text: { good: "✓", warning: "!", serious: "!", critical: "×", info: "i" }[level] }),
  label);

document.addEventListener("DOMContentLoaded", () => {
  const slot = document.getElementById("theme-slot");
  if (slot) slot.append(themeButton());
});
