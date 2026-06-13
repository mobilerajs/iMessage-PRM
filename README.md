# iMessage Personal Relationship Manager

A **local, offline** relationship manager built on top of your iMessage history. It
turns a copy of `chat.db` into a clean, categorized index of the people you actually
talk to — junk, bots, OTP codes, and marketing removed — so you can find someone fast,
see who you've lost touch with, and keep light relationship context. All intelligence
(categorization, search) runs **on-device** via a local model. Nothing is uploaded.

> **Status: first-pass / work-in-progress.** Built as a time-boxed project. The core
> works end-to-end, but the UI is still emerging and several things are deliberately
> unfinished — see **[What's not done yet](#whats-not-done-yet)**. Read that section;
> it's honest about the gaps (no formal evals, light test coverage, no installer, uses
> a *copy* of the database, no email enrichment yet).

It's a **view, not a system of record** — a read-only lens over your messages, with
*suggestions* you act on if you want, never a profile you're obligated to curate.

---

## Quick start

```bash
# 1. Put two files in ./data (see Setup below):
#    data/chat.db        a copy of ~/Library/Messages/chat.db
#    data/contacts.vcf   Contacts.app → Export vCard

# 2. One-time: create the venv + install deps
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 3. Build the dataset + serve (model loads warm)
./start.sh            # http://localhost:8001
./start.sh --rebuild  # force a fresh rebuild first
```

`cp config.example.json config.json` and set your name (used to anchor family
detection and to avoid suggesting your own name back to you).

### Setup (the two source files)

- **`data/chat.db`** — a copy of `~/Library/Messages/chat.db`. Quit Messages first so
  it isn't mid-write; copy `chat.db-wal`/`chat.db-shm` too for the most recent texts.
- **`data/contacts.vcf`** — Contacts.app → ⌘A → File → Export → Export vCard… (names + photos).

The database is **always opened read-only + immutable** — the app physically cannot
write to it (see [Safety](#safety--privacy)). Today we point at a *copy* for testing;
pointing at the live database is one env var away (see below) and is a roadmap item.

---

## What it does

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

### Fast hybrid semantic search
Type a phrase ("people I ate pizza with", "restaurants in the bay area") → the local
**embedding index** (bge-small) retrieves the nearest conversations in ~3 ms, a soft
**category prior** nudges ranking toward the query's likely category, and the model
**confirms** the top candidates with a recall-favoring *relevance* prompt (intent, not
literal keyword match). ~2.5 s warm, vs ~2 min for the naive full-model scan it replaced.

### Relationship tooling
- **Name inference → Contacts** — suggests full names for bare numbers and writes them
  back to Contacts.app (additive merge; never destructive).
- **Birthdays** — inferred from conversation and shown/sortable.
- **Multi-select** — checkboxes (reveal on hover) → bulk **move** to a category or
  **hide**.
- **Sort** — Newest / Oldest (your "who have I gone quiet with" view) / Most / Least
  contacted / Name (A–Z).
- **Open in Messages / Contacts**, jump-to-top/bottom in long threads.

---

## Architecture

```
chat.db (read-only) ─┐
contacts.vcf ────────┼─► build.py ──► out/people.json        (the index)
                     │               out/messages/<id>.json  (lazy-loaded threads)
data/enrich_parts/ ──┘               out/embeddings.npy      (search index)
                                     out/filters.json        (categories)

classify.py   local Qwen3-4B (MLX): category/work/family judgments, filter routing,
              semantic confirm, name inference
embeddings.py local bge-small (MLX): build the search index + cosine retrieval
server.py     Flask: serves the UI, keeps the model warm, exposes the API
              (/api/search, /api/filter, /api/contacts/*, /api/userstate, …)
app.js        vanilla-JS frontend: exclusive-category rendering, multi-select,
              search, the read-only thread pane
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

- **No formal evals.** Classification quality was checked by hand-labeled spot-checks
  (e.g. the work/personal prompt was tuned against ~130 labels, ~68% precision / ~75%
  recall), not a maintained eval harness. There's no regression eval for search
  relevance or category accuracy.
- **Light test coverage.** 53 unit tests cover the *pure* logic (read-only DB open,
  category partition/priority, family veto, search re-rank, filter routing). The
  integration surfaces — Flask endpoints, AppleScript/Contacts, the model passes, the
  frontend — are **not** covered by automated tests and were verified manually.
- **Uses a copy of `chat.db`, not the live database.** Live access works via env var +
  Full Disk Access (below) but isn't the default flow, and there's no installer.
- **No email enrichment.** Work-vs-Personal is iMessage-only today; the `unsure` flag
  marks exactly the people the planned Gmail/Outlook step would resolve.
- **UI is in flux.** The right-hand chat pane, category presentation, and search UX are
  all still being shaped.
- **No packaging/auto-update.** Re-running `build.py` is the manual seam a scheduled
  refresh would hook into.

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

## Pointing at the live database

Source paths default to `./data` but aren't hardcoded — override via env vars:

```bash
CHAT_DB=~/Library/Messages/chat.db \
CONTACTS_VCF=~/Downloads/contacts.vcf \
./start.sh --rebuild
```

| Env var | Default | Meaning |
|---------|---------|---------|
| `CHAT_DB` | `data/chat.db` | Source Messages database (always opened read-only) |
| `CONTACTS_VCF` | `data/contacts.vcf` | Exported vCard |
| `EXCLUDE_FILE` | `data/exclude.json` | Persistent junk-filter feedback |

Reading the live DB needs **Full Disk Access** for your terminal (System Settings →
Privacy & Security). `CRM_SKIP_EMBED=1` skips rebuilding the search index for faster
iteration.

---

## Project layout

| Path | Purpose |
|------|---------|
| `build.py` | Pipeline: chat.db + vCard → `out/` (people, threads, embeddings, categories) |
| `classify.py` | Local-model passes: category/work/family, filter routing, semantic confirm, names |
| `embeddings.py` | Local embedding index + cosine retrieval (hybrid search) |
| `server.py` | Flask: UI + warm model + API |
| `imessage_db.py` | Read-only/immutable chat.db open + backup |
| `typedstream_text.py` | Decodes message text from `attributedBody` |
| `index.html` · `app.js` · `styles.css` | The local web view |
| `tests/` | Unit tests (pure logic) — `pytest -q` |
| `docs/plans/` · `docs/spikes/` | Design docs + de-risking spikes |
| `data/` · `out/` | Your source data + generated data (both gitignored) |

Run the tests: `.venv/bin/python -m pytest -q`
