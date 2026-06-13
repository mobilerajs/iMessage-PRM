"use strict";

const DATA = "out/"; // generated data lives under ./out (see build.py)
const LS_EXCL = "imsg_crm_excluded";   // "mark as junk" feedback
const LS_APPROVED = "imsg_crm_approved"; // approved name/birthday -> vCard

let PEOPLE = [];
// Active sidebar selection. Values: all | <category-filter-id> | hidden (internal).
// Categories are a MUTUALLY-EXCLUSIVE partition: one person, exactly one category.
let active = "all";
let sortMode = "recent";
let query = "";
// Ephemeral semantic-search results (model-powered, run on Enter). null = no
// search active; otherwise a Set of matching person keys shown as a transient
// view. Never persisted; cleared returns to the normal category view.
let searchKeys = null;
let searchLabel = "";   // the phrase that produced searchKeys (for the header)
let searching = false;  // spinner state while the model runs
let selectedId = null;
let refiningId = null; // filter currently being refined (drives the chip spinner)
// Multi-select: person keys checked for a bulk category move. Cleared when the
// active chip changes. Drives the bulk-action bar.
const SELECTED = new Set();
let lastCheckedKey = null; // anchor for shift-click range select
// Groups are NOT a category — they're surfaced by an orthogonal toggle (default
// OFF). Persisted in localStorage (survives reload without a server round-trip).
const LS_SHOW_GROUPS = "imsg_crm_show_groups";
let showGroups = localStorage.getItem(LS_SHOW_GROUPS) === "1";

// User state lives on disk (via the server) so it isn't tied to one browser;
// localStorage is a fallback when running static with no server.
let EXCLUDED = loadLS(LS_EXCL);
let APPROVED = loadLS(LS_APPROVED);
let SAVED = loadLS("imsg_crm_saved"); // key -> name written to Contacts
let DISMISSED = loadLS("imsg_crm_dismissed"); // key -> true (name suggestion dismissed)
// Per-person category override: { <personKey>: <Category> }. Disk-backed via
// userstate so a manual recategorization survives reload AND a build.py rebuild
// (it lives here, not in people.json). The exclusive partition is enforced by
// effectiveCategory(): an override REPLACES the person's computed category.
const LS_CATOVR = "imsg_crm_catoverride";
let CAT_OVERRIDE = loadLS(LS_CATOVR);

async function loadState() {
  const n = (o) => Object.keys(o || {}).length;
  const localHad = n(EXCLUDED) + n(APPROVED) + n(SAVED) + n(DISMISSED) + n(CAT_OVERRIDE);
  try {
    const s = await fetch("/api/userstate").then((r) => r.json());
    const serverHad = n(s.excluded) + n(s.approved) + n(s.saved) + n(s.dismissed) + n(s.catOverride);
    if (serverHad === 0 && localHad > 0) {
      persistState(); // one-time migration: push existing browser state to disk
    } else {
      EXCLUDED = s.excluded || {}; APPROVED = s.approved || {};
      SAVED = s.saved || {}; DISMISSED = s.dismissed || {};
      CAT_OVERRIDE = s.catOverride || {};
    }
  } catch (e) { /* static mode: keep the localStorage values */ }
}

function persistState() {
  // Mirror to localStorage immediately, and to disk via the server if present.
  saveLS(LS_EXCL, EXCLUDED); saveLS(LS_APPROVED, APPROVED);
  saveLS("imsg_crm_saved", SAVED); saveLS("imsg_crm_dismissed", DISMISSED);
  saveLS(LS_CATOVR, CAT_OVERRIDE);
  fetch("/api/userstate", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ excluded: EXCLUDED, approved: APPROVED, saved: SAVED, dismissed: DISMISSED, catOverride: CAT_OVERRIDE }),
  }).catch(() => {});
}

const $ = (sel) => document.querySelector(sel);
const listEl = $("#list");

const COLORS = ["#0b84fe", "#ff9500", "#34c759", "#af52de", "#ff2d55",
  "#5ac8fa", "#ffcc00", "#ff3b30", "#5856d6", "#64d2ff"];
const CAT_COLOR = {
  Family: "#ff2d55", Friend: "#34c759", Professional: "#0b84fe",
  Service: "#ff9500", Acquaintance: "#8e8e93", "Wrong number": "#c1121f",
};
const MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
  "Sep", "Oct", "Nov", "Dec"];

function loadLS(k) { try { return JSON.parse(localStorage.getItem(k)) || {}; } catch (e) { return {}; } }
function saveLS(k, v) { localStorage.setItem(k, JSON.stringify(v)); }

function initials(name) {
  const parts = name.replace(/[^\p{L}\p{N} ]/gu, "").trim().split(/\s+/);
  if (!parts[0]) return "#";
  return (parts[0][0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}
function colorFor(name) {
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return COLORS[h % COLORS.length];
}
function avatarHTML(p, cls) {
  if (p.photo) return `<div class="avatar ${cls}"><img src="${DATA}${p.photo}" alt=""></div>`;
  const bg = p.kind === "group" ? "" : `style="background:${colorFor(p.name)}"`;
  return `<div class="avatar ${cls} ${p.kind === "group" ? "group" : ""}" ${bg}>${initials(p.name)}</div>`;
}
function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const days = Math.floor((now - d) / 86400000);
  if (days === 0) return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  if (days < 7) return d.toLocaleDateString([], { weekday: "short" });
  return d.toLocaleDateString([], d.getFullYear() === now.getFullYear()
    ? { month: "short", day: "numeric" } : { month: "short", day: "numeric", year: "2-digit" });
}
function daysSince(iso) {
  if (!iso) return 0;
  return Math.max(0, Math.round((Date.now() - new Date(iso)) / 86400000));
}
function recencyLabel(p) {
  const d = daysSince(p.last_date);
  if (d <= 1) return "talked today";
  if (d <= 14) return `talked ${d}d ago`;
  if (d < 365) return `quiet ${Math.round(d / 30)}mo`;
  return `quiet ${(d / 365).toFixed(1)}y`;
}
function bdayLabel(b) { return b ? `${MONTHS[b.month]} ${b.day}` : ""; }
function daysUntilBday(b) {
  if (!b) return 9999;
  const now = new Date();
  let next = new Date(now.getFullYear(), b.month - 1, b.day);
  if (next < new Date(now.getFullYear(), now.getMonth(), now.getDate())) next.setFullYear(now.getFullYear() + 1);
  return Math.round((next - new Date(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000);
}

let FILTERS = []; // loaded from out/filters.json
const filterById = (id) => FILTERS.find((f) => f.id === id);

// The category a chip filter represents: built-ins carry it on rule.category,
// user-added (type:"category") on .category. Non-category filters return null.
const chipCategory = (f) => (f && (f.category || (f.rule && f.rule.category))) || null;

// Color a category label to match its sidebar chip. Looks up the chip whose
// category equals the given name. Falls back to neutral gray.
function catColor(category) {
  const f = FILTERS.find((x) => chipCategory(x) === category);
  return (f && f.color) || "#8e8e93";
}
// Map a category VALUE (e.g. "Personal") to its chip's display NAME (e.g. "Other"),
// so renamed chips show their label, not the underlying value. Falls back to value.
function catName(category) {
  const f = FILTERS.find((x) => chipCategory(x) === category);
  return (f && f.name) || category;
}

// personKey -> user-category name, precomputed once per render from the
// type:"category" filters' `keys` arrays (avoids scanning every filter per
// person). Rebuilt by rebuildCategoryIndex() whenever FILTERS changes.
let CATEGORY_KEY_MAP = {};
function rebuildCategoryIndex() {
  CATEGORY_KEY_MAP = {};
  for (const f of FILTERS) {
    if (f.type !== "category" || !Array.isArray(f.keys)) continue;
    const cat = chipCategory(f);
    if (!cat) continue;
    for (const k of f.keys) {
      // First category filter in FILTERS order wins (matches the priority walk).
      if (!(k in CATEGORY_KEY_MAP)) CATEGORY_KEY_MAP[k] = cat;
    }
  }
}

// The person's CURRENT category — single source of truth for the exclusive
// partition. Priority walk: (1) manual override always wins; (2) else a
// model-assigned user-category membership (type:"category" keys); (3) else the
// base built-in partition (p.category). Returning one value keeps chips exclusive.
function effectiveCategory(p) {
  return CAT_OVERRIDE[p.key] || CATEGORY_KEY_MAP[p.key] || p.category;
}

function isHidden(p) { return !!EXCLUDED[p.key]; }

// Internal-only "hidden" view (reachable from the ⋯ overflow menu, NOT a sidebar
// chip): lists EXCLUDED people so they can be un-hidden.
const HIDDEN_VIEW = "hidden";

// A group conversation is suppressed from every category view; it only appears in
// "All" when the Show-groups toggle is ON. Category chips (rule.kind==='person')
// never match groups regardless of the toggle.
function groupSuppressed(p) { return p.kind === "group" && !showGroups; }

function passesComputed(p, r) {
  // Category comparisons use the EFFECTIVE category (manual override > computed),
  // so chip membership reflects bulk/right-click moves exclusively.
  const ec = effectiveCategory(p);
  if (r.kind && p.kind !== r.kind) return false;
  if (r.not_kind && p.kind === r.not_kind) return false;
  if (r.in_contacts && !p.in_contacts) return false;
  if (r.not_in_contacts && p.in_contacts) return false;
  if (r.category && ec !== r.category) return false;
  if (r.not_category && ec === r.not_category) return false;
  if (r.has_birthday && !p.birthday) return false;
  if (r.has_suggestion && !p.suggested_name) return false;
  if (r.min_count != null && p.count < r.min_count) return false;
  if (r.max_count != null && p.count > r.max_count) return false;
  if (r.two_way && !(p.sent > 0 && p.recv > 0)) return false;
  if (r.min_days_since != null && daysSince(p.last_date) < r.min_days_since) return false;
  if (r.max_days_since != null && daysSince(p.last_date) > r.max_days_since) return false;
  return true;
}

// Evaluate one filter object against a conversation.
function passesFilter(p, f) {
  // Category chips (built-in type:"computed" with rule.category, OR user-added
  // type:"category") are the EXCLUSIVE partition over PEOPLE: membership =
  // person AND effective category equals the chip's category. The person guard
  // preserves the built-ins' original rule.kind==="person" (groups can carry a
  // category in the data but are never part of the people partition).
  const cat = chipCategory(f);
  if (cat) return p.kind === "person" && effectiveCategory(p) === cat;
  if (f.exclude_keys && f.exclude_keys.includes(p.key)) return false; // manual exception
  if (f.include_keys && f.include_keys.includes(p.key)) return true;  // manual addition
  if (f.type === "computed") return passesComputed(p, f.rule || {});
  if (f.type === "keyword") return (f.keywords || []).some((k) => (p.blob || "").includes(k.toLowerCase()));
  if (f.type === "semantic") return (p.tags || []).includes(f.name);
  return true;
}

function matches(p) {
  // Semantic-search results are a transient view: show exactly the model-matched
  // keys (hidden people still excluded), ignoring the active category chip.
  if (searchKeys) {
    if (!searchKeys.has(p.key)) return false;
    if (isHidden(p)) return false;
    return true;
  }
  if (active === HIDDEN_VIEW) { if (!isHidden(p)) return false; }
  else {
    if (isHidden(p)) return false; // hidden people are excluded from every category view
    if (groupSuppressed(p)) return false; // groups appear only in All when toggle is ON
    if (active !== "all") {
      const f = filterById(active);
      if (f && !passesFilter(p, f)) return false;
    }
  }
  if (query) {
    const q = query.toLowerCase();
    const hit = nameMatches(p.name, q) ||
      nameMatches(p.suggested_name || "", q) ||
      (p.members || []).some((m) => nameMatches(m, q)) ||
      (p.raw_id || "").toLowerCase().includes(q); // numbers/emails: substring
    if (!hit) return false;
  }
  return true;
}

// Match when any name part (first / last / …) STARTS with the query.
function nameMatches(name, q) {
  return (name || "").toLowerCase().split(/[^a-z0-9]+/).some((tok) => tok && tok.startsWith(q));
}

function countFilter(f) {
  return PEOPLE.reduce((n, p) => n + (!isHidden(p) && passesFilter(p, f) ? 1 : 0), 0);
}

function renderFilters() {
  const chip = (id, label, color, n) =>
    `<button class="fchip ${active === id ? "on" : ""}" data-f="${id}"` +
    (color ? ` style="--cc:${color}"` : "") + `>${label}` +
    (n != null ? ` <em>${n}</em>` : "") + `</button>`;

  const filterChip = (f) => {
    const nCorr = f.type === "semantic" ? ((f.exclude_keys || []).length + (f.include_keys || []).length) : 0;
    const refining = refiningId === f.id;
    const refineIcon = nCorr
      ? ` <span class="chip-refine ${refining ? "spin" : ""}" data-refine="${f.id}"` +
        ` title="Refine from ${nCorr} correction${nCorr > 1 ? "s" : ""}">↻</span>` : "";
    return `<button class="fchip ${active === f.id ? "on" : ""}" data-f="${f.id}" style="--cc:${f.color || "#5856d6"}">` +
      `${escapeHtml(f.name)} <em>${countFilter(f)}</em>${refineIcon}` +
      ` <span class="chip-edit" data-edit="${f.id}" title="Edit filter">✎</span>` +
      ` <span class="chip-x" data-del="${f.id}" title="Delete filter">×</span></button>`;
  };

  // "All" = everyone visible: not hidden, and (when the toggle is OFF) persons only.
  const allCount = PEOPLE.filter((p) => !isHidden(p) && !groupSuppressed(p)).length;
  const all = chip("all", "All", "#8e8e93", allCount);

  // Exclusive category chips come straight from filters.json: the four built-ins
  // plus any user-added type:"category". A person is in exactly one (resolved via
  // effectiveCategory). Groups aren't a category; hidden people live in ⋯ menu.
  const chips = FILTERS.map(filterChip).join("");
  $("#filterbar").innerHTML = `<div class="frow">${all}${chips}</div>`;
}

function renderList() {
  let rows = PEOPLE.filter(matches);
  // The Sort dropdown always controls ordering. Filters only narrow.
  const inHidden = active === HIDDEN_VIEW;
  const af = (active !== "all" && !inHidden) ? filterById(active) : null;
  const inSugg = !!(af && af.rule && af.rule.has_suggestion);
  const inBday = false;

  // The "Name to add" view defaults to best-suggestion-first, but any explicit
  // Sort choice still overrides it.
  if (inSugg && sortMode === "recent") {
    const rank = { high: 0, medium: 1, low: 2 };
    rows.sort((a, b) => (rank[a.name_confidence] ?? 3) - (rank[b.name_confidence] ?? 3));
  } else if (sortMode === "name") {
    rows.sort((a, b) => (a.name || "").localeCompare(b.name || "", undefined, { sensitivity: "base", numeric: true }));
  } else if (sortMode === "most") {
    rows.sort((a, b) => b.count - a.count);
  } else if (sortMode === "least") {
    rows.sort((a, b) => a.count - b.count);
  } else if (sortMode === "oldest") {
    rows.sort((a, b) => (a.last_date > b.last_date ? 1 : -1));
  } else {
    rows.sort((a, b) => (a.last_date < b.last_date ? 1 : -1));
  }

  const nHidden = Object.keys(EXCLUDED).length;
  if (searching) {
    $("#count").innerHTML = `<span class="searching"><span class="spin-dot"></span>searching “${escapeHtml(searchLabel)}”…</span>`;
  } else if (searchKeys) {
    $("#count").innerHTML = `${rows.length} result${rows.length === 1 ? "" : "s"} for ` +
      `“${escapeHtml(searchLabel)}” · <a href="#" id="clearsearch">Clear</a>`;
  } else {
    $("#count").innerHTML = `${rows.length} shown · ${PEOPLE.length} total · ${nHidden} hidden`;
  }

  listEl.innerHTML = rows.map((p) => {
    // Relationship facts line.
    const overdue = daysSince(p.last_date) > 120;
    const facts = [];
    if (p.birthday) facts.push(`<span class="bd">🎂 ${bdayLabel(p.birthday)}</span>`);
    if (p.kind === "group") facts.push(`${p.members.length} ppl`);
    facts.push(`<span class="${overdue ? "overdue" : ""}">${recencyLabel(p)}</span>`);
    facts.push(`${p.count.toLocaleString()} msgs`);
    const factsLine = `<div class="row-facts">${facts.join(" · ")}</div>`;

    // Exclusive categories: one static label per row (the person's single
    // category) — no remove-from-filter ×, no multi-membership. Groups show a
    // "Group" marker instead. An `unsure` person (low-confidence guess, defaults
    // to Work) carries a subtle marker so the user can spot guesses.
    let memberLine = "";
    if (p.kind === "group") {
      memberLine = `<div class="row-members"><span class="cat-label" style="--cc:#8e8e93">Group</span></div>`;
    } else {
      const ec = effectiveCategory(p);
      if (ec) {
        const cc = catColor(ec);
        const moved = CAT_OVERRIDE[p.key] ? ` <span class="cat-moved" title="Manually moved">moved</span>` : "";
        const unsure = (!CAT_OVERRIDE[p.key] && p.unsure) ? ` <span class="cat-unsure" title="Low-confidence guess">?</span>` : "";
        memberLine = `<div class="row-members"><span class="cat-label" style="--cc:${cc}">${escapeHtml(catName(ec))}</span>${moved}${unsure}</div>`;
      }
    }

    // Inline name suggestion + Save + Dismiss (for numbers / first-name-only).
    // Saved ones become normal contact rows (handled by applyContactOverrides).
    let suggest = "";
    if (p.suggested_name && !SAVED[p.key] && !DISMISSED[p.key]) {
      suggest = `<div class="row-suggest"><span class="sg-name">Possible name: <b>${escapeHtml(p.suggested_name)}</b></span>` +
        `<button class="sg-btn" data-key="${p.key}" data-act="savecontact">Save</button>` +
        `<button class="sg-x" data-key="${p.key}" title="Dismiss this suggestion">×</button></div>`;
    }

    const msgLink = (p.kind === "person" && p.raw_id)
      ? `<a class="row-msg" href="imessage://${encodeURIComponent(p.raw_id)}" title="Message in iMessage">💬</a>` : "";
    const right = `${msgLink}<span class="row-date">${fmtDate(p.last_date)}</span>`;
    // In the Hidden view (reached from ⋯ overflow), each row offers Unhide. There
    // is no per-row remove-from-category: categories are exclusive; reassignment
    // happens via the right-click "Move to <category>" menu.
    const action = inHidden
      ? `<button class="rowbtn unhide" data-key="${p.key}" title="Unhide (restore to your lists)">Unhide</button>`
      : "";
    const snip = `<div class="row-snip">${escapeHtml(p.snippet || "")}</div>`;

    // Selection checkbox for bulk category move (people only; groups have no
    // category). Hidden in the internal Hidden view. The whole control carries
    // data-sel so a click anywhere on it is caught (handled in JS, not native).
    const isSel = SELECTED.has(p.key);
    const checkbox = (p.kind === "person" && !inHidden)
      ? `<span class="row-check" data-sel="${escapeHtml(p.key)}" title="Select">` +
        `<input type="checkbox" tabindex="-1"${isSel ? " checked" : ""}></span>`
      : "";

    return `
      <li class="row ${p.id === selectedId ? "sel" : ""}${isSel ? " checked" : ""}" data-id="${p.id}">
        ${checkbox}
        ${avatarHTML(p, "")}
        <div class="row-body">
          <div class="row-top">
            <span class="row-name">${escapeHtml(p.name)}</span>
            ${right}
          </div>
          ${factsLine}
          ${memberLine}
          ${suggest}
          ${snip}
        </div>
        ${action}
      </li>`;
  }).join("");

  renderBulkBar();
}

// Every category chip as {category, label, color} — the four built-ins AND every
// user-added type:"category". Labels come from filters.json so a renamed chip
// (e.g. "personal" -> "Other") shows its real display name. `category` is the
// value written to CAT_OVERRIDE (rule.category for built-ins, .category for user).
function categoryTargets() {
  return FILTERS
    .map((f) => ({ f, category: chipCategory(f) }))
    .filter((x) => x.category)
    .map((x) => ({ category: x.category, label: x.f.name, color: x.f.color || "#8e8e93" }));
}

// Bulk-action bar: shown whenever ≥1 person is selected. "{n} selected · Move to
// [chips] · Clear". Targets and labels are pulled live from the filter chips.
function renderBulkBar() {
  const bar = $("#bulkbar");
  if (!bar) return;
  if (SELECTED.size === 0) { bar.hidden = true; bar.innerHTML = ""; return; }
  const targets = categoryTargets().map((t) =>
    `<button class="bulk-move" data-cat="${escapeHtml(t.category)}" style="--cc:${t.color}">` +
    `${escapeHtml(t.label)}</button>`).join("");
  bar.hidden = false;
  bar.innerHTML =
    `<span class="bulk-n">${SELECTED.size} selected</span>` +
    `<span class="bulk-sep">·</span><span class="bulk-label">Move to</span>${targets}` +
    `<span class="bulk-sep">·</span><button class="bulk-hide">Hide</button>` +
    `<button class="bulk-clear">Clear</button>`;
}

// Apply a category to every selected person via the persistent override, once.
function bulkMoveTo(category) {
  if (!category || SELECTED.size === 0) return;
  for (const key of SELECTED) CAT_OVERRIDE[key] = category;
  persistState();              // one disk write for the whole batch
  clearSelection();            // selection resets after a move
  renderFilters();             // chip counts shift (exclusive)
  renderList();                // moved rows leave a category view they no longer match
}

// Hide every selected person via the existing EXCLUDED path, persisting ONCE.
// They leave every view and are recoverable via ⋯ "Hidden (N)" -> Unhide.
function bulkHide() {
  if (SELECTED.size === 0) return;
  for (const key of SELECTED) {
    const p = PEOPLE.find((x) => x.key === key);
    if (p) markExcluded(p, "");   // set EXCLUDED entry, no per-row persist
  }
  persistState();                 // one disk write for the whole batch
  clearSelection();
  renderFilters();                // "All"/chip counts drop hidden people
  renderList();
}

function clearSelection() {
  SELECTED.clear();
  lastCheckedKey = null;
}

// ---- semantic search (model-powered, on Enter) ----------------------------
// True name-prefix matches in the current data — used to decide whether Enter
// should fall back to a semantic content search.
function hasNameMatch(q) {
  const lc = q.toLowerCase();
  return PEOPLE.some((p) => !isHidden(p) && (
    nameMatches(p.name, lc) || nameMatches(p.suggested_name || "", lc) ||
    (p.members || []).some((m) => nameMatches(m, lc)) ||
    (p.raw_id || "").toLowerCase().includes(lc)));
}

// Run a semantic search over conversation CONTENT via the local model. Shows a
// spinner, then renders the matched people as a transient view. Ephemeral —
// nothing is saved. Triggered on Enter for phrases (or when nothing matches by
// name); per-keystroke name search is untouched.
async function runSemanticSearch(q) {
  searching = true; searchKeys = null; searchLabel = q;
  selectedId = null;
  renderList(); // show the spinner immediately
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`).then((x) => x.json());
    searching = false;
    if (r.error) {
      searchKeys = null;
      $("#count").innerHTML = `⚠️ ${escapeHtml(r.error)} · <a href="#" id="clearsearch">Clear</a>`;
      return;
    }
    searchKeys = new Set(r.keys || []);
    renderList();
  } catch (e) {
    searching = false; searchKeys = null;
    $("#count").innerHTML = `⚠️ No server — semantic search needs server.py · <a href="#" id="clearsearch">Clear</a>`;
  }
}

// Leave search-results mode and return to the normal category view.
function clearSearch() {
  searchKeys = null; searchLabel = ""; searching = false;
  query = "";
  const box = $("#search"); if (box) box.value = "";
  renderList();
}

// Enter in the search box: phrases (>2 words) or queries with no name hit run a
// semantic search; otherwise the instant name search already shown stands.
function onSearchEnter() {
  const q = query.trim();
  if (!q) { clearSearch(); return; }
  const isPhrase = q.split(/\s+/).length > 2;
  if (isPhrase || !hasNameMatch(q)) runSemanticSearch(q);
}

// Toggle one person's selection. Shift-click selects the contiguous range
// between the last-clicked row and this one (in current display order).
function toggleSelect(key, shift) {
  const keysInOrder = [...listEl.querySelectorAll("[data-sel]")].map((el) => el.dataset.sel);
  if (shift && lastCheckedKey && lastCheckedKey !== key) {
    const a = keysInOrder.indexOf(lastCheckedKey);
    const b = keysInOrder.indexOf(key);
    if (a !== -1 && b !== -1) {
      const [lo, hi] = a < b ? [a, b] : [b, a];
      // Shift-range adds (matches the anchor row's resulting state = selected).
      for (let i = lo; i <= hi; i++) SELECTED.add(keysInOrder[i]);
      lastCheckedKey = key;
      renderList();
      return;
    }
  }
  if (SELECTED.has(key)) SELECTED.delete(key); else SELECTED.add(key);
  lastCheckedKey = key;
  renderList();
}

function clearThread() {
  selectedId = null;
  $("#thread-head").hidden = true;
  const roster = $("#th-roster"); if (roster) roster.hidden = true;
  $("#messages").innerHTML = "";
  $("#jump-nav").hidden = true;
  $("#empty").hidden = false;
}

async function openConvo(id) {
  selectedId = id;
  renderList();
  const p = PEOPLE.find((x) => x.id === id);
  if (!p) return;

  $("#empty").hidden = true;
  $("#thread-head").hidden = false;
  $("#jump-nav").hidden = false;
  $("#th-avatar").outerHTML = avatarHTML(p, "").replace('class="avatar ', 'id="th-avatar" class="avatar ');
  $("#th-name").textContent = p.name;

  const sub = [`${p.count.toLocaleString()} messages`,
    `${p.sent.toLocaleString()} sent / ${p.recv.toLocaleString()} received`];
  const ec = effectiveCategory(p);
  if (ec) sub.push(catName(ec));
  if (p.birthday) sub.push(`🎂 ${bdayLabel(p.birthday)} (${p.birthday.confidence})`);
  if (p.kind === "person" && !p.in_contacts) sub.push("not in contacts · " + p.raw_id);
  $("#th-sub").textContent = sub.join(" · ");

  const roster = $("#th-roster");
  if (p.kind === "group" && p.members && p.members.length) {
    roster.hidden = false;
    roster.innerHTML = `<span class="roster-label">${p.members.length} members:</span> ` +
      p.members.map((m) => `<span class="chip">${escapeHtml(m)}</span>`).join("");
  } else { roster.hidden = true; roster.innerHTML = ""; }

  // Open in Messages to actually send a message (1:1 people only).
  const msgBtn = $("#msg-btn");
  if (p.kind === "person" && p.raw_id) {
    msgBtn.href = `imessage://${encodeURIComponent(p.raw_id)}`;
    msgBtn.hidden = false;
  } else {
    msgBtn.hidden = true;
  }

  // In Contacts → open the card; not in Contacts → add them (even with no suggestion).
  const contactBtn = $("#contact-btn");
  if (p.kind === "person") {
    contactBtn.hidden = false;
    if (p.in_contacts) { contactBtn.textContent = "👤 Contact"; contactBtn.dataset.mode = "open"; contactBtn.dataset.name = p.name; }
    else { contactBtn.textContent = "👤 Add to Contacts"; contactBtn.dataset.mode = "add"; contactBtn.dataset.key = p.key; }
  } else { contactBtn.hidden = true; }

  const hideBtn = $("#hide-btn");
  hideBtn.textContent = EXCLUDED[p.key] ? "Unhide" : "Hide";

  const msgsEl = $("#messages");
  msgsEl.innerHTML = `<div class="daysep">Loading…</div>`;
  let msgs;
  try { msgs = await fetch(`${DATA}messages/${id}.json`).then((r) => r.json()); }
  catch (e) { msgsEl.innerHTML = `<div class="daysep">Could not load conversation.</div>`; return; }
  renderMessages(p, msgs);
}

function renderMessages(p, msgs) {
  const isGroup = p.kind === "group";
  const out = [];
  let lastDay = "", lastSender = "";
  for (const m of msgs) {
    const d = m.date ? new Date(m.date) : null;
    const day = d ? d.toDateString() : "";
    if (day !== lastDay) {
      out.push(`<div class="daysep">${d ? d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric", year: "numeric" }) : ""}</div>`);
      lastDay = day; lastSender = "";
    }
    const side = m.me ? "me" : "them";
    const showSender = isGroup && !m.me && m.from !== lastSender;
    lastSender = m.from;
    const senderTag = showSender ? `<div class="sender">${escapeHtml(m.from)}</div>` : "";
    const attach = m.text === "[attachment]" ? "attach" : "";
    const time = d ? d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
    out.push(`${senderTag}<div class="msg ${side}"><div class="bubble ${attach}">${escapeHtml(m.text)}</div><div class="time">${time}</div></div>`);
  }
  const el = $("#messages");
  el.innerHTML = out.join("");
  el.scrollTop = el.scrollHeight;
  updateJumpNav();
}

// Show each jump button only when there's somewhere to go in that direction —
// ↑ hidden when already at the top, ↓ hidden when already at the bottom.
function updateJumpNav() {
  if ($("#jump-nav").hidden) return;
  const el = $("#messages");
  const atTop = el.scrollTop <= 8;
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 8;
  $("#jump-top").classList.toggle("gone", atTop);
  $("#jump-bottom").classList.toggle("gone", atBottom);
}

// ---- feedback (hide) ----
// Set the EXCLUDED entry WITHOUT persisting — lets a bulk op write many then
// persist once. hideConvo() is the single-person path and still persists.
function markExcluded(p, reason) {
  EXCLUDED[p.key] = { name: p.name, raw_id: p.raw_id, kind: p.kind, snippet: p.snippet || "", reason: reason || "", ts: new Date().toISOString() };
}
function hideConvo(p, reason) {
  markExcluded(p, reason);
  persistState();
}
function unhide(key) { delete EXCLUDED[key]; persistState(); }

function exportFeedback() {
  const keys = Object.keys(EXCLUDED);
  if (!keys.length) { showModal({ title: "Nothing hidden yet", message: "Use the Hide button on conversations first.", cancelLabel: null }); return; }
  download("feedback.json", { exported_at: new Date().toISOString(),
    note: "Hand to Claude to bake into data/exclude.json.",
    keys, entries: keys.map((k) => ({ key: k, ...EXCLUDED[k] })) });
}

// ---- approvals -> vCard ----
function approveName(p) {
  APPROVED[p.key] = { name: p.suggested_name, phone: p.raw_id,
    birthday: p.birthday || null, existing: false };
  persistState();
}
function approveBday(p) {
  APPROVED[p.key] = { name: p.name, phone: p.in_contacts ? "" : p.raw_id,
    birthday: p.birthday || null, existing: p.in_contacts };
  persistState();
}
function unapprove(key) { delete APPROVED[key]; persistState(); }

// Review + write a suggested name into Contacts (update existing or create new).
async function saveContact(p) {
  const bdayMsg = p.birthday ? `  ·  🎂 ${bdayLabel(p.birthday)} (will be added)` : "";
  const vals = await showModal({
    title: "Add to Contacts",
    message: `${p.raw_id}${bdayMsg}  ·  ${p.name_reason || "inferred from conversation"}\n\n` +
      "I'll first check Contacts for an existing match by this name — if one exists, " +
      "you can add the number to it; otherwise I'll create a new contact.",
    fields: [{ name: "name", label: "Name", value: p.suggested_name || "" }],
    confirmLabel: "Continue",
  });
  if (!vals || !vals.name) return;
  const name = vals.name.trim();
  const bar = $("#jobbar"); bar.hidden = false;

  // Search Contacts for an existing person to merge into.
  bar.textContent = `Checking Contacts for "${name}"…`;
  let candidates = [];
  try {
    candidates = ((await fetch("/api/contacts/search", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }).then((x) => x.json())).candidates) || [];
  } catch (e) { /* offline / static */ }

  let contactId = null;
  if (candidates.length) {
    const choice = await showModal({
      title: "Possible existing contact",
      message: `"${name}" may already be in your Contacts. Add this number to one of them, or create a new contact?`,
      choices: [
        ...candidates.map((c) => ({ label: `Add ${p.raw_id} to “${c.name}”${c.phone ? ` (${c.phone})` : ""}`, value: c.id })),
        { label: "Create as a new contact", value: "__new__", primary: true },
      ],
    });
    if (choice === null) { bar.hidden = true; return; }
    if (choice !== "__new__") contactId = choice;
  }

  bar.hidden = false;
  bar.textContent = `Saving "${name}" → Contacts…`;
  try {
    const r = await fetch("/api/contacts/update", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, raw_id: p.raw_id, birthday: p.birthday || null, contact_id: contactId }),
    }).then((x) => x.json());
    if (r.error) { bar.textContent = "⚠️ " + r.error; return; }
    SAVED[p.key] = name;
    persistState();
    // Reflect the write in the CRM immediately (our data is a snapshot).
    p.name = name; p.in_contacts = true; p.suggested_name = null;
    if (selectedId === p.id) openConvo(p.id); else renderList();
    const verb = r.action === "merged" ? "Merged into" : r.action === "updated" ? "Updated" : "Added";
    bar.textContent = `✓ ${verb} "${name}" in Contacts`;
    setTimeout(() => { bar.hidden = true; }, 2500);
  } catch (e) {
    bar.textContent = "⚠️ No server — run server.py to write to Contacts.";
  }
}

// Correct the name of a contact you saved from here (display + the name used to
// open it in Contacts). Doesn't re-write Contacts, so no duplicate numbers.
async function fixSavedName(p) {
  const cur = (SAVED[p.key] || p.name || "").replace(/\s*\(merged\)$/, "");
  const vals = await showModal({
    title: "Fix saved name",
    message: "Correct the name shown here and used to open the contact.",
    fields: [{ name: "name", label: "Name", value: cur }],
    confirmLabel: "Save",
  });
  if (!vals || !vals.name) return;
  SAVED[p.key] = vals.name.trim();
  persistState();
  p.name = vals.name.trim();
  if (selectedId === p.id) openConvo(p.id); else renderList();
}

// Re-derive a semantic filter from the user's remove/move corrections so the
// model generalizes (similar contacts get fixed too), not just the exact rows.
async function refineFilter(fid) {
  if (refiningId) return; // one at a time
  const f = filterById(fid);
  refiningId = fid; renderFilters(); // start chip spinner
  let res;
  try {
    res = await fetch(`/api/filter/${fid}/refine`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.json());
  } catch (e) { refiningId = null; renderFilters(); return; }
  if (res.error) {
    refiningId = null; renderFilters();
    const bar = $("#jobbar"); bar.hidden = false; bar.textContent = "⚠️ " + res.error;
    setTimeout(() => { bar.hidden = true; }, 4000);
    return;
  }
  const poll = setInterval(async () => {
    const j = await fetch(`/api/job/${res.job_id}`).then((r) => r.json());
    if (j.state === "running") return; // chip keeps spinning
    clearInterval(poll);
    refiningId = null;
    if (j.state === "done") {
      await reloadData();
      const bar = $("#jobbar"); bar.hidden = false;
      bar.textContent = `✓ Refined “${f ? f.name : fid}” — now ${j.result.matches} matches`;
      setTimeout(() => { bar.hidden = true; }, 2500);
    } else { renderFilters(); }
  }, 1000);
}

// Reassign one person's category via the persistent override (exclusive: this
// REPLACES their effective category). Survives reload + build.py rebuild.
function setCategory(key, category) {
  CAT_OVERRIDE[key] = category;
  persistState();
  renderFilters();
  renderList();
}

// ---- right-click context menu on a row ----
let ctxEl = null;
function hideContextMenu() { if (ctxEl) { ctxEl.remove(); ctxEl = null; } }
function showContextMenu(x, y, p) {
  hideContextMenu();
  const items = [];
  if (p.kind === "person" && !p.in_contacts && !SAVED[p.key]) items.push({ label: "Add to Contacts…", fn: () => saveContact(p) });
  if (SAVED[p.key]) items.push({ label: "Fix saved name…", fn: () => fixSavedName(p) });
  // Reassign the person's single category via the persistent override. The
  // effective category is the current one; offer every other category as a
  // target. Groups have no category, so no "Move to" for them.
  if (p.kind === "person") {
    const cur = effectiveCategory(p);
    const targets = categoryTargets().filter((t) => t.category !== cur);
    if (targets.length) {
      items.push({ header: "Move to" });
      for (const t of targets) {
        items.push({ label: `→ ${t.label}`, fn: () => setCategory(p.key, t.category) });
      }
    }
  }
  items.push({ sep: true });
  items.push({
    label: EXCLUDED[p.key] ? "Unhide" : "Hide conversation",
    fn: () => { if (EXCLUDED[p.key]) unhide(p.key); else hideConvo(p, ""); renderFilters(); renderList(); },
  });

  showMenu(x, y, items);
}

// Generic floating menu used by right-click and the overflow button.
function showMenu(x, y, items) {
  hideContextMenu();
  const menu = document.createElement("div");
  menu.className = "ctxmenu";
  menu.innerHTML = items.map((it, i) =>
    it.sep ? `<div class="ctx-sep"></div>`
      : it.header ? `<div class="ctx-h">${escapeHtml(it.header)}</div>`
        : `<div class="ctx-item" data-i="${i}">${escapeHtml(it.label)}</div>`).join("");
  document.body.appendChild(menu);
  menu.style.left = Math.min(x, window.innerWidth - menu.offsetWidth - 8) + "px";
  menu.style.top = Math.min(y, window.innerHeight - menu.offsetHeight - 8) + "px";
  menu.querySelectorAll(".ctx-item").forEach((el) =>
    el.addEventListener("click", () => { hideContextMenu(); items[+el.dataset.i].fn(); }));
  ctxEl = menu;
  setTimeout(() => document.addEventListener("click", hideContextMenu, { once: true }), 0);
}

async function deleteFilter(id) {
  const f = filterById(id);
  const ok = await showModal({ title: "Delete filter",
    message: `Delete the "${f ? f.name : id}" filter?`, confirmLabel: "Delete", danger: true });
  if (!ok) return;
  const bar = $("#jobbar"); bar.hidden = false; bar.textContent = `Deleting…`;
  try {
    await fetch(`/api/filter/${id}`, { method: "DELETE" });
    if (active === id) active = "all";
    await reloadData();
    bar.hidden = true;
  } catch (e) { bar.textContent = "⚠️ No server — delete needs server.py."; }
}

function exportVCard() {
  const entries = Object.values(APPROVED);
  if (!entries.length) { showModal({ title: "Nothing to export", message: "Approve some name or birthday suggestions first.", cancelLabel: null }); return; }
  let vcf = "";
  for (const e of entries) {
    vcf += "BEGIN:VCARD\nVERSION:3.0\n";
    vcf += `FN:${e.name}\n`;
    const parts = (e.name || "").split(" ");
    vcf += `N:${parts.slice(1).join(" ")};${parts[0]};;;\n`;
    if (e.phone && e.phone.includes("@")) vcf += `EMAIL:${e.phone}\n`;
    else if (e.phone) vcf += `TEL;TYPE=CELL:${e.phone}\n`;
    if (e.birthday) vcf += `BDAY:--${String(e.birthday.month).padStart(2, "0")}-${String(e.birthday.day).padStart(2, "0")}\n`;
    if (e.existing) vcf += `NOTE:iMessage CRM — birthday update for existing contact\n`;
    vcf += "END:VCARD\n";
  }
  const blob = new Blob([vcf], { type: "text/vcard" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "imessage-crm-contacts.vcf";
  a.click();
  URL.revokeObjectURL(a.href);
}

// In-app modal. `fields` -> prompt (resolves values or null). `choices` -> a
// vertical list of buttons (resolves the picked value or null). Otherwise a
// confirm (true/false). `cancelLabel:null` makes it an alert.
function showModal({ title, message, fields, choices, confirmLabel = "OK", cancelLabel = "Cancel", danger = false }) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    let html = `<div class="modal"><div class="modal-title">${escapeHtml(title || "")}</div>`;
    if (message) html += `<div class="modal-msg">${escapeHtml(message)}</div>`;
    for (const f of (fields || [])) {
      const v = escapeHtml(f.value || ""), ph = escapeHtml(f.placeholder || "");
      html += `<label class="modal-field"><span>${escapeHtml(f.label || "")}</span>` +
        (f.type === "textarea"
          ? `<textarea data-name="${f.name}" rows="3" placeholder="${ph}">${v}</textarea>`
          : `<input data-name="${f.name}" type="text" placeholder="${ph}" value="${v}">`) +
        `</label>`;
    }
    if (choices) {
      html += `<div class="modal-choices">` + choices.map((c, i) =>
        `<button class="btn modal-choice ${c.primary ? "primary" : ""}" data-i="${i}">${escapeHtml(c.label)}</button>`).join("") + `</div>`;
    }
    html += `<div class="modal-actions">` +
      (cancelLabel ? `<button class="btn modal-cancel">${escapeHtml(cancelLabel)}</button>` : "") +
      (choices ? "" : `<button class="btn primary ${danger ? "danger" : ""} modal-ok">${escapeHtml(confirmLabel)}</button>`) +
      `</div></div>`;
    overlay.innerHTML = html;
    document.body.appendChild(overlay);

    const card = overlay.querySelector(".modal");
    const inputs = [...card.querySelectorAll("[data-name]")];
    if (inputs[0]) { inputs[0].focus(); inputs[0].select(); }
    const vals = () => Object.fromEntries(inputs.map((i) => [i.dataset.name, i.value.trim()]));
    const close = (r) => { overlay.remove(); document.removeEventListener("keydown", onKey); resolve(r); };
    const cancelVal = (fields || choices) ? null : false;
    const onCancel = () => close(cancelVal);
    const okBtn = card.querySelector(".modal-ok");
    if (okBtn) okBtn.onclick = () => close(fields ? vals() : true);
    card.querySelectorAll(".modal-choice").forEach((b) => { b.onclick = () => close(choices[+b.dataset.i].value); });
    const cancelBtn = card.querySelector(".modal-cancel");
    if (cancelBtn) cancelBtn.onclick = onCancel;
    overlay.onclick = (e) => { if (e.target === overlay) onCancel(); };
    const onKey = (e) => {
      if (e.key === "Escape") onCancel();
      else if (e.key === "Enter" && okBtn && e.target.tagName !== "TEXTAREA") { e.preventDefault(); okBtn.click(); }
    };
    document.addEventListener("keydown", onKey);
  });
}

function download(name, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name; a.click();
  URL.revokeObjectURL(a.href);
}

function wire() {
  listEl.addEventListener("click", (e) => {
    // Selection checkbox: toggle membership (with shift-click range select).
    // Handle before the row-open so clicking the box never opens the thread.
    const cb = e.target.closest("[data-sel]");
    if (cb) {
      e.stopPropagation();
      toggleSelect(cb.dataset.sel, e.shiftKey);
      return;
    }
    const sx = e.target.closest(".sg-x");
    if (sx) {
      e.stopPropagation();
      DISMISSED[sx.dataset.key] = true; persistState(); renderList();
      return;
    }
    const sg = e.target.closest(".sg-btn");
    if (sg) {
      e.stopPropagation();
      const p = PEOPLE.find((x) => x.key === sg.dataset.key);
      if (p) saveContact(p);
      return;
    }
    const btn = e.target.closest(".rowbtn");
    if (btn) {
      e.stopPropagation();
      const key = btn.dataset.key;
      const p = PEOPLE.find((x) => x.key === key);
      const act = btn.dataset.act;
      if (btn.classList.contains("unhide")) { unhide(key); renderFilters(); }
      else if (act === "savecontact" && p) { saveContact(p); return; }
      else if (act === "approve-bday" && p) approveBday(p);
      else if (act === "unapprove") unapprove(key);
      renderList();
      return;
    }
    const row = e.target.closest(".row");
    if (row) openConvo(row.dataset.id);
  });

  listEl.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(".row");
    if (!row) return;
    e.preventDefault();
    const p = PEOPLE.find((x) => x.id === row.dataset.id);
    if (p) showContextMenu(e.clientX, e.clientY, p);
  });

  $("#search").addEventListener("input", (e) => {
    query = e.target.value.trim();
    // Typing leaves any semantic-results view and resumes instant name search.
    if (searchKeys || searching) { searchKeys = null; searching = false; searchLabel = ""; }
    renderList();
  });
  $("#search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); onSearchEnter(); }
    else if (e.key === "Escape" && (searchKeys || searching)) { e.preventDefault(); clearSearch(); }
  });
  $("#sort").addEventListener("change", (e) => { sortMode = e.target.value; renderList(); });

  $("#jump-top").addEventListener("click", () => $("#messages").scrollTo({ top: 0, behavior: "smooth" }));
  $("#jump-bottom").addEventListener("click", () =>
    $("#messages").scrollTo({ top: $("#messages").scrollHeight, behavior: "smooth" }));
  $("#messages").addEventListener("scroll", updateJumpNav, { passive: true });

  // Groups toggle: orthogonal to categories. OFF = persons only everywhere;
  // ON = group conversations also appear in "All". Persisted to localStorage.
  const groupsBox = $("#show-groups");
  groupsBox.checked = showGroups;
  groupsBox.addEventListener("change", (e) => {
    showGroups = e.target.checked;
    localStorage.setItem(LS_SHOW_GROUPS, showGroups ? "1" : "0");
    // Groups only ever live in "All"; if a category is active the toggle changes
    // nothing there, but re-render counts + list to reflect the new visible set.
    renderFilters();
    renderList();
  });

  $("#filterbar").addEventListener("click", (e) => {
    const del = e.target.closest(".chip-x");
    if (del) { e.stopPropagation(); deleteFilter(del.dataset.del); return; }
    const ed = e.target.closest(".chip-edit");
    if (ed) { e.stopPropagation(); openEditFilter(ed.dataset.edit); return; }
    const rf = e.target.closest(".chip-refine");
    if (rf) { e.stopPropagation(); refineFilter(rf.dataset.refine); return; }
    const b = e.target.closest(".fchip");
    if (!b) return;
    active = active === b.dataset.f ? "all" : b.dataset.f;
    if (searchKeys || searching) clearSearch(); // leave search-results view
    clearSelection(); // selection is per-view; switching chips resets it
    clearThread();
    renderFilters();
    renderList();
  });

  // Bulk-action bar: move all selected people to a category, or clear.
  $("#bulkbar").addEventListener("click", (e) => {
    const mv = e.target.closest(".bulk-move");
    if (mv) { bulkMoveTo(mv.dataset.cat); return; }
    if (e.target.closest(".bulk-hide")) { bulkHide(); return; }
    if (e.target.closest(".bulk-clear")) { clearSelection(); renderList(); }
  });

  $("#count").addEventListener("click", (e) => {
    if (e.target.id === "exportvcf") { e.preventDefault(); exportVCard(); }
    else if (e.target.id === "refinefilter") { e.preventDefault(); refineFilter(active); }
    else if (e.target.id === "clearsearch") { e.preventDefault(); clearSearch(); }
  });

  $("#contact-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    if (btn.dataset.mode === "add") {
      const p = PEOPLE.find((x) => x.key === btn.dataset.key);
      if (p) saveContact(p);
      return;
    }
    const name = btn.dataset.name;
    const bar = $("#jobbar"); bar.hidden = false; bar.textContent = `Opening "${name}" in Contacts…`;
    try {
      const r = await fetch("/api/contacts/open", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }).then((x) => x.json());
      if (r.error) bar.textContent = "⚠️ " + r.error;
      else setTimeout(() => { bar.hidden = true; }, 1200);
    } catch (e2) { bar.textContent = "⚠️ No server — needs server.py."; }
  });

  $("#hide-btn").addEventListener("click", async () => {
    const p = PEOPLE.find((x) => x.id === selectedId);
    if (!p) return;
    if (EXCLUDED[p.key]) {
      unhide(p.key); $("#hide-btn").textContent = "Hide"; renderFilters(); renderList(); return;
    }
    const vals = await showModal({
      title: "Hide conversation",
      message: "Removes this person from your lists. Reversible — review hidden people from the ⋯ menu.",
      fields: [{ name: "reason", label: "Why? (optional)", placeholder: "marketing, don't know them…" }],
      confirmLabel: "Hide", danger: true,
    });
    if (!vals) return;
    hideConvo(p, vals.reason); $("#hide-btn").textContent = "Unhide";
    renderFilters(); renderList();
  });

  $("#newfilter").addEventListener("click", () => createFilter());
  $("#overflow-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    const nHidden = Object.keys(EXCLUDED).length;
    showMenu(r.right - 220, r.bottom + 4, [
      { label: "⟳ Sync names from Contacts", fn: syncContacts },
      { label: `🗑 Hidden (${nHidden})`, fn: showHidden },
    ]);
  });
}

function openEditFilter(id) {
  const f = filterById(id);
  if (f) createFilter({ id: f.id, name: f.name, description: f.description || "" });
}

// Reachable from the ⋯ overflow menu (not a sidebar chip): show the EXCLUDED
// people so they can be un-hidden. Reuses the list-rendering + per-row Unhide
// path; selecting any real filter chip leaves this internal view.
function showHidden() {
  active = HIDDEN_VIEW;
  clearThread();
  renderFilters();
  renderList();
}

// Pull current names from Contacts.app (picks up renames you made there).
async function syncContacts() {
  const bar = $("#jobbar"); bar.hidden = false;
  bar.textContent = "Reading Contacts… (this can take a minute)";
  let res;
  try {
    res = await fetch("/api/contacts/sync", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }).then((r) => r.json());
  } catch (e) { bar.textContent = "⚠️ No server — needs server.py."; return; }
  if (res.error) { bar.textContent = "⚠️ " + res.error; return; }
  const poll = setInterval(async () => {
    const j = await fetch(`/api/job/${res.job_id}`).then((r) => r.json());
    if (j.state === "running") {
      bar.textContent = `Syncing Contacts… ${j.message}`;
    } else if (j.state === "done") {
      clearInterval(poll);
      await reloadData();
      bar.textContent = `✓ Synced names from ${j.result.contacts.toLocaleString()} contacts`;
      setTimeout(() => { bar.hidden = true; }, 3000);
    } else { clearInterval(poll); bar.textContent = "⚠️ " + j.message; }
  }, 1500);
}

async function createFilter(prefill) {
  const editing = !!prefill;
  const fields = [
    { name: "name", label: "Filter name", placeholder: "e.g. Plumbers, College friends",
      value: editing ? prefill.name : "" },
    { name: "description", label: "Who should match? (plain English)", type: "textarea",
      placeholder: "e.g. friends I had long chats with but haven't talked to in a year; exclude family",
      value: editing ? (prefill.description || "") : "" },
  ];
  const vals = await showModal({
    title: editing ? "Edit filter" : "New filter",
    fields, confirmLabel: editing ? "Save" : "Create",
  });
  if (!vals) return;
  const name = vals.name;
  if (!name) return;
  const description = vals.description || name;

  const bar = $("#jobbar");
  bar.hidden = false;
  bar.textContent = `Creating "${name}"… starting (runs the local model over all conversations)`;
  let res;
  try {
    res = await fetch("/api/filter", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(editing ? { name, description, id: prefill.id } : { name, description }),
    }).then((r) => r.json());
  } catch (e) {
    bar.textContent = "⚠️ No server. Run  ./start.sh  (server.py) to create filters live.";
    return;
  }
  if (res.error) { bar.textContent = "⚠️ " + res.error; return; }

  const poll = setInterval(async () => {
    const j = await fetch(`/api/job/${res.job_id}`).then((r) => r.json());
    if (j.state === "running") {
      const pct = j.total ? Math.round((j.done / j.total) * 100) : 0;
      bar.textContent = `Creating "${name}"… ${j.message} ${pct}% (${j.done}/${j.total})`;
    } else if (j.state === "done") {
      clearInterval(poll);
      const r = j.result || {};
      await reloadData();
      if (r.id) active = r.id;
      renderFilters(); renderList();
      if (r.warning) {
        bar.textContent = `⚠️ ${r.warning}`;
        setTimeout(() => { bar.hidden = true; }, 6000);
      } else {
        bar.textContent = `✓ "${name}" — ${r.matches ?? "?"} matches.`;
        setTimeout(() => { bar.hidden = true; }, 2500);
      }
    } else if (j.state === "superseded") {
      clearInterval(poll); bar.hidden = true; // a newer change replaced this run
    } else {
      clearInterval(poll);
      bar.textContent = "⚠️ " + j.message;
    }
  }, 1000);
}

async function reloadData() {
  const [people, filters] = await Promise.all([
    fetch(`${DATA}people.json`).then((r) => r.json()),
    fetch(`${DATA}filters.json`).then((r) => r.json()).catch(() => FILTERS),
  ]);
  PEOPLE = people; FILTERS = filters;
  rebuildCategoryIndex(); // FILTERS changed -> refresh the key->category index
  applyContactOverrides();
  renderFilters(); renderList();
}

// Names you've saved to Contacts from here aren't in the snapshot, so re-apply
// them on every load: show the saved name and treat the person as a contact.
function applyContactOverrides() {
  for (const p of PEOPLE) {
    if (SAVED[p.key]) {
      p.name = SAVED[p.key].replace(/\s*\(merged\)$/, "");
      p.in_contacts = true; p.suggested_name = null;
    }
  }
}

async function main() {
  wire();
  await loadState();
  try { await reloadData(); }
  catch (e) { $("#count").textContent = "Could not load data — run build.py first."; }
}

main();
