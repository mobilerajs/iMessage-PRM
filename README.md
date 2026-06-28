# iMessage Personal Relationship Manager

A **local, offline** relationship manager built on top of your iMessage history. It
turns a copy of `chat.db` into a clean, categorized index of the people you actually
talk to — junk, bots, OTP codes, and marketing removed — so you can find someone fast,
see who you've lost touch with, and keep light relationship context. All intelligence
(categorization, search) runs **on-device** via a local model. Nothing is uploaded.

> **Status: baseline / first-pass.** This was built as a **homework assignment for a
> Maven class**, in a **few hours**. It works end-to-end, but it's deliberately just a
> baseline — there are **numerous enhancements, scaffolding, and hardening still to do**
> (real evals, broader test coverage, packaging/installer, live-database access, email
> enrichment, UI rework, error handling). See **[What's not done yet](#whats-not-done-yet)**
> and **[Roadmap](#roadmap-planned-enhancements)** for the honest gaps. Treat this as a
> proof-of-concept, not production software.

It's a **view, not a system of record** — a read-only lens over your messages, with
*suggestions* you act on if you want, never a profile you're obligated to curate.

---

## Requirements

- **Apple-Silicon Mac** (M1 or later) — MLX is arm64-only.
- **macOS** with **Python 3.x** and the **Xcode Command Line Tools** (`xcode-select --install`).
- **~3 GB free disk** for the on-device models.
- **Network access on the first run only** (to download the models — see below).

## Quick start

```bash
# 1. One-time: create the venv + install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. Configure: copy the example and set your name
cp config.example.json config.json   # then edit "user_name"

# 3. Start it (first run downloads models — see below)
./start.sh            # http://localhost:8001
```

On first run the app opens a **Setup screen** in the browser — no manual file
copying. Pick one:

- **Set up from this Mac** (one click) — snapshots your live Messages DB
  (read-only) and syncs Contacts, then builds. macOS will require **Full Disk
  Access**; the screen walks you through granting it and a **Try again** button.
- **Point at a folder** (no Full Disk Access) — if you'd rather not grant FDA,
  copy `chat.db` (and optionally `contacts.vcf`) into a folder like your Desktop in
  Finder, then give the app that folder. See [Setup](#setup) for how to export them.

Setting `user_name` in `config.json` anchors family detection and avoids suggesting
your own name back to you. The **Refresh** button keeps the index current afterwards.

### First-run model download

The **first** build/serve downloads the on-device models from Hugging Face: the
Qwen3-4B classifier (~2.5 GB) and the bge-small embedding model. This happens
**once**, needs **network access**, and takes a few minutes; afterwards the models
are cached locally and nothing leaves your machine. See
[`docs/SETUP-QUESTIONS.md`](docs/SETUP-QUESTIONS.md) for more.

### Setup

The **Setup screen** (above) is the easy path. **"Set up from this Mac"** needs Full
Disk Access for your terminal — a one-time grant in **System Settings → Privacy &
Security → Full Disk Access** (this is an OS requirement for reading
`~/Library/Messages/`; there's no way around it). The app reads the DB strictly
read-only and never modifies it.

If you use the **"Point at a folder"** path (no FDA), produce the two files first:

- **`chat.db`** — copy `~/Library/Messages/chat.db` in **Finder** (a user-initiated
  copy needs no FDA). **Fully quit Messages (⌘Q) before copying** so it flushes
  pending writes — the DB is opened `immutable=1`, which ignores any separate `-wal`
  file, so the copied `chat.db` must already contain your latest messages.
- **`contacts.vcf`** (optional) — Contacts.app → ⌘A → File → Export → Export vCard…
  (names + photos).

Put both in a folder (e.g. your Desktop) and point the Setup screen at it. The
database is **always opened read-only + immutable** — the app physically cannot write
to it (see [Safety](#safety--privacy)). You can also pre-place files in `./data` and
set paths via env vars (see [below](#pointing-at-the-live-database)) for a scripted setup.

---

## What it does

### Search-first table (the whole app)
One search box and a dense, sortable table; click a row to open that person in the
**native Messages app**. Built for "find someone fast." Semantic results add a **Match**
column showing the exact bit of thread that matched (click to expand), and a header line
shows library counts (people / groups / filtered). Toggles hide bare phone numbers with
no name and show/hide group chats; jump-to-top/bottom buttons appear in long results.

### Mutually-exclusive categories (a partition)
Every person lands in **exactly one** category — no duplicates across chips:

- **Family · Personal · Work · Contractors** (the built-in partition), plus any
  **user-added categories** you create (e.g. "Kids' Friends", "Medical").
- Assignment is by priority: `Contractors → Family → Work → Personal`, with user
  categories layered on top. You can **reclassify** anyone (right-click → Move to, or
  multi-select → bulk move); your corrections persist and survive rebuilds.
- **Groups** aren't a category — they're a show/hide toggle. **Junk** isn't a chip —
  hidden conversations are removed from view (reversible via ⋯ → Hidden).

### On-device classification
- **Family** — a kinship-group heuristic (members of "Mom/Dad"-anchored group chats)
  *plus* a conservative model pass for 1:1 relatives, with deterministic vetoes for
  the cases a small model gets wrong (school-parents like "Riley's Dad", your own
  number, professional-role names).
- **Work vs Personal** — the local model reads each conversation's substance, combined
  with an **`in_contacts` prior** (a saved name leans personal; a bare number leans
  transactional). Low-confidence calls default to Work and carry an **`unsure` flag**
  (a subtle `?` marker) — the precise set the email step will later resolve.
- **Contractors** — vendors/transactional contacts (folds in the model's "Service").

### User-added categories that actually re-partition
Create a category by describing it ("friends of my son", "doctors and dentists"). The
description is **routed** to the right evaluation tier (a structural rule vs. the model
reading conversations) — identity/topic descriptions correctly go semantic instead of
becoming a broad keyword match — and matching people are assigned to it **exclusively**.
These also **get sharper over time**: every time you reclassify someone by hand, the
override persists (in `userstate`) and survives rebuilds, so your corrections accumulate.

> **Opinionated defaults — and how to make them yours.** The four built-ins
> (**Family / Work / Personal / Contractors**) reflect the author's needs and have
> hand-tuned auto-classification. They aren't meant to be universal — if you fork this,
> add your own categories with the feature above (this install also runs **Kids Stuff**
> and **Medical**, created exactly that way). To change the *built-in* set at the code
> level: the partition lives in `assign_category()` (build.py); Family's kinship anchors
> are `KIN_TERMS` (build.py); the Contractors/Work seed lists are
> `data/enrich_parts/filter_contractors.json` and `filter_work.json`. (Adding categories
> through the app needs no code change — that's the recommended path.)

### Fast hybrid search (keyword + semantic)
Type a name to filter the table live; press **Enter** to search message **content**.
Content search fuses two retrievers so both exact strings and paraphrased topics are
findable:

- **Keyword (literal).** A local **SQLite FTS5** index (`out/fts.db`) over the same
  chunked conversation content gives BM25 ranking, phrase/boolean support, and a
  keyword-anchored snippet. Wrap a query in **`"quotes"`** to force an exact-phrase
  match (fast, no model needed).
- **Semantic (intent).** A local **embedding index** (bge-small) over the full chunked
  content retrieves nearest chunks in ~3 ms, aggregates them to people, and a soft
  **category prior** nudges ranking toward the query's likely category.

The two ranked lists are combined with **Reciprocal Rank Fusion**, then the local model
**confirms** the top candidates against the **matched chunk** with a relevance prompt
(intent, not literal keyword match). The result adds a **Match** column showing the exact
bit of thread that matched. ~2.5 s warm for the fused path; quoted/literal queries return
in milliseconds. (Embedding the full content — not a short digest sample — plus the
keyword index is what makes both in-passing topics and exact strings findable.)

**What you can type:**

| You type | What happens | Examples |
|----------|--------------|----------|
| A **name** | Live-filters the table as you type (name prefix) | `sarah`, `dr` |
| A **topic**, then **Enter** | Searches message *content* (keyword + semantic fused) | `lunch spots in palo alto` · `people I talked about pizza with` · `who recommended a plumber` · `ski trip` |
| An **exact phrase** in `"quotes"`, then Enter | Literal match, instant (no model) | `"happy birthday"` · `"the wifi password is"` · an address or order number |

Plus the **Recent** (default) and **Lost touch** views — the latter surfaces people
you've gone quiet with (and is group-activity-aware, so someone you still chat with in
a group thread isn't wrongly flagged).

### Relationship tooling
- **Name inference → Contacts** — suggests full names for bare numbers and writes them
  back to Contacts.app (additive merge; never destructive).
- **Birthdays** — inferred from conversation and shown/sortable.
- **Multi-select** — checkboxes (reveal on hover) → bulk **move** to a category or
  **hide**.
- **Sort** — Newest / Oldest (your "who have I gone quiet with" view) / Most / Least
  contacted / Name (A–Z).
- **Open in Messages / Contacts**, jump-to-top/bottom in long threads.

### Refresh (snapshot + sync, on demand)
A **Refresh** button snapshots the live Messages DB (SQLite backup API — never touches
the original), re-syncs names from Contacts.app, and rebuilds the index — all in one
click. A header line shows **"Last synced on …"**. Before running, a modal **estimates
how long it will take** (from the chat.db size and your contact count) and asks you to
confirm; while it runs, a **blocking overlay** prevents interaction until it finishes.
Rebuilds are **incremental** — only conversations whose message count or last date
changed are re-embedded, so a no-op refresh is ~2 s instead of re-embedding everything.

---

## Architecture

```
chat.db (read-only) ─┐
contacts.vcf ────────┼─► build.py ──► out/people.json        (the index)
                     │               out/messages/<id>.json  (lazy-loaded threads)
data/enrich_parts/ ──┘               out/embeddings.npy      (semantic index)
                                     out/fts.db              (keyword/FTS5 index)
                                     out/filters.json        (categories)

appconfig.py  settings resolver: env var > config.json > built-in default
classify.py   local Qwen3-4B (MLX): category/work/family judgments, filter routing,
              semantic confirm, name inference
embeddings.py local bge-small (MLX): build the semantic index + cosine retrieval
keyword_search.py  SQLite FTS5 keyword search + reciprocal-rank fusion (no model)
server.py     Flask: serves the app, keeps the model warm, exposes the API
              (/api/search, /api/filter, /api/contacts/*, /api/userstate,
               /api/refresh, /api/refresh/estimate, …); static routes are
               allowlisted so data files and source are never served
index.html ·  vanilla-JS search-first table UI — the single app, served at /
app.js ·
styles.css
```

- **Data flow:** `build.py` is the pipeline (parse → junk-filter → classify → write
  JSON). The frontend reads JSON; the browser never loads the multi-hundred-MB
  database. User corrections (category overrides, hides) live in `data/userstate.json`
  and are layered at render time, so a rebuild never clobbers them.
- **Message text** lives in the `attributedBody` column (an Apple "typedstream" blob),
  decoded by `typedstream_text.py` (verified 100% on a 30k-row sample).
- **Local model:** Qwen3-4B-Instruct-2507-4bit via MLX — benchmarked to beat the 8B on
  both accuracy and speed for this task. Embeddings: bge-small (also MLX, no torch).

See `docs/plans/` for the design docs (incl. the 2026-06-13 pivot to exclusive
categories) and `docs/spikes/` for the de-risking spikes (model tool-calling, MCP
servers).

---

## Safety & privacy

- **iMessage DB is read-only by construction** — opened `mode=ro&immutable=1`; there is
  no code path that can modify or delete it. A timestamped backup is made on connect.
- **Contacts writes are additive-only** — create/merge names + birthdays on explicit
  action; never delete a contact, remove a field, or overwrite existing data.
- **Everything is local/offline.** `data/` and `out/` hold your messages, contacts, and
  photos — both are gitignored; `config.json` is gitignored (copy from the example).
- The only writes anywhere: Contacts.app (on your explicit Save) and the app's own
  `data/`/`out/`.

---

## What's not done yet

This is a first pass; these are known, deliberate gaps:

- **Evals are early.** Classification quality was checked by hand-labeled spot-checks
  (e.g. the work/personal prompt was tuned against ~130 labels, ~68% precision / ~75%
  recall). There's now a **search eval harness** in `tests/eval/` (recall@k /
  precision@k / MRR over a per-query-type gold set, with a baseline regression check),
  but the gold set is local/gitignored (it references real conversation keys) — only a
  synthetic, PII-free fixture is committed. Category accuracy still has no regression eval.
- **Light test coverage.** 100+ unit tests cover the *pure* logic (read-only DB open,
  category partition/priority, family veto, search re-rank, chunking/aggregation,
  filter routing, keyword/FTS + RRF fusion, incremental embedding reuse, group-aware
  recency, UTC timestamps, phone-key normalization, eval metrics). The integration
  surfaces — Flask endpoints, AppleScript/Contacts, the model passes, the frontend —
  are **not** covered by automated tests and were verified manually.
- **Uses a copy of `chat.db`, not the live database** for the build. The **Refresh**
  button snapshots the live DB on demand (read-only backup API), but there's no
  background poller and no installer — refresh is a manual click.
- **No email enrichment.** Work-vs-Personal is iMessage-only today; the `unsure` flag
  marks exactly the people the planned Gmail/Outlook step would resolve.
- **UI is still settling.** The single search-first table works end-to-end but is being
  actively shaped, not a final, polished product.
- **No background auto-update.** Refresh (snapshot → sync → incremental rebuild) is the
  manual seam a scheduled poller would hook into.

---

## Roadmap (planned enhancements)

- **Search** — better relevance/ranking; surface the `category_hint`; tune the
  recall/precision balance.
- **Categorization & filtering** — improve precision, especially the Work/Personal
  boundary and family recall; better handling of the `unsure` set.
- **UI** — rethink the layout (the chat-on-the-right may become a denser card grid or a
  cleaner dossier); polish.
- **Native install** — package as an installer that points at the **live** iMessage
  database directly (with Full Disk Access), instead of a hand-copied file, and make
  the context flow seamless.
- **Email/MCP enrichment** — connect a **Gmail or Outlook MCP** so the model can use
  email context to decide who's genuinely *work* vs *personal* — resolving the `unsure`
  contacts with a signal iMessage alone can't provide.
- **Evals + tests** — a real eval harness for classification/search and coverage for the
  integration surfaces.

---

## Using live data (and a note on paths)

The way to pull in your live Messages is the **Setup screen** / **Refresh** button:
they read `~/Library/Messages/chat.db` strictly **read-only** (SQLite backup API) and
write a *working copy* to `data/chat.db` — your real Messages database is never
modified. That needs **Full Disk Access** for your terminal (System Settings → Privacy
& Security).

> ⚠️ **Do not point `CHAT_DB` at `~/Library/Messages/chat.db`.** `CHAT_DB` is the
> *working copy* — the build reads it, and snapshot/refresh **write** to it. Setting it
> to the live database would mean writing to your real Messages DB, so the app now
> **refuses** any `CHAT_DB` under `~/Library/Messages`. Use it only to relocate the
> working copy elsewhere.

Any setting can be overridden by an env var (which beats `config.json`, which beats the
default):

| Env var | Default | Meaning |
|---------|---------|---------|
| `CHAT_DB` | `data/chat.db` | **Working copy** of the Messages DB (build reads it; snapshot writes it). Must not be the live DB. |
| `CONTACTS_VCF` | `data/contacts.vcf` | Exported vCard |
| `EXCLUDE_FILE` | `data/exclude.json` | Persistent junk-filter feedback |

`CRM_SKIP_EMBED=1` skips rebuilding the search index for faster iteration.

---

## Project layout

| Path | Purpose |
|------|---------|
| `build.py` | Pipeline: chat.db + vCard → `out/` (people, threads, embeddings, categories) |
| `classify.py` | Local-model passes: category/work/family, filter routing, semantic confirm, names |
| `embeddings.py` | Local embedding index + cosine retrieval (semantic search) |
| `keyword_search.py` | SQLite FTS5 keyword search + reciprocal-rank fusion (no model) |
| `appconfig.py` | Settings resolver: env var > config.json > default |
| `server.py` | Flask: UI + warm model + API |
| `imessage_db.py` | Read-only/immutable chat.db open + backup |
| `typedstream_text.py` | Decodes message text from `attributedBody` |
| `index.html` · `app.js` · `styles.css` | The local web view |
| `tests/` | Unit tests (pure logic) — `pytest -q` |
| `tests/eval/` | Search eval harness (recall@k/precision@k/MRR; gold set gitignored) |
| `docs/plans/` · `docs/spikes/` | Design docs + de-risking spikes |
| `data/` · `out/` | Your source data + generated data (both gitignored) |

Run the tests: `.venv/bin/python -m pytest -q`
