# First-Run Onboarding (Live-DB Bootstrap) — Design

**Date:** 2026-06-27
**Status:** Approved (brainstorming → ready for implementation plan)

## Goal

Remove the manual "copy `chat.db` + export `contacts.vcf`" step so a new user can
clone the repo and set the app up against their own data from inside the app, with
no terminal file-wrangling. When there's no index yet, the app shows a **Setup
screen** that bootstraps everything.

## Scope

In scope: a first-run onboarding screen with two setup paths (live read, and
point-at-a-folder), reusing the existing snapshot/Contacts/build plumbing.

Explicitly **out of scope** (considered and dropped during brainstorming):
- Relationship cadence / overdue nudges (#2) — inferring cadence reliably is hard;
  a wrong "overdue" is worse than none. "Lost touch" stays as-is.
- Inline thread preview on hover (#3) — click-to-open already covers it; hover risks
  flashing message content on casual mouse-over.
- Search confidence / per-result match badges (#6/#7) — per-row badges crowd the UI;
  an always-on confidence indicator wasn't pulling its weight. (The keyword-vs-
  semantic "modes" question (#7) is moot: search already fuses both via RRF, with
  quoted = literal — no mode to choose.)
- Saved searches (#4), eval gold set (#5), email/MCP enrichment (#8).

## Background: why Full Disk Access is unavoidable for the live path

macOS protects the *contents* of `~/Library/Messages/` via TCC. Any process that
reads `chat.db` (our terminal-launched Python/Flask process) must have **Full Disk
Access** granted to its host terminal in System Settings → Privacy & Security → Full
Disk Access. This is a one-time manual grant; there is **no programmatic bypass**,
and "pick the folder" does not help for the protected location (the security-scoped
bookmark trick only works for sandboxed apps with entitlements — we are neither).

The only way to avoid FDA is to copy `chat.db` to an unprotected location yourself
(a Finder GUI copy is user-initiated and allowed) and point the app at that copy —
which is the "point at a folder" fallback below.

## User experience

When `out/people.json` is missing, the app serves a **Setup screen** (not an empty
table). It states plainly that all data stays on this Mac, then offers:

1. **"Set up from this Mac"** (one click)
   - Snapshots the live `~/Library/Messages/chat.db` (read-only SQLite backup),
     syncs Contacts via AppleScript, then builds — with live progress.
   - If macOS blocks the read (no FDA), the screen detects it and shows a
     **"Grant Full Disk Access"** step with the exact System Settings path and a
     **Try again** button. (Contacts sync may separately trigger the normal
     Automation consent prompt — that's expected, not FDA.)

2. **"Point at a folder"** (no FDA)
   - The user Finder-copied `chat.db` (and optionally `contacts.vcf`) into an
     unprotected folder (Desktop/Documents) and gives the path.
   - We validate the folder contains `chat.db`, copy it into `data/`, and build.

After either path completes, the page reloads into the normal app. From then on the
existing **Refresh** button keeps the index current (it already snapshots live +
syncs Contacts).

## Architecture

**Server (`server.py`)** — new endpoints, all reusing existing helpers:
- `GET /api/setup/status` → `{ needs_setup: bool, fda_ok: bool, chat_db_present: bool }`.
  `needs_setup` = `out/people.json` absent. `fda_ok` = a cheap read-only probe of the
  live DB path succeeds.
- `POST /api/setup/from-mac` → background job (mirrors `/api/refresh`): `snapshot_live_db()`
  → `sync_contacts_live()` → build. On a permission error, the job result carries
  `fda_needed: true` so the UI can show the grant step.
- `POST /api/setup/from-folder` → body `{ folder }`. Validate the folder exists and
  contains `chat.db` (reject otherwise with a clear error; sanitize/expanduser the
  path, no traversal surprises). Copy `chat.db` (and `contacts.vcf` if present) into
  `data/`, then background-build.
- Both jobs surface progress + errors via the existing `job_set`/`job_get` +
  `pollJob` machinery (and stderr surfacing from the background-rebuild work already
  shipped).

**Frontend (`index.html` / `app.js`)**:
- On load, call `/api/setup/status` (or treat an `out/people.json` 404 as the signal).
  If `needs_setup`, render the Setup screen in place of the table.
- "Set up from this Mac" → POST `from-mac`, poll the job; on `fda_needed` show the
  grant instructions + Try again; on done reload.
- "Point at a folder" → text input → POST `from-folder`, poll, reload on done.
- The Setup screen reuses the existing blocking-overlay/progress styles.

**Reused, already-built:** `snapshot_live_db()`, `sync_contacts_live()`, the build
pipeline, the background-job pattern (`job_set`/`job_get`), `pollJob`, the
blocking-overlay UI.

## Data flow

```
no out/people.json
   └─► Setup screen
         ├─ "from this Mac"  ─► snapshot live chat.db (ro) ─► sync Contacts ─► build ─► reload
         │      └─ blocked? ─► "Grant Full Disk Access" + Try again
         └─ "point at folder" ─► validate folder/chat.db ─► copy into data/ ─► build ─► reload
```

## Error handling

- **FDA blocked:** detected via the read-only probe / job error → guided grant step +
  Try again. Never a stack trace.
- **Folder missing `chat.db`:** clear inline error, no build attempted.
- **Build failure:** surfaced via the job's `error` (stderr), as with the existing
  background rebuilds.
- **Server must run with no index:** the Setup screen and `/api/setup/*` endpoints
  must work before `out/` exists (don't assume `people.json`/`digests.json` on boot).

## Security & privacy

- Live DB is only ever read **read-only/immutable** (snapshot via backup API) — no
  write path, consistent with the rest of the app.
- The folder path is `expanduser`'d and validated; we only read `chat.db`/`contacts.vcf`
  from it and copy into `data/` (gitignored).
- FDA is the user's explicit, revocable grant. The Setup screen states data stays
  local. No new network calls (models still download once on first build, as today).

## Testing

- **Pure/unit:** the DB-open-error classifier (`is this fda_needed vs a real error?`)
  and the folder-validation function (exists + contains `chat.db` + safe path) are
  pure → unit-tested. `needs_setup` logic tested.
- **Manual:** the live read (with and without FDA), the Contacts Automation prompt,
  the folder path, and the reload-into-app transition — verified by hand (no browser
  test harness).

## Follow-ups (not now)

- Deferred from the prior audit: full list virtualization (Task 6.1 of the audit plan).
- The static-fallback in `start.sh` (`python3 -m http.server`) serves a broken UI and
  bypasses the allowlist — worth removing or fixing separately; out of scope here.
