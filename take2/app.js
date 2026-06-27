/* iMessage PRM — Second Take (search-first).
   A smart search/index layer over Messages: one search box, a dense table,
   and clicking a person row opens the native Messages app. No in-app chat.

   Category state is shared with the classic UI: it lives in the server-side
   userstate (catOverride) + out/filters.json (type:"category" memberships),
   resolved by effectiveCategory() exactly like the classic app. */

"use strict";

/* ---------------- State ---------------- */
const state = {
  all: [],            // every conversation from /out/people.json
  view: "recent",     // "recent" | "lost"
  category: null,     // active category facet, or null
  showGroups: false,
  hideUnnamed: false, // hide bare phone numbers with no name help
  sortKey: "last",    // name | category | last | count
  sortDir: "desc",    // asc | desc
  // Search modes: "" = none, instant filter applied live from box; semantic =
  // server keys after Enter.
  instantTokens: [],  // live name-prefix tokens (client-side)
  semantic: null,     // { q, keys:Set, n, ms } when a semantic search is active
  selected: new Set(),// keys of person rows selected for bulk category move
};

const LS_HIDE_UNNAMED = "imsg_crm2_hide_unnamed";

/* ---- Shared category state (mirrors classic app.js) ----
   USERSTATE holds the WHOLE server object so we always POST it back intact and
   never clobber the classic app's excluded/approved/saved/dismissed. */
let USERSTATE = { excluded: {}, approved: {}, saved: {}, dismissed: {}, catOverride: {} };
let CAT_OVERRIDE = {};        // alias into USERSTATE.catOverride
let FILTERS = [];             // out/filters.json (list)
let CATEGORY_KEY_MAP = {};    // personKey -> user-category name (type:"category")

// The category a filter chip represents: built-ins on rule.category, user-added
// (type:"category") on .category. Non-category filters return null. (classic)
const chipCategory = (f) => (f && (f.category || (f.rule && f.rule.category))) || null;

// personKey -> user-category name, from type:"category" filters' keys arrays.
function rebuildCategoryIndex() {
  CATEGORY_KEY_MAP = {};
  for (const f of FILTERS) {
    if (f.type !== "category" || !Array.isArray(f.keys)) continue;
    const cat = chipCategory(f);
    if (!cat) continue;
    for (const k of f.keys) {
      if (!(k in CATEGORY_KEY_MAP)) CATEGORY_KEY_MAP[k] = cat; // first wins
    }
  }
}

// CURRENT category — single source of truth, identical priority walk to classic:
// (1) manual override; (2) user-category membership; (3) base p.category.
function effectiveCategory(p) {
  return CAT_OVERRIDE[p.key] || CATEGORY_KEY_MAP[p.key] || p.category;
}

/* Category colors. Prefer the classic chip's own color (from filters.json) so
   both UIs agree; fall back to a small self-contained map + hashed palette. */
const CAT_COLOR = {
  Family: "#ff2d55",
  Personal: "#34c759",
  Work: "#0b84fe",
  Contractors: "#ff9500",
  Service: "#ff9500",
};
function catColor(cat) {
  if (!cat) return "#8e8e93";
  const f = FILTERS.find((x) => chipCategory(x) === cat);
  if (f && f.color) return f.color;
  if (CAT_COLOR[cat]) return CAT_COLOR[cat];
  let h = 0;
  for (const ch of cat) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const palette = ["#0b84fe", "#ff9500", "#34c759", "#af52de", "#ff2d55",
    "#5ac8fa", "#5856d6", "#64d2ff"];
  return palette[h % palette.length];
}

// All assignable category targets: every chip that carries a category, plus any
// override-only value not represented by a chip. {category, label, color}.
function categoryTargets() {
  const seen = new Set();
  const out = [];
  for (const f of FILTERS) {
    const cat = chipCategory(f);
    if (!cat || seen.has(cat)) continue;
    seen.add(cat);
    out.push({ category: cat, label: f.name || cat, color: f.color || catColor(cat) });
  }
  // Categories that exist only because someone assigned them via override.
  for (const k in CAT_OVERRIDE) {
    const c = CAT_OVERRIDE[k];
    if (c && !seen.has(c)) { seen.add(c); out.push({ category: c, label: c, color: catColor(c) }); }
  }
  return out;
}

/* ---------------- DOM ---------------- */
const $ = (sel) => document.querySelector(sel);
const searchEl = $("#search");
const spinEl = $("#search-spin");
const statusEl = $("#status");
const rowsEl = $("#rows");
const emptyEl = $("#empty");
const facetsEl = $("#facets");
const groupsEl = $("#show-groups");
const hideUnnamedEl = $("#hide-unnamed");
const theadEl = $("#grid thead");
const bulkbarEl = $("#bulkbar");

/* ---------------- Helpers ---------------- */
function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Split a name into lowercase tokens on any non-alphanumeric boundary.
function tokenize(s) {
  return String(s || "").toLowerCase().split(/[^a-z0-9]+/i).filter(Boolean);
}

// Relative "last contact": 3d, 2mo, 1y. Empty string if no/invalid date.
function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!isFinite(then)) return "";
  const days = Math.floor((Date.now() - then) / 86400000);
  if (days <= 0) return "today";
  if (days < 7) return days + "d";
  if (days < 60) return Math.floor(days / 7) + "w";
  if (days < 365) return Math.floor(days / 30) + "mo";
  return Math.floor(days / 365) + "y";
}

function lastTs(p) {
  const t = p.last_date ? new Date(p.last_date).getTime() : NaN;
  return isFinite(t) ? t : 0;
}

// A person row that's just a bare number with no name help.
function isUnnamed(p) {
  return p.kind === "person" && !p.in_contacts && !p.suggested_name;
}

/* ---------------- Shared-state load / persist (mirrors classic) ---------------- */
async function loadSharedState() {
  // Userstate: keep the WHOLE object; alias catOverride.
  try {
    const s = await fetch("/api/userstate").then((r) => r.json());
    USERSTATE = {
      excluded: s.excluded || {},
      approved: s.approved || {},
      saved: s.saved || {},
      dismissed: s.dismissed || {},
      catOverride: s.catOverride || {},
    };
  } catch (e) { /* leave defaults */ }
  CAT_OVERRIDE = USERSTATE.catOverride;

  // Filters: user-category memberships + chip colors/labels.
  try {
    FILTERS = await fetch("/out/filters.json").then((r) => r.json());
    if (!Array.isArray(FILTERS)) FILTERS = [];
  } catch (e) { FILTERS = []; }
  rebuildCategoryIndex();
}

// Persist the FULL userstate object so we never wipe the classic app's state.
// Re-GET first to merge any concurrent changes, then overwrite only catOverride.
async function persistState() {
  let server = null;
  try { server = await fetch("/api/userstate").then((r) => r.json()); } catch (e) { /* offline */ }
  const merged = {
    excluded: (server && server.excluded) || USERSTATE.excluded || {},
    approved: (server && server.approved) || USERSTATE.approved || {},
    saved: (server && server.saved) || USERSTATE.saved || {},
    dismissed: (server && server.dismissed) || USERSTATE.dismissed || {},
    catOverride: CAT_OVERRIDE, // our authoritative copy for this field
  };
  USERSTATE = merged;
  try {
    await fetch("/api/userstate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(merged),
    });
  } catch (e) { /* offline: in-memory change still applies this session */ }
}

/* ---------------- Data load ---------------- */
async function load() {
  statusEl.textContent = "Loading…";
  state.hideUnnamed = localStorage.getItem(LS_HIDE_UNNAMED) === "1";
  if (hideUnnamedEl) hideUnnamedEl.checked = state.hideUnnamed;

  let people;
  try {
    [people] = await Promise.all([
      fetch("/out/people.json").then((r) => r.json()),
      loadSharedState(),
    ]);
  } catch (e) {
    statusEl.textContent = "Failed to load people.json";
    return;
  }
  state.all = people;
  setView("recent");      // default: Recent
  render();
  // Optional, non-blocking model-status hint for first semantic search.
  fetch("/api/status").then((r) => r.json()).then((s) => {
    if (s && s.loaded === false) {
      statusEl.dataset.modelHint = "Model still loading — first search may be slow.";
    }
  }).catch(() => {});
}

/* ---------------- Facets (driven by effectiveCategory) ---------------- */
function buildFacets() {
  const counts = new Map();
  for (const p of state.all) {
    if (p.kind !== "person") continue;
    const c = effectiveCategory(p) || "—";
    counts.set(c, (counts.get(c) || 0) + 1);
  }
  const cats = [...counts.keys()].sort((a, b) => counts.get(b) - counts.get(a));
  facetsEl.innerHTML = cats.map((c) =>
    `<button class="facet${state.category === c ? " on" : ""}" data-cat="${escapeHtml(c)}" style="--cc:${catColor(c)}">` +
    `${escapeHtml(c)}<em>${counts.get(c)}</em></button>`
  ).join("");
}

/* ---------------- View / sort selection ---------------- */
function setView(v) {
  state.view = v;
  if (v === "lost") { state.sortKey = "last"; state.sortDir = "asc"; }
  else { state.sortKey = "last"; state.sortDir = "desc"; }
  document.querySelectorAll(".view-btn").forEach((b) =>
    b.classList.toggle("on", b.dataset.view === v));
}

/* ---------------- Pipeline ---------------- */
function currentRows() {
  let rows = state.all.slice();

  // Groups hidden unless explicitly shown.
  if (!state.showGroups) rows = rows.filter((p) => p.kind !== "group");

  // Hide bare, unidentified numbers.
  if (state.hideUnnamed) rows = rows.filter((p) => !isUnnamed(p));

  // "Lost touch" = real two-way relationships only (people view by nature).
  if (state.view === "lost") {
    rows = rows.filter((p) => p.kind === "person" &&
      p.sent > 0 && p.recv > 0 && (p.count || 0) >= 10);
  }

  // Category facet (uses effectiveCategory so it matches the classic UI).
  if (state.category) rows = rows.filter((p) => (effectiveCategory(p) || "—") === state.category);

  // Semantic search overrides everything else by key set.
  if (state.semantic) {
    const keys = state.semantic.keys;
    rows = rows.filter((p) => keys.has(p.key));
  } else if (state.instantTokens.length) {
    const qs = state.instantTokens;
    rows = rows.filter((p) => {
      const toks = tokenize(p.name).concat(tokenize(p.suggested_name));
      return qs.every((q) => toks.some((t) => t.startsWith(q)));
    });
  }

  return sortRows(rows);
}

function sortRows(rows) {
  const dir = state.sortDir === "asc" ? 1 : -1;
  const key = state.sortKey;
  rows.sort((a, b) => {
    let av, bv;
    if (key === "name") { av = (a.name || "").toLowerCase(); bv = (b.name || "").toLowerCase(); }
    else if (key === "category") { av = (effectiveCategory(a) || "").toLowerCase(); bv = (effectiveCategory(b) || "").toLowerCase(); }
    else if (key === "count") { av = a.count || 0; bv = b.count || 0; }
    else { av = lastTs(a); bv = lastTs(b); } // last
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    const tb = lastTs(b) - lastTs(a);
    if (tb) return tb;
    return (a.name || "").localeCompare(b.name || "");
  });
  return rows;
}

/* ---------------- Render ---------------- */
function render() {
  closePopover();
  const rows = currentRows();
  buildFacets();
  renderHeader();
  renderStatus(rows.length);
  renderBulkBar();

  rowsEl.innerHTML = rows.map(rowHtml).join("");
  emptyEl.hidden = rows.length > 0;
}

function renderHeader() {
  theadEl.querySelectorAll("th").forEach((th) => {
    const k = th.dataset.sort;
    const base = th.textContent.replace(/[ ▲▼]+$/, "").trim();
    if (k === state.sortKey) {
      th.innerHTML = `${escapeHtml(base)}<span class="arrow">${state.sortDir === "asc" ? "▲" : "▼"}</span>`;
    } else {
      th.textContent = base;
    }
  });
}

function renderStatus(n) {
  if (state.semantic) {
    const s = state.semantic;
    statusEl.innerHTML =
      `${s.n} result${s.n === 1 ? "" : "s"} for “${escapeHtml(s.q)}”` +
      (s.ms ? ` · ${Math.round(s.ms)}ms` : "") +
      ` · <a id="clear">Clear</a>`;
    const c = $("#clear"); if (c) c.onclick = clearSearch;
    return;
  }
  if (state.instantTokens.length) {
    statusEl.innerHTML = `${n} match${n === 1 ? "" : "es"} · <a id="clear">Clear</a>`;
    const c = $("#clear"); if (c) c.onclick = clearSearch;
    return;
  }
  const label = state.view === "lost" ? "lost touch (oldest first)" : "recent";
  const cat = state.category ? ` · ${escapeHtml(state.category)}` : "";
  statusEl.textContent = `${n} ${label}` + cat;
}

function rowHtml(p) {
  const isPerson = p.kind === "person";
  const canOpen = isPerson && !!p.raw_id;
  const cat = effectiveCategory(p);
  const color = catColor(cat);
  const checked = state.selected.has(p.key);

  let nameCell;
  if (isPerson) {
    const showRaw = !p.in_contacts && p.raw_id && p.raw_id !== p.name;
    const sub = showRaw
      ? `<span class="raw">${escapeHtml(p.raw_id)}</span>`
      : (p.suggested_name ? `<span class="suggest">maybe: ${escapeHtml(p.suggested_name)}</span>` : "");
    const check = `<span class="rowcheck"><input type="checkbox" data-key="${escapeHtml(p.key)}"${checked ? " checked" : ""} aria-label="Select"></span>`;
    nameCell = `${check}<div class="cell-name"><span class="nm">${escapeHtml(p.name || p.raw_id || "Unknown")}</span>${sub}</div>`;
  } else {
    nameCell = `<span class="rowcheck"></span><div class="cell-name"><span class="nm">${escapeHtml(p.name || "Group")}<span class="grouptag">Group</span></span></div>`;
  }

  const cls = (isPerson ? "person" : "group") + (checked ? " checked" : "");
  const dataKey = ` data-key="${escapeHtml(p.key)}"`;
  const dataOpen = canOpen ? ` data-raw="${escapeHtml(p.raw_id)}"` : "";
  const title = canOpen ? ` title="Open in Messages"` : "";
  // Category pill is a button for people (opens the inline category menu).
  const pill = isPerson
    ? `<button class="pill pill-btn" data-key="${escapeHtml(p.key)}" style="--cc:${color}" title="Change category">${escapeHtml(cat || "—")}</button>`
    : `<span class="pill" style="--cc:${color}">${escapeHtml(cat || "—")}</span>`;

  return `<tr class="${cls}"${dataKey}${dataOpen}${title}>` +
    `<td class="col-name">${nameCell}</td>` +
    `<td class="col-cat">${pill}</td>` +
    `<td class="col-last">${escapeHtml(relTime(p.last_date))}</td>` +
    `<td class="col-count">${p.count || 0}</td>` +
    `</tr>`;
}

/* ---------------- Category editing ---------------- */

// Assign a category to a set of person keys via the persistent override, once.
async function assignCategory(keys, category) {
  if (!category) return;
  for (const k of keys) CAT_OVERRIDE[k] = category;
  await persistState();   // one full-object round-trip for the whole batch
  render();
}

// Prompt for a brand-new category value (no model run — pure override label).
function promptNewCategory() {
  const name = (window.prompt("New category name:") || "").trim();
  return name || null;
}

/* --- Inline (Airtable-style) pill popover --- */
let popoverEl = null;
function closePopover() {
  if (popoverEl) { popoverEl.remove(); popoverEl = null; }
}
function openCategoryPopover(anchor, key) {
  closePopover();
  const cur = effectiveCategory(state.all.find((p) => p.key === key) || {});
  const targets = categoryTargets();
  const items = targets.map((t) =>
    `<div class="po-item${t.category === cur ? " cur" : ""}" data-cat="${escapeHtml(t.category)}">` +
    `<span class="po-dot" style="background:${t.color}"></span>${escapeHtml(t.label)}</div>`
  ).join("");
  popoverEl = document.createElement("div");
  popoverEl.className = "popover";
  popoverEl.innerHTML = items +
    `<div class="po-sep"></div><div class="po-item po-new">+ New category…</div>`;
  document.body.appendChild(popoverEl);

  const r = anchor.getBoundingClientRect();
  popoverEl.style.top = (window.scrollY + r.bottom + 4) + "px";
  popoverEl.style.left = (window.scrollX + r.left) + "px";

  popoverEl.addEventListener("click", async (e) => {
    const item = e.target.closest(".po-item");
    if (!item) return;
    e.stopPropagation();
    let cat;
    if (item.classList.contains("po-new")) { cat = promptNewCategory(); if (!cat) { closePopover(); return; } }
    else cat = item.dataset.cat;
    closePopover();
    await assignCategory([key], cat);
  });
}

/* --- Multi-select bulk bar --- */
function renderBulkBar() {
  if (!bulkbarEl) return;
  if (state.selected.size === 0) { bulkbarEl.hidden = true; bulkbarEl.innerHTML = ""; return; }
  const targets = categoryTargets().map((t) =>
    `<button class="bulk-move" data-cat="${escapeHtml(t.category)}" style="--cc:${t.color}">${escapeHtml(t.label)}</button>`
  ).join("");
  bulkbarEl.hidden = false;
  bulkbarEl.innerHTML =
    `<span class="bulk-n">${state.selected.size} selected</span>` +
    `<span class="bulk-sep">·</span><span class="bulk-label">Move to:</span>${targets}` +
    `<button class="bulk-new">+ New category…</button>` +
    `<span class="bulk-sep">·</span><button class="bulk-clear">Clear</button>`;
}

function clearSelection() { state.selected.clear(); }

/* ---------------- Search behavior ---------------- */
function onInput() {
  const q = searchEl.value.trim();
  state.semantic = null;
  spinEl.hidden = true;
  state.instantTokens = q ? tokenize(q) : [];
  render();
}

async function onEnter() {
  const q = searchEl.value.trim();
  if (!q) { clearSearch(); return; }

  const tokens = tokenize(q);
  const instantHits = currentInstantHitCount(tokens);
  const multiWord = tokens.length > 1;

  if (!multiWord && instantHits > 0) return; // fast instant view is enough

  spinEl.hidden = false;
  statusEl.innerHTML = `<span class="searching">searching…` +
    (statusEl.dataset.modelHint ? ` ${escapeHtml(statusEl.dataset.modelHint)}` : "") +
    `</span>`;
  try {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await r.json();
    state.instantTokens = [];
    state.semantic = {
      q,
      keys: new Set(data.keys || []),
      n: typeof data.n === "number" ? data.n : (data.keys || []).length,
      ms: data.ms || 0,
    };
  } catch (e) {
    statusEl.textContent = "Search failed.";
    spinEl.hidden = true;
    return;
  }
  spinEl.hidden = true;
  render();
}

function currentInstantHitCount(tokens) {
  if (!tokens.length) return 0;
  let rows = state.all;
  if (!state.showGroups) rows = rows.filter((p) => p.kind !== "group");
  let n = 0;
  for (const p of rows) {
    const toks = tokenize(p.name).concat(tokenize(p.suggested_name));
    if (tokens.every((q) => toks.some((t) => t.startsWith(q)))) n++;
  }
  return n;
}

function clearSearch() {
  searchEl.value = "";
  state.instantTokens = [];
  state.semantic = null;
  spinEl.hidden = true;
  render();
}

/* ---------------- Events ---------------- */
searchEl.addEventListener("input", onInput);
searchEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); onEnter(); }
  else if (e.key === "Escape") { clearSearch(); }
});

document.querySelectorAll(".view-btn").forEach((b) =>
  b.addEventListener("click", () => { setView(b.dataset.view); render(); }));

groupsEl.addEventListener("change", () => {
  state.showGroups = groupsEl.checked;
  render();
});

if (hideUnnamedEl) hideUnnamedEl.addEventListener("change", () => {
  state.hideUnnamed = hideUnnamedEl.checked;
  localStorage.setItem(LS_HIDE_UNNAMED, state.hideUnnamed ? "1" : "0");
  render();
});

facetsEl.addEventListener("click", (e) => {
  const btn = e.target.closest(".facet");
  if (!btn) return;
  const cat = btn.dataset.cat;
  state.category = state.category === cat ? null : cat;
  render();
});

theadEl.addEventListener("click", (e) => {
  const th = e.target.closest("th");
  if (!th) return;
  const k = th.dataset.sort;
  if (state.sortKey === k) {
    state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
  } else {
    state.sortKey = k;
    state.sortDir = (k === "name" || k === "category") ? "asc" : "desc";
  }
  render();
});

// Table clicks: route between (a) row checkbox select, (b) category pill menu,
// (c) row open-in-Messages. The checkbox and pill stopPropagation so they never
// trigger the row's Messages open.
rowsEl.addEventListener("click", (e) => {
  // (a) selection checkbox
  const cb = e.target.closest(".rowcheck input");
  if (cb) {
    e.stopPropagation();
    const key = cb.dataset.key;
    if (cb.checked) state.selected.add(key); else state.selected.delete(key);
    const tr = cb.closest("tr");
    if (tr) tr.classList.toggle("checked", cb.checked);
    renderBulkBar();
    return;
  }
  // (b) category pill -> inline popover
  const pill = e.target.closest(".pill-btn");
  if (pill) {
    e.stopPropagation();
    openCategoryPopover(pill, pill.dataset.key);
    return;
  }
  // (c) open native Messages for people with a raw_id
  const tr = e.target.closest("tr.person");
  if (!tr) return;
  const raw = tr.dataset.raw;
  if (!raw) return;
  window.location.href = "imessage://" + encodeURIComponent(raw);
});

// Bulk bar actions.
bulkbarEl.addEventListener("click", async (e) => {
  if (e.target.closest(".bulk-clear")) { clearSelection(); render(); return; }
  if (e.target.closest(".bulk-new")) {
    const cat = promptNewCategory();
    if (!cat) return;
    const keys = [...state.selected]; clearSelection();
    await assignCategory(keys, cat);
    return;
  }
  const move = e.target.closest(".bulk-move");
  if (move) {
    const keys = [...state.selected]; clearSelection();
    await assignCategory(keys, move.dataset.cat);
  }
});

// Dismiss the inline popover on outside click / Escape / scroll.
document.addEventListener("click", (e) => {
  if (popoverEl && !popoverEl.contains(e.target)) closePopover();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePopover(); });
$("#table-scroll").addEventListener("scroll", closePopover);

/* ---------------- Go ---------------- */
load();
