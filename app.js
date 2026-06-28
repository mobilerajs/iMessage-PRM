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
  semantic: null,     // { q, keys:Set, n, ms, snippets, terms } when active
  selected: new Set(),// keys of person rows selected for bulk category move
  expanded: new Set(),// keys whose Match snippet is expanded to the full chunk
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
const loadingEl = $("#loading");
const statusEl = $("#status");
const rowsEl = $("#rows");
const emptyEl = $("#empty");
const facetsEl = $("#facets");
const groupsEl = $("#show-groups");
const hideUnnamedEl = $("#hide-unnamed");
const theadEl = $("#grid thead");
const bulkbarEl = $("#bulkbar");
const countsEl = $("#counts");
const syncedEl = $("#synced");
const refreshBtn = $("#refresh-btn");

/* Library counts under the title: people / groups / filtered-out, so it's clear
   the table is the kept set (junk filtered) and groups are toggled, not missing. */
function renderCounts() {
  if (!countsEl) return;
  const people = state.all.filter((p) => p.kind === "person").length;
  const groups = state.all.filter((p) => p.kind === "group").length;
  const parts = [`${people.toLocaleString()} people`,
    `${groups.toLocaleString()} groups ${state.showGroups ? "shown" : "hidden"}`];
  const f = state.stats && state.stats.filtered;
  if (f && typeof f.total === "number") parts.push(`${f.total.toLocaleString()} filtered out`);
  countsEl.textContent = parts.join(" · ");
}

/* Friendly "Last synced" label: "just now", "5m ago", "3h ago", "2d ago", or a
   date for anything older. Empty string if no/invalid timestamp. */
function friendlySince(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (!isFinite(then)) return "";
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 0) return "just now";
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + "m ago";
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return hrs + "h ago";
  const days = Math.floor(hrs / 24);
  if (days < 7) return days + "d ago";
  return new Date(then).toLocaleDateString();
}

/* "Last synced" label from stats.last_synced. Hidden if unknown. */
function renderSynced() {
  if (!syncedEl) return;
  const iso = state.stats && state.stats.last_synced;
  const when = friendlySince(iso);
  if (when) {
    syncedEl.textContent = "Last synced: " + when;
    syncedEl.title = iso;
    syncedEl.hidden = false;
  } else {
    syncedEl.hidden = true;
  }
}

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
  // Recency = later of the 1:1 thread and the person's group activity, so
  // someone you chat with daily in a group isn't shown as "lost touch".
  const iso = p.last_active || p.last_date;
  const t = iso ? new Date(iso).getTime() : NaN;
  return isFinite(t) ? t : 0;
}

// A person row that's just a bare number with no name help.
function isUnnamed(p) {
  return p.kind === "person" && !p.in_contacts && !p.suggested_name;
}

/* ---- Match-snippet rendering (semantic mode only) ---- */
const SNIP_MAX = 90; // truncated length, ~1 line

// True when the table is showing the dynamic "Match" column.
function semanticActive() { return !!state.semantic; }

// Escape, then bold the query terms inside an already-escaped string.
function highlightTerms(escaped, terms) {
  if (!terms || !terms.length) return escaped;
  // Build one alternation of escaped terms; match case-insensitively on the
  // (HTML-escaped) text. Terms are alphanumeric tokens, so no regex specials.
  const pat = terms
    .filter((t) => t.length >= 2)
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  if (!pat) return escaped;
  return escaped.replace(new RegExp("(" + pat + ")", "gi"), "<b>$1</b>");
}

// Truncated snippet: collapse newlines/whitespace to single spaces, cap length.
function snippetTruncatedHtml(text, terms) {
  const flat = String(text || "").replace(/\s+/g, " ").trim();
  if (!flat) return "";
  const cut = flat.length > SNIP_MAX ? flat.slice(0, SNIP_MAX).trimEnd() + "…" : flat;
  return highlightTerms(escapeHtml(cut), terms);
}

// Full snippet: preserve line breaks (rendered via CSS white-space: pre-wrap).
function snippetFullHtml(text, terms) {
  const t = String(text || "").trim();
  if (!t) return "";
  return highlightTerms(escapeHtml(t), terms);
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
  // First run: no index yet -> show the Setup screen instead of the table.
  try {
    const st = await fetch("/api/setup/status").then((r) => r.json());
    if (st.needs_setup) { showSetup(); return; }
  } catch (e) { /* not the live server (static mode) — fall through to normal load */ }
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
  // Library counts (people / groups / filtered) so it's clear what's shown vs
  // dropped by the junk filter. Non-blocking; tolerates a missing stats file.
  fetch("/out/stats.json").then((r) => r.json()).then((s) => {
    state.stats = s; renderCounts(); renderSynced();
  }).catch(() => { renderCounts(); renderSynced(); });
  // Optional, non-blocking model-status hint for first semantic search. Also
  // tells us the local server is up, so we can reveal the Refresh button (it
  // stays hidden in static/file mode, where /api/refresh wouldn't exist).
  pollModelStatus();
}

// Poll /api/status until the model is ready. While it reports model_loading,
// keep a persistent banner in #status (visible at rest, not only during a
// search) so the first-search slowness is never a surprise; clear it once the
// model flips to ready. The banner only shows while idle (no active search /
// instant filter), so it never stomps a real search-status message.
let modelLoading = false;
function pollModelStatus() {
  fetch("/api/status").then((r) => r.json()).then((s) => {
    if (refreshBtn) refreshBtn.hidden = false;  // server is reachable
    const loading = !!(s && s.model_loading === true);
    if (loading) {
      modelLoading = true;
      statusEl.dataset.modelHint = "Model loading — first search may be slow.";
      // Show the persistent banner only when nothing else owns #status.
      if (!state.semantic && !state.instantTokens.length) {
        statusEl.textContent = statusEl.dataset.modelHint;
      }
      setTimeout(pollModelStatus, 2000);
    } else if (modelLoading) {
      // Just flipped ready: drop the hint and clear a stale banner.
      modelLoading = false;
      delete statusEl.dataset.modelHint;
      if (!state.semantic && !state.instantTokens.length) renderStatus(currentRows().length);
    }
  }).catch(() => { /* static mode: leave Refresh hidden, no banner */ });
}

/* ---------------- Facets (driven by effectiveCategory) ---------------- */
// The facet chips depend ONLY on: the person set, each person's effectiveCategory
// (manual override / user-category membership / base category), and which facet
// is currently active (the "on" highlight). They do NOT depend on the search
// tokens, sort, expand state, view, or the groups/hide-unnamed toggles. So we
// memoize the rendered chips on a cheap signature of exactly those inputs and
// skip the (full-array) recompute + DOM write when nothing facet-relevant moved.
// This keeps every keystroke off the facet path while preserving identical
// output whenever a category move / bulk-move / data reload DOES change them.
let _facetSig = null;
function facetSignature() {
  // people count + a digest of category assignments + the active facet.
  let people = 0;
  let h = 0;
  for (const p of state.all) {
    if (p.kind !== "person") continue;
    people++;
    const c = effectiveCategory(p) || "—";
    // Order-independent contribution per person (key + its effective category)
    // so any reassignment changes the digest, but row order/sort does not.
    let ph = 0;
    const s = p.key + " " + c;
    for (let i = 0; i < s.length; i++) ph = (ph * 31 + s.charCodeAt(i)) >>> 0;
    h = (h + ph) >>> 0;
  }
  return `${people}:${h}:${state.category || ""}`;
}

function buildFacets() {
  const sig = facetSignature();
  if (sig === _facetSig) return;   // nothing facet-relevant changed; skip DOM
  _facetSig = sig;
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
  renderEmpty(rows.length);
}

// Empty-state that distinguishes "a search/filter is active but matched nothing"
// (offer a Clear action) from a fresh/blank table (hint to press Enter). Mirrors
// the #clear link pattern so the action reuses the existing clear logic.
function renderEmpty(n) {
  emptyEl.hidden = n > 0;
  if (n > 0) return;
  const filterActive = !!(state.semantic || state.instantTokens.length ||
    state.category || state.view === "lost");
  if (filterActive) {
    emptyEl.innerHTML =
      `No matches for your current search/filter. ` +
      `<a id="empty-clear">Clear</a>`;
    const c = $("#empty-clear");
    if (c) c.onclick = () => { clearActiveFilters(); };
  } else {
    emptyEl.innerHTML =
      `No people to show. Type a name to filter, ` +
      `or press Enter to search messages.`;
  }
}

// Clear whatever is narrowing the table: the search, the category facet, and
// any non-default view. Reuses clearSearch (which re-renders).
function clearActiveFilters() {
  state.category = null;
  if (state.view === "lost") setView("recent");
  clearSearch();   // clears search + instant tokens, then render()
}

function renderHeader() {
  // Sortable columns (the static 4).
  theadEl.querySelectorAll("th[data-sort]").forEach((th) => {
    const k = th.dataset.sort;
    const base = th.textContent.replace(/[ ▲▼]+$/, "").trim();
    if (k === state.sortKey) {
      th.innerHTML = `${escapeHtml(base)}<span class="arrow">${state.sortDir === "asc" ? "▲" : "▼"}</span>`;
      th.setAttribute("aria-sort", state.sortDir === "asc" ? "ascending" : "descending");
    } else {
      th.textContent = base;
      th.removeAttribute("aria-sort");
    }
  });
  // Dynamic, non-sortable Match column header — present only in semantic mode.
  const headRow = theadEl.querySelector("tr");
  let matchTh = theadEl.querySelector("th.col-match");
  if (semanticActive()) {
    if (!matchTh) {
      matchTh = document.createElement("th");
      matchTh.className = "col-match";
      matchTh.textContent = "Match";
      headRow.appendChild(matchTh); // last column
    }
  } else if (matchTh) {
    matchTh.remove();
  }
  // Toggle a class on the grid so CSS can switch to a wider, auto layout.
  $("#grid").classList.toggle("with-match", semanticActive());
}

function renderStatus(n) {
  if (state.semantic) {
    const s = state.semantic;
    const collapse = state.expanded.size
      ? ` · <a id="collapse-all">Collapse all (${state.expanded.size})</a>` : "";
    statusEl.innerHTML =
      `${s.n} result${s.n === 1 ? "" : "s"} for “${escapeHtml(s.q)}”` +
      (s.ms ? ` · ${Math.round(s.ms)}ms` : "") +
      ` · <a id="clear">Clear</a>` + collapse;
    const c = $("#clear"); if (c) c.onclick = clearSearch;
    const ca = $("#collapse-all");
    if (ca) ca.onclick = () => { state.expanded.clear(); render(); };
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

  // Dynamic Match cell — only in semantic mode. Truncated vs. full per-row.
  let matchCell = "";
  if (semanticActive()) {
    const raw = state.semantic.snippets[p.key];
    if (raw && String(raw).trim()) {
      const expanded = state.expanded.has(p.key);
      const terms = state.semantic.terms;
      const inner = expanded ? snippetFullHtml(raw, terms) : snippetTruncatedHtml(raw, terms);
      const tip = expanded ? "Click to collapse" : "Click to expand";
      matchCell = `<td class="col-match"><div class="snip${expanded ? " expanded" : ""}" ` +
        `data-key="${escapeHtml(p.key)}" title="${tip}">${inner}</div></td>`;
    } else {
      matchCell = `<td class="col-match"></td>`; // no snippet → empty cell
    }
  }

  return `<tr class="${cls}"${dataKey}${dataOpen}${title}>` +
    `<td class="col-name">${nameCell}</td>` +
    `<td class="col-cat">${pill}</td>` +
    `<td class="col-last">${escapeHtml(relTime(p.last_active || p.last_date))}</td>` +
    `<td class="col-count">${p.count || 0}</td>` +
    matchCell +
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
  if (popoverEl) {
    if (popoverEl._scrim) popoverEl._scrim.remove();
    popoverEl.remove();
    popoverEl = null;
  }
}
function openCategoryPopover(anchor, key) {
  closePopover();
  const cur = effectiveCategory(state.all.find((p) => p.key === key) || {});
  const targets = categoryTargets();
  const items = targets.map((t) =>
    `<div class="po-item${t.category === cur ? " cur" : ""}" data-cat="${escapeHtml(t.category)}">` +
    `<span class="po-dot" style="background:${t.color}"></span>${escapeHtml(t.label)}</div>`
  ).join("");
  // Scrim behind the menu so a misclick on the look-alike filter-facet bar just
  // closes the menu instead of filtering the table.
  const scrim = document.createElement("div");
  scrim.className = "po-scrim";
  scrim.addEventListener("click", closePopover);
  document.body.appendChild(scrim);

  popoverEl = document.createElement("div");
  popoverEl.className = "popover";
  popoverEl._scrim = scrim;
  popoverEl.innerHTML =
    `<div class="po-head">Move to…</div>` + items +
    `<div class="po-sep"></div><div class="po-item po-new">+ New category…</div>`;
  document.body.appendChild(popoverEl);

  // Position below the pill, but flip above / clamp horizontally so it never
  // runs off the viewport (rows near the bottom or right edge).
  const r = anchor.getBoundingClientRect();
  const pw = popoverEl.offsetWidth, ph = popoverEl.offsetHeight, m = 8;
  let left = r.left;
  if (left + pw > window.innerWidth - m) left = window.innerWidth - pw - m;
  if (left < m) left = m;
  let top = r.bottom + 4;
  let below = true;
  if (top + ph > window.innerHeight - m) {
    const above = r.top - ph - 4;
    if (above >= m) { top = above; below = false; }
    else { top = Math.max(m, window.innerHeight - ph - m); }
  }
  popoverEl.style.top = (window.scrollY + top) + "px";
  popoverEl.style.left = (window.scrollX + left) + "px";

  // Caret tethering the menu to the pill (only when it sits below it), so it
  // reads as "this contact's menu", not the global filter bar. Clamped to stay
  // within the popover's rounded corners.
  if (below) {
    const caret = document.createElement("div");
    caret.className = "po-caret";
    const cx = (r.left + r.width / 2) - left;             // pill center within popover
    caret.style.left = Math.max(10, Math.min(pw - 16, cx - 6)) + "px";
    popoverEl.appendChild(caret);
  }

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
// Debounce the per-keystroke TABLE re-render so rapid typing coalesces into one
// render(), instead of rebuilding all ~742-1028 rows on every keystroke. The
// search box's own value updates instantly (the browser owns the input);
// onInput updates the instant tokens synchronously (so onEnter / a flush always
// sees the latest query) but defers the expensive render(). Only this
// per-keystroke path is debounced — every other caller of render() is untouched.
const INPUT_DEBOUNCE_MS = 120;
let _inputTimer = null;
function flushInputRender() {
  if (_inputTimer !== null) { clearTimeout(_inputTimer); _inputTimer = null; }
}

function onInput() {
  const q = searchEl.value.trim();
  state.semantic = null;
  spinEl.hidden = true;
  // Update tokens synchronously so a flush / onEnter always reflects the latest
  // keystroke; only the table re-render is debounced.
  state.instantTokens = q ? tokenize(q) : [];
  if (_inputTimer !== null) clearTimeout(_inputTimer);
  _inputTimer = setTimeout(() => { _inputTimer = null; render(); }, INPUT_DEBOUNCE_MS);
}

async function onEnter() {
  // Cancel any pending debounced instant-filter render; the semantic search
  // below supersedes it and instantTokens were already updated in onInput.
  flushInputRender();
  const q = searchEl.value.trim();
  if (!q) { clearSearch(); return; }

  // Pressing Enter ALWAYS runs a content search now (single-word topics like
  // "pizza" work too; quoted phrases run an exact/literal search server-side).
  // Show a clear loading state in the CONTENT area (a semantic search takes a
  // couple seconds); the tiny search-box dot was easy to miss / jittered.
  const lt = document.getElementById("loading-text");
  if (lt) lt.textContent = `Searching for “${q}”…` +
    (statusEl.dataset.modelHint ? ` ${statusEl.dataset.modelHint}` : "");
  loadingEl.hidden = false;
  statusEl.textContent = "";
  try {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q));
    const data = await r.json();
    state.instantTokens = [];
    state.expanded.clear();   // fresh search starts all rows collapsed
    state.semantic = {
      q,
      keys: new Set(data.keys || []),
      n: typeof data.n === "number" ? data.n : (data.keys || []).length,
      ms: data.ms || 0,
      snippets: data.snippets || {},   // { personKey: matched chunk text }
      terms: tokenize(q),              // query terms, for highlighting
    };
  } catch (e) {
    // Keep the typed query and offer an inline Retry (no dead-end). Mirrors the
    // existing #clear link pattern: an inline <a> with an id + click handler.
    loadingEl.hidden = true;
    statusEl.innerHTML = `Search failed. <a id="retry">Retry</a>`;
    const rt = $("#retry"); if (rt) rt.onclick = onEnter;
    return;
  }
  loadingEl.hidden = true;
  render();
}

function clearSearch() {
  flushInputRender();   // drop any pending debounced instant-filter render
  searchEl.value = "";
  state.instantTokens = [];
  state.semantic = null;
  spinEl.hidden = true;
  render();
}

/* ---------------- Refresh (snapshot live DB + incremental rebuild) ----------------
   UX: clicking Refresh first opens a confirmation modal with a time estimate
   (the contacts sync over a large address book is slow). On Proceed we POST
   /api/refresh and put up a full-page BLOCKING overlay (backdrop intercepts all
   clicks) that shows live stage text until the job finishes. */
let refreshing = false;

// Friendly duration: seconds -> "~45 sec" / "~90 sec" / "~2 min" / "~3.5 min".
function friendlyDuration(secs) {
  const s = Math.max(1, Math.round(Number(secs) || 0));
  if (s < 90) {
    // Round to the nearest 5s for a "~45 sec" feel; never below ~15 sec.
    const r = Math.max(15, Math.round(s / 5) * 5);
    return `~${r} sec`;
  }
  const mins = s / 60;
  // Whole minute if close, else one decimal ("~2 min" / "~2.5 min").
  const rounded = Math.round(mins * 2) / 2;
  const label = Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
  return `~${label} min`;
}

// Estimate refresh seconds from the estimate payload. Prefer the measured
// last_seconds; otherwise model it: AppleScript contacts sync dominates
// (~contacts/80 s), plus a small base for db parse / embed / rebuild.
function estimateSeconds(est) {
  if (est && typeof est.last_seconds === "number" && est.last_seconds > 0) {
    return { secs: est.last_seconds, measured: true };
  }
  let secs = 20; // base: db parse + embeddings + rebuild
  if (est && typeof est.contacts === "number" && est.contacts > 0) {
    secs += est.contacts / 80;        // AppleScript contacts sync dominates
  } else {
    secs += 70;                       // unknown contact count → assume "a minute or two"
  }
  if (est && typeof est.db_mb === "number" && est.db_mb > 0) {
    secs += Math.min(30, est.db_mb / 40); // larger DBs parse a bit slower (capped)
  }
  return { secs, measured: false };
}

// "~140 MB messages · 3,200 contacts" — context line, omits unknown parts.
function refreshContextLine(est) {
  const parts = [];
  if (est && typeof est.db_mb === "number" && est.db_mb > 0) {
    parts.push(`~${Math.round(est.db_mb).toLocaleString()} MB messages`);
  }
  if (est && typeof est.contacts === "number" && est.contacts > 0) {
    parts.push(`${est.contacts.toLocaleString()} contacts`);
  }
  return parts.join(" · ");
}

// Map the job `message` to friendly stage text for the blocking overlay.
function refreshStageText(message) {
  switch (message) {
    case "snapshotting messages":
    case "snapshotting": return "Snapshotting new messages…";
    case "syncing contacts": return "Re-reading your contacts… (this is the slow part)";
    case "rebuilding": return "Rebuilding the index…";
    case "done": return "Done";
    default: return "Refreshing…";
  }
}

/* ---- Confirmation modal ---- */
let refreshModalEl = null;
function closeRefreshModal() {
  if (refreshModalEl) { refreshModalEl.remove(); refreshModalEl = null; }
}

async function openRefreshModal() {
  if (refreshing) return;
  closeRefreshModal();

  // Build the modal up front (with a placeholder estimate) so the click feels
  // instant; fill in the real estimate once /api/refresh/estimate resolves.
  refreshModalEl = document.createElement("div");
  refreshModalEl.className = "modal-backdrop";
  refreshModalEl.innerHTML =
    `<div class="modal-card" role="dialog" aria-modal="true" aria-labelledby="rf-title">
       <h2 class="modal-title" id="rf-title">Refresh from your live data?</h2>
       <p class="modal-body">
         This will snapshot your newest messages, re-read your contacts, and
         rebuild the search index.
       </p>
       <p class="modal-est" id="rf-est">Checking estimate…</p>
       <p class="modal-ctx" id="rf-ctx" hidden></p>
       <div class="modal-actions">
         <button class="modal-btn modal-cancel" id="rf-cancel">Cancel</button>
         <button class="modal-btn modal-proceed" id="rf-proceed">Proceed</button>
       </div>
     </div>`;
  document.body.appendChild(refreshModalEl);

  const cancel = () => closeRefreshModal();
  $("#rf-cancel").onclick = cancel;
  $("#rf-proceed").onclick = () => { closeRefreshModal(); startRefresh(); };
  // Click outside the card (on the backdrop) cancels; Escape cancels.
  refreshModalEl.addEventListener("click", (e) => {
    if (e.target === refreshModalEl) cancel();
  });
  const onKey = (e) => {
    if (e.key === "Escape") { document.removeEventListener("keydown", onKey); cancel(); }
  };
  document.addEventListener("keydown", onKey);

  // Fetch the estimate; degrade to a generic message on any failure.
  let est = null;
  try {
    est = await fetch("/api/refresh/estimate").then((r) => r.json());
  } catch (e) { est = null; }
  if (!refreshModalEl) return; // user already cancelled

  const estEl = $("#rf-est");
  const ctxEl = $("#rf-ctx");
  if (est) {
    const { secs, measured } = estimateSeconds(est);
    estEl.textContent = measured
      ? `Usually takes ${friendlyDuration(secs)}.`
      : `This may take ${friendlyDuration(secs)} — re-reading your contacts is the slow part.`;
    const ctx = refreshContextLine(est);
    if (ctx) { ctxEl.textContent = ctx; ctxEl.hidden = false; }
  } else {
    estEl.textContent = "This may take a minute or two — re-reading your contacts is the slow part.";
  }
}

/* ---- Blocking overlay during the run ---- */
let refreshOverlayEl = null;
function openRefreshOverlay() {
  if (refreshOverlayEl) return;
  refreshOverlayEl = document.createElement("div");
  refreshOverlayEl.className = "refresh-overlay";
  refreshOverlayEl.innerHTML =
    `<div class="refresh-overlay-card" role="status" aria-live="polite">
       <span class="spin-dot spin-lg"></span>
       <div class="refresh-overlay-text">
         <div class="refresh-overlay-stage" id="rf-stage">Starting refresh…</div>
         <div class="refresh-overlay-note">Please wait — don't close this tab.</div>
       </div>
     </div>`;
  document.body.appendChild(refreshOverlayEl);
  // Trap Escape so it can't dismiss anything underneath while the run blocks.
  refreshOverlayEl.tabIndex = -1;
}
function setRefreshStage(text) {
  const el = refreshOverlayEl && refreshOverlayEl.querySelector("#rf-stage");
  if (el) el.textContent = text;
}
function closeRefreshOverlay() {
  if (refreshOverlayEl) { refreshOverlayEl.remove(); refreshOverlayEl = null; }
}

function setRefreshing(on) {
  refreshing = on;
  if (!refreshBtn) return;
  refreshBtn.disabled = on;
  refreshBtn.innerHTML = on
    ? `<span class="spin-dot"></span> Refreshing…`
    : "↻ Refresh";
}

// Poll the shared /api/job/<id> route ~every 1s until the job finishes.
// Calls onProgress(job) while running, onDone(job) on success, onError(message)
// on failure or a lost connection. Reused by Refresh and filter deletes so the
// background-job UX (no synchronous request-thread hangs) stays consistent.
function pollJob(jobId, { onProgress, onDone, onError }) {
  const tick = async () => {
    let j;
    try {
      j = await fetch("/api/job/" + jobId).then((r) => r.json());
    } catch (e) {
      if (onError) onError("lost connection");
      return;
    }
    if (j.state === "done") { if (onDone) onDone(j); return; }
    if (j.state === "error") {
      if (onError) onError(j.error || j.message || "unknown error");
      return;
    }
    if (onProgress) onProgress(j);   // running / superseded-as-running stages
    setTimeout(tick, 1000);
  };
  setTimeout(tick, 1000);
}

// First-run Setup screen: bootstrap the index from the live Messages DB on this
// Mac (Full Disk Access aware) or from a user-provided folder (no FDA). Reuses
// the shared pollJob helper. Note: pollJob's onError callback receives a STRING
// (job.error || job.message), not the job object — so the fda_needed branch
// matches the message string the backend sets ("fda_needed").
function showSetup() {
  document.getElementById("table-scroll").hidden = true;
  const head = document.getElementById("controls"); if (head) head.hidden = true;
  document.getElementById("setup").hidden = false;
  const ss = document.getElementById("setup-status");
  const fda = document.getElementById("setup-fda");

  function runJob(promise) {
    ss.textContent = "Working…"; fda.hidden = true;
    promise.then((r) => r.json()).then((j) => {
      if (j.error) { ss.textContent = j.error; return; }
      pollJob(j.job_id, {
        onProgress: (job) => { ss.textContent = job.message || "Working…"; },
        onDone: () => { ss.textContent = "Done — loading…"; location.reload(); },
        onError: (msg) => {
          if (msg === "fda_needed") { fda.hidden = false; ss.textContent = "Full Disk Access needed."; }
          else { ss.textContent = "Setup failed: " + (msg || "unknown"); }
        },
      });
    }).catch((e) => { ss.textContent = "Setup failed: " + e; });
  }

  const go = () => runJob(fetch("/api/setup/from-mac", { method: "POST" }));
  document.getElementById("setup-mac").onclick = go;
  document.getElementById("setup-retry").onclick = go;
  document.getElementById("setup-folder-go").onclick = () => {
    const folder = document.getElementById("setup-folder").value.trim();
    if (!folder) { ss.textContent = "Enter a folder path."; return; }
    runJob(fetch("/api/setup/from-folder", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder }) }));
  };
}

// Re-pull the generated data + shared state after a rebuild and re-render.
async function reloadAfterRefresh() {
  try {
    const [people] = await Promise.all([
      fetch("/out/people.json").then((r) => r.json()),
      loadSharedState(),
    ]);
    state.all = people;
  } catch (e) { /* keep current data if the re-fetch fails */ }
  try {
    state.stats = await fetch("/out/stats.json").then((r) => r.json());
  } catch (e) { /* leave prior stats */ }
  render();
  renderCounts();
  renderSynced();
}

// Render a failure message in #status with an inline Retry link that re-invokes
// the given starter (no dead-end). Mirrors the #clear link pattern.
function statusWithRetry(msg, retryFn) {
  statusEl.innerHTML = `${escapeHtml(msg)} <a id="retry">Retry</a>`;
  const rt = $("#retry"); if (rt) rt.onclick = retryFn;
}

async function startRefresh() {
  if (refreshing) return;
  setRefreshing(true);
  openRefreshOverlay();
  let job;
  try {
    job = await fetch("/api/refresh", { method: "POST" }).then((r) => r.json());
  } catch (e) {
    closeRefreshOverlay();
    setRefreshing(false);
    statusWithRetry("Refresh failed (server unreachable).", startRefresh);
    return;
  }
  if (!job || !job.job_id) {
    closeRefreshOverlay();
    setRefreshing(false);
    statusWithRetry("Refresh failed: " + ((job && job.error) || "no job id"), startRefresh);
    return;
  }
  // Poll the shared /api/job/<id> route until done/error.
  pollJob(job.job_id, {
    onProgress: (j) => setRefreshStage(refreshStageText(j.message)),
    onDone: async (j) => {
      await reloadAfterRefresh();
      closeRefreshOverlay();
      setRefreshing(false);
      // Brief confirmation using the job result, if present.
      const res = j.result || {};
      let msg = "Refreshed";
      if (typeof res.contacts === "number") {
        msg += res.contacts_synced
          ? ` · ${res.contacts.toLocaleString()} contacts synced`
          : ` · ${res.contacts.toLocaleString()} contacts`;
      } else if (res.snapshotted) {
        msg += " · new messages snapshotted";
      }
      statusEl.textContent = msg;
    },
    onError: (m) => {
      closeRefreshOverlay();
      setRefreshing(false);
      statusWithRetry("Refresh failed: " + m, startRefresh);
    },
  });
}

// Delete a filter, then (for semantic filters) wait on the server's background
// rebuild before reloading. The DELETE persists immediately and returns a
// job_id when a rebuild is needed; we poll it like Refresh so the UI never
// blocks on the rebuild and surfaces the build's stderr inline on failure.
async function deleteFilter(fid) {
  let resp;
  try {
    resp = await fetch("/api/filter/" + encodeURIComponent(fid),
                       { method: "DELETE" }).then((r) => r.json());
  } catch (e) {
    statusEl.textContent = "Delete failed (server unreachable).";
    return;
  }
  if (resp && resp.error) {
    statusEl.textContent = "Delete failed: " + resp.error;
    return;
  }
  // No job_id => nothing to rebuild (e.g. a category filter): reload now.
  if (!resp || !resp.job_id) {
    await reloadAfterRefresh();
    return;
  }
  statusEl.textContent = "Removing filter…";
  pollJob(resp.job_id, {
    onDone: async () => {
      await reloadAfterRefresh();
      statusEl.textContent = "Filter removed";
    },
    onError: (m) => { statusEl.textContent = "Delete failed: " + m; },
  });
}
// Exposed for the filter-management UI (inline handlers) to call.
window.deleteFilter = deleteFilter;

if (refreshBtn) refreshBtn.addEventListener("click", openRefreshModal);

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
  renderCounts();
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

// Toggle/select the sort for a sortable header (shared by click + keyboard).
function sortByHeader(th) {
  const k = th && th.dataset.sort;
  if (!k) return;  // non-sortable header (e.g. the Match column) — ignore
  if (state.sortKey === k) {
    state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
  } else {
    state.sortKey = k;
    state.sortDir = (k === "name" || k === "category") ? "asc" : "desc";
  }
  render();
}

theadEl.addEventListener("click", (e) => {
  sortByHeader(e.target.closest("th"));
});

// Keyboard-operable headers: Enter/Space triggers the same sort as a click.
// preventDefault on Space so the page doesn't scroll.
theadEl.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const th = e.target.closest("th[data-sort]");
  if (!th) return;
  e.preventDefault();
  sortByHeader(th);
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
  // (b) Match snippet -> toggle truncated/full. The WHOLE cell is clickable
  // (not just the text), so you don't have to aim precisely. Never opens Messages.
  const matchCell = e.target.closest("td.col-match");
  if (matchCell) {
    e.stopPropagation();
    const snip = matchCell.querySelector(".snip");
    if (!snip) return;
    const key = snip.dataset.key;
    if (state.expanded.has(key)) state.expanded.delete(key); else state.expanded.add(key);
    // Re-render just this cell to keep other rows / scroll position stable.
    const raw = state.semantic && state.semantic.snippets[key];
    const expanded = state.expanded.has(key);
    const terms = state.semantic ? state.semantic.terms : [];
    snip.innerHTML = expanded ? snippetFullHtml(raw, terms) : snippetTruncatedHtml(raw, terms);
    snip.classList.toggle("expanded", expanded);
    snip.title = expanded ? "Click to collapse" : "Click to expand";
    return;
  }
  // (c) category pill -> inline popover
  const pill = e.target.closest(".pill-btn");
  if (pill) {
    e.stopPropagation();
    openCategoryPopover(pill, pill.dataset.key);
    return;
  }
  // (d) open native Messages for people with a raw_id
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
