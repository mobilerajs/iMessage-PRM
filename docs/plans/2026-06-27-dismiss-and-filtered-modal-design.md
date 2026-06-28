# Dismiss a Person + Filtered-Out Modal — Design

**Date:** 2026-06-27
**Status:** Approved (brainstorming → ready for implementation plan)

## Goal

Two related, frontend-only features:
1. **Dismiss a person** you don't care about — a reversible, instant hide.
2. Make the **"N filtered out"** header count **explain itself** — click it to open a
   modal breaking down *why* conversations aren't shown, by bucket, including your own
   dismissals (with Restore).

They share one mental model: "things not shown, and why" — junk/spam/alerts the build
filtered, plus people you chose to dismiss.

## What already exists (explored first)

- The current search-first UI has **no** hide/dismiss action (that was in the dropped
  Classic UI). `userstate` has empty `excluded`/`dismissed` buckets and a working
  `/api/userstate` POST (full-object round-trip), but nothing reads/writes them.
- The filtered breakdown is **already computed and loaded**: `state.stats.filtered` =
  `{total: 1090, junk: 859, feedback: 122, empty: 23, opt_out_spam: 44, alerts: 42}`.
- `out/filtered_out.json` holds a *sample* of actually-filtered conversations with
  reasons (not all 1090) — not needed for v1.

So **no backend work** is required: counts come from `stats.filtered`, dismissals
persist through the existing userstate endpoint.

## Feature 1 — Dismiss a person (reversible hide)

**Affordance:** a small **×** revealed on hover at the right edge of each person row
(same reveal pattern as the bulk checkboxes). Plus a **"Dismiss"** action added to the
existing multi-select **bulk bar** for clearing several at once. (Right-click is skipped
— low discoverability, awkward on touch.)

**Behavior:** clicking × adds the person's `key` to `userstate.dismissed` (a `{key:
true}` map), persists via the existing `/api/userstate` POST (full-object round-trip so
`excluded`/`approved`/`saved`/`catOverride` are never clobbered — same pattern as
`catOverride`), and the client **immediately** removes them from the table. No rebuild.
Dismissed people are hidden in **all** views and search results.

**Restore:** from the modal's "Dismissed by you" bucket — removes the key from
`userstate.dismissed`, persists, re-renders. Fully reversible.

**Persistence:** lives in `userstate` (on disk), so it survives reloads and is layered
at render time, so a rebuild never clobbers it — identical to how `catOverride` works.

## Feature 2 — Filtered-out modal

**Trigger:** the "N filtered out" portion of the header counts becomes clickable
(cursor/hover affordance, `role="button"`, keyboard-operable). Clicking opens a modal.

**Content:** one row per bucket — a one-line *why* + the count, from
`state.stats.filtered`:
- **Junk / spam** — `junk`
- **Marketing & opt-out** — `opt_out_spam`
- **Alerts & one-time codes** — `alerts`
- **Empty threads** — `empty`
- **Filter feedback** — `feedback`
- **Dismissed by you (N)** — from `userstate.dismissed`; lists each person's name with a
  **Restore** button.

The auto buckets are **explain-only** in v1 (counts + description, not individually
restorable — rescuing those is the build-time `exclude.json` path, out of scope). Only
*your* dismissals are restorable here.

## Architecture (frontend only)

- **app.js**
  - `loadSharedState()` reads `userstate.dismissed` into `state.dismissed` (a Set);
    `USERSTATE.dismissed` stays the source of truth for round-trip saves.
  - `currentRows()` filters out keys in `state.dismissed`.
  - `dismissPerson(key)` / `restorePerson(key)` mutate `USERSTATE.dismissed` + the Set,
    `persistUserstate()` (reuse the existing full-object POST helper), re-render.
  - `rowHtml()` adds the hover-× button (`data-dismiss=<key>`); the row click handler
    routes the × to `dismissPerson` (and must NOT also open Messages).
  - Bulk bar gains a `.bulk-dismiss` button → dismiss all selected.
  - `openFilteredModal()` builds the modal from `state.stats.filtered` + the dismissed
    list (resolve keys → names via `state.all`, which still contains dismissed people).
  - The filtered-count element gets a click/keydown handler.
- **index.html** — the filtered count becomes a focusable control; modal built
  dynamically (like the existing popover/refresh modal) or a hidden container.
- **styles.css** — hover-× styling; reuse the existing modal/overlay styles from the
  refresh modal; a clickable look for the count.

No new endpoints; `/api/userstate` already validates (`validate_userstate`) and backs up.

## Error handling

- Userstate save failure → keep the optimistic UI change but surface a small status
  (mirror existing patterns); on reload the on-disk state wins.
- Missing `stats.filtered` (older build) → modal shows the dismissed bucket only, with a
  note that build-time stats are unavailable.
- Dismissing the currently-open popover's row → close the popover first.

## Testing

- Frontend-heavy, no JS test runner → **`node --check` + a manual checklist**
  (dismiss hides instantly; reload keeps it hidden; modal counts match
  `stats.filtered`; Restore brings them back; bulk dismiss; dismissed don't appear in
  search).
- Optional backend guard: a server test that `/api/userstate` round-trips a `dismissed`
  key without dropping other buckets (largely covered by existing userstate validation).

## Out of scope (v1)

- Restoring/rescuing auto-filtered (junk/spam/alerts) conversations from the modal
  (that's the build-time `exclude.json` path).
- Right-click context menu.
- A permanent (build-time) exclude — this is a reversible render-time hide by design.
