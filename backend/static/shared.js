// Shared across index.html and workspace.html: markdown-lite rendering and
// theme-toggle wiring. Extracted so the two pages can't silently drift.

function hexToRgba(hex, alpha) {
  const h = hex.replace("#", "");
  const full = h.length === 3 ? h.split("").map(c => c + c).join("") : h;
  const n = parseInt(full, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function escapeHtmlText(s) {
  const div = document.createElement("div");
  div.textContent = s;
  return div.innerHTML;
}

// Small dependency-free Markdown subset for assistant answers: "- "/"* "
// bullet lists, "1. " numbered lists, **bold**, blank-line paragraph breaks.
// Every piece of raw text is escaped via escapeHtmlText before any of our
// own <ul>/<li>/<strong> tags are added, so AI-generated text can't inject
// markup -- only our own controlled wrapper tags ever get inserted.
function inlineFormat(s) {
  return escapeHtmlText(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

// Gemini occasionally answers with literal <ul>/<ol>/<li>/<p>/<br> tags
// instead of the markdown-lite convention below (observed in real chat
// answers -- e.g. a whole bulleted answer arriving as one line of
// "<ul><li>...</li><li>...</li></ul>"). Without this, that line falls
// through to the plain-paragraph branch, gets correctly escaped for safety,
// and renders as literal visible "<ul><li>" text instead of a list.
//
// This converts *only* those specific structural tags into the markdown-lite
// syntax the rest of this function already parses -- every other character
// in the string (including any other tag-like text) still goes through
// escapeHtmlText untouched afterward, so this doesn't widen what markup can
// reach the DOM; it just recognizes one more input shape as "a list", the
// same way "- " and "1. " already are.
function normalizeHtmlListsToMarkdown(text) {
  if (!text || !/<\/?(ul|ol|li|p|br)\b/i.test(text)) return text; // fast path
  const itemsToMarkdown = (inner, marker) => {
    const items = [...inner.matchAll(/<li[^>]*>([\s\S]*?)<\/li>/gi)].map(m => `${marker} ${m[1].trim()}`);
    return `\n${items.join("\n")}\n`;
  };
  return text
    .replace(/<ol[^>]*>([\s\S]*?)<\/ol>/gi, (_, inner) => itemsToMarkdown(inner, "1."))
    .replace(/<ul[^>]*>([\s\S]*?)<\/ul>/gi, (_, inner) => itemsToMarkdown(inner, "-"))
    .replace(/<\/p>/gi, "\n\n")
    .replace(/<p[^>]*>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n");
}

function renderMarkdownLite(text) {
  const lines = normalizeHtmlListsToMarkdown(text || "").split("\n");
  let html = "", listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };

  for (const raw of lines) {
    const line = raw.trim();
    const bullet = line.match(/^[-*]\s+(.*)/);
    const numbered = line.match(/^\d+[.)]\s+(.*)/);
    if (bullet) {
      if (listType !== "ul") { closeList(); html += "<ul>"; listType = "ul"; }
      html += `<li>${inlineFormat(bullet[1])}</li>`;
    } else if (numbered) {
      if (listType !== "ol") { closeList(); html += "<ol>"; listType = "ol"; }
      html += `<li>${inlineFormat(numbered[1])}</li>`;
    } else if (line === "") {
      closeList();
    } else {
      closeList();
      html += `<p>${inlineFormat(line)}</p>`;
    }
  }
  closeList();
  return html;
}

// ---------------- theme toggle ----------------
// Expects #theme-toggle-btn / #theme-toggle-icon to exist. currentTheme is
// attached to window so page-specific scripts (e.g. graph link colors) can
// read/react to it.

let currentTheme = document.documentElement.getAttribute("data-theme") || "dark";
window.currentTheme = currentTheme;
window.onThemeChange = null; // page scripts may set this to a callback(theme)

function initThemeToggle() {
  const btn = document.getElementById("theme-toggle-btn");
  const icon = document.getElementById("theme-toggle-icon");
  if (!btn || !icon) return;

  function applyThemeButtonState() {
    icon.textContent = currentTheme === "dark" ? "☀" : "☾";
    btn.title = `Switch to ${currentTheme === "dark" ? "light" : "dark"} theme`;
  }
  applyThemeButtonState();

  btn.addEventListener("click", () => {
    currentTheme = currentTheme === "dark" ? "light" : "dark";
    window.currentTheme = currentTheme;
    document.documentElement.setAttribute("data-theme", currentTheme);
    localStorage.setItem("theme", currentTheme);
    applyThemeButtonState();
    if (typeof window.onThemeChange === "function") window.onThemeChange(currentTheme);
  });
}

document.addEventListener("DOMContentLoaded", initThemeToggle);

// ---------------- context-window usage badge ----------------
// Shared by both pages. Expects #ctx-badge / #ctx-popover to exist. Call
// refreshCtxBadge(url) after selecting a video/board and after any
// successful Gemini call, to pull the latest real usage snapshot recorded
// by the backend (backend/usage.py) for that scope.
//
// Every number rendered here came straight from the Gemini API response
// (exact token counts, not estimated from character counts) -- see
// backend/usage.py for where each field comes from.

function fmtTokens(n) {
  if (n === null || n === undefined) return "?";
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(2).replace(/\.00$/, "").replace(/(\.\d)0$/, "$1") + "M";
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, "") + "K";
  return String(n);
}

function fmtCost(n) {
  if (n === null || n === undefined) return "$0.00";
  if (n === 0) return "$0.00";
  return `$${n < 0.01 ? n.toFixed(6) : n.toFixed(4)}`;
}

function renderCtxPopover(snap) {
  const pct = snap.percent_used;
  const rows = Object.entries(snap.components || {})
    .filter(([, val]) => typeof val === "number" && val > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([name, val]) => `
      <div class="ctx-row">
        <span class="ctx-row-name">${escapeHtmlText(name)}</span>
        <span class="ctx-row-vals">${fmtTokens(val)} &middot; ${(val / snap.context_window * 100).toFixed(1)}%</span>
      </div>
    `).join("");

  const cacheLine = snap.cached_tokens > 0
    ? `<div class="ctx-row"><span class="ctx-row-name">Cached (discounted)</span><span class="ctx-row-vals">${fmtTokens(snap.cached_tokens)}</span></div>`
    : `<div class="ctx-row"><span class="ctx-row-name">Cached</span><span class="ctx-row-vals">none this call</span></div>`;

  let costLine = "cost unavailable for this model";
  if (snap.estimated_cost_usd !== null && snap.estimated_cost_usd !== undefined) {
    costLine = `<span class="ctx-cost">${fmtCost(snap.estimated_cost_usd)}</span> est. &middot; ${escapeHtmlText(snap.model)} pricing`;
  }

  const st = snap.scope_totals;
  const totalsBlock = st ? `
    <hr class="ctx-sep">
    <div class="ctx-row"><span class="ctx-row-name">Total for ${escapeHtmlText(snap.scope_label || "this")}</span><span class="ctx-row-vals ctx-cost">${fmtCost(st.cost_usd)}</span></div>
    <div class="ctx-row"><span class="ctx-row-name" style="color:var(--text-faint);font-size:11px;">${st.calls} call${st.calls === 1 ? "" : "s"} &middot; ${fmtTokens(st.prompt_tokens + st.output_tokens + st.thinking_tokens)} tokens</span></div>
  ` : "";

  return `
    <div class="ctx-title">Context window</div>
    <div class="ctx-headline">
      <span class="frac">${fmtTokens(snap.prompt_tokens)}/${fmtTokens(snap.context_window)} tokens</span>
      <span class="pct">${pct}% used</span>
    </div>
    <div class="ctx-bar-bg"><div class="ctx-bar-fg" style="width:${Math.min(100, pct)}%"></div></div>
    ${rows}
    <div class="ctx-row ctx-free"><span class="ctx-row-name">Free space</span><span class="ctx-row-vals">${fmtTokens(snap.free_tokens)} &middot; ${(100 - pct).toFixed(1)}%</span></div>
    <hr class="ctx-sep">
    <div class="ctx-row"><span class="ctx-row-name">Response</span><span class="ctx-row-vals">${fmtTokens(snap.output_tokens)}</span></div>
    ${snap.thinking_tokens > 0 ? `<div class="ctx-row"><span class="ctx-row-name">Thinking</span><span class="ctx-row-vals">${fmtTokens(snap.thinking_tokens)}</span></div>` : ""}
    ${cacheLine}
    <div class="ctx-footer">
      ${costLine}<br>
      ${escapeHtmlText(snap.label || "")} &middot; ${snap.elapsed_s}s &middot; ${escapeHtmlText(snap.timestamp || "")}
    </div>
    ${totalsBlock}
  `;
}

async function refreshCtxBadge(url) {
  const badge = document.getElementById("ctx-badge");
  const popover = document.getElementById("ctx-popover");
  if (!badge || !popover) return;
  if (!url) { badge.classList.remove("visible"); popover.classList.remove("open"); return; }
  try {
    const res = await fetch(url);
    if (!res.ok) { badge.classList.remove("visible"); popover.classList.remove("open"); return; }
    const snap = await res.json();
    badge.textContent = `${Math.round(snap.percent_used)}%`;
    badge.classList.add("visible");
    popover.innerHTML = renderCtxPopover(snap);
  } catch (e) {
    badge.classList.remove("visible");
  }
  refreshGlobalUsageBadge(); // a call just happened -- the all-time total moved too
}

function initCtxBadge() {
  const badge = document.getElementById("ctx-badge");
  const popover = document.getElementById("ctx-popover");
  if (!badge || !popover) return;
  badge.addEventListener("click", (e) => {
    e.stopPropagation();
    popover.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    if (popover.classList.contains("open") && !popover.contains(e.target) && e.target !== badge) {
      popover.classList.remove("open");
    }
  });
}

document.addEventListener("DOMContentLoaded", initCtxBadge);

// ---------------- global (all-time, all-videos/boards) usage badge ----------------
// Expects #global-usage-badge to exist (a small always-visible text element,
// no popover -- deliberately quieter than the per-call badge above, since
// this is background awareness, not something you click through).

async function refreshGlobalUsageBadge() {
  const el = document.getElementById("global-usage-badge");
  if (!el) return;
  try {
    const res = await fetch("/api/usage/global");
    if (!res.ok) return;
    const t = await res.json();
    el.textContent = fmtCost(t.cost_usd);
    el.title = `${t.calls} Gemini call${t.calls === 1 ? "" : "s"} total · ${fmtTokens(t.prompt_tokens + t.output_tokens + t.thinking_tokens)} tokens · estimated at paid per-token pricing -- your actual bill depends on whether your key's project has billing enabled`;
  } catch (e) {
    // quiet background indicator -- a failed fetch just leaves the last known value
  }
}

document.addEventListener("DOMContentLoaded", refreshGlobalUsageBadge);

// ---------------- knowledge-graph node rendering ----------------
// Shared "ring" node style used by both pages' force-graph instances: a thin
// colored outline with a hollow, background-matched center (not a solid
// dot), sized by how connected the node is, with a label drawn below it
// only once zoomed in far enough to read -- our graphs run from a handful
// of nodes up to several hundred (a full project's merged graph), so labels
// permanently on every node would be unreadable clutter at overview zoom;
// gating on globalScale keeps the wide view clean and still gives full
// labels the moment you zoom into a cluster.
//
// Colors are hardcoded per-theme pairs (not a live getComputedStyle read)
// for the same reason GRAPH_LINK_COLOR already is: this paint function runs
// once per node per animation frame, and reading CSS custom properties that
// often would be wasted work -- these values just need to stay in sync with
// theme.css's :root / [data-theme="light"] tokens by hand.
const GRAPH_BG_COLOR = { dark: "#14161a", light: "#f6f1e6" };
const GRAPH_TEXT_COLOR = { dark: "#dcdfe3", light: "#3a3222" };
const GRAPH_TEXT_FAINT_COLOR = { dark: "#6f7580", light: "#948765" };

function computeNodeDegrees(nodes, links) {
  const deg = {};
  nodes.forEach(n => { deg[n.id] = 0; });
  links.forEach(l => {
    const s = typeof l.source === "object" ? l.source.id : l.source;
    const t = typeof l.target === "object" ? l.target.id : l.target;
    if (deg[s] !== undefined) deg[s]++;
    if (deg[t] !== undefined) deg[t]++;
  });
  return deg;
}

function graphNodeRadius(node) {
  const d = node.__degree || 0;
  return Math.min(15, 4 + Math.sqrt(d) * 3);
}

// `focusedNodeIds`: null (nothing focused) or a Set of ids to keep at full
// opacity, dimming everything else -- same semantics as the existing
// focus-mode dimming, just applied inside the custom paint instead of via
// a color-accessor function.
function paintGraphNode(node, ctx, globalScale, colorsMap, focusedNodeIds, hexToRgbaFn) {
  const r = graphNodeRadius(node);
  const baseColor = colorsMap[node.type] || "#888";
  const dimmed = focusedNodeIds && !focusedNodeIds.has(node.id);
  const theme = window.currentTheme === "light" ? "light" : "dark";

  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
  ctx.fillStyle = GRAPH_BG_COLOR[theme];
  ctx.fill();
  ctx.lineWidth = Math.max(1.3, 2.2 / Math.sqrt(globalScale));
  ctx.strokeStyle = dimmed ? hexToRgbaFn(baseColor, 0.15) : baseColor;
  ctx.stroke();

  if (globalScale > 2.2 && !dimmed) {
    const fontSize = Math.max(3.5, 11 / globalScale);
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.font = `600 ${fontSize}px -apple-system, BlinkMacSystemFont, sans-serif`;
    ctx.fillStyle = GRAPH_TEXT_COLOR[theme];
    ctx.fillText(node.label, node.x, node.y + r + 2);

    ctx.font = `${(fontSize * 0.82).toFixed(1)}px -apple-system, BlinkMacSystemFont, sans-serif`;
    ctx.fillStyle = GRAPH_TEXT_FAINT_COLOR[theme];
    ctx.fillText((node.type || "").toUpperCase(), node.x, node.y + r + fontSize + 3);
  }
}

// Hit-testing must use the same radius as the visible ring (plus a small
// margin) or clicks near a small node silently miss.
function paintGraphNodePointerArea(node, color, ctx) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(node.x, node.y, graphNodeRadius(node) + 2, 0, 2 * Math.PI, false);
  ctx.fill();
}
