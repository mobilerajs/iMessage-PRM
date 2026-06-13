# iMessage-Oriented PRM Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the existing iMessage CRM into a read-only, live-updating relationship *lens* — iMessage-populated, email-enhanced via Qwen-orchestrated MCP calls, with seeded category-filters and a Messages|Email right pane.

**Architecture:** Local Flask server + vanilla-JS frontend + local Qwen (MLX). iMessage is the read-only spine (people come only from here). Email (Gmail/Outlook) is enrichment only, fetched on demand through standalone MCP servers that the app drives via an embedded MCP client; Qwen orchestrates *which* calls to make, a capped tool layer enforces *how much*. Contacts.app is read + additive-write. Everything offline; the app is a view, never a system of record.

**Tech Stack:** Python 3 (Flask, sqlite3, mlx-lm), vanilla JS/HTML/CSS, MCP (Python SDK client + standalone Gmail/Outlook servers), AppleScript (Contacts), pytest (new).

**Source of truth for intent:** `docs/plans/2026-06-10-prm-redesign-design.md`. Read it before starting.

**Working agreements:**
- This codebase has **no tests today**. Phase 1 adds pytest. Use TDD where logic is unit-testable (DB open mode, backup, classification routing, budget caps, delta detection). For macOS-permission / OAuth / AppleScript / UI work, automated unit tests are not meaningful — those tasks specify **manual verification steps** instead. Don't fake tests for integration surfaces.
- **Safety is non-negotiable and load-bearing** (see design "Guardrails"). The iMessage DB is opened read-only/immutable; Contacts writes are additive-only; email scopes are read-only. Several tasks exist solely to prove these.
- During development, **never write to the real Contacts.app or send any email**. Contacts-write tests run against a throwaay test path or are manually triggered by the user only.
- Commit after every green task. Use `@superpowers:executing-plans`.

---

## Phase 0 — De-risk the unknowns (spikes)

These two unknowns gate the email/agent work. Do them FIRST; their outcomes finalize the code in Phases 3–4. Each spike ends in a short written finding committed under `docs/spikes/`.

### Task 0.1: Spike — Qwen3-4B tool-calling in mlx-lm

**Files:**
- Create: `spikes/qwen_toolcall_spike.py`
- Create: `docs/spikes/2026-06-10-qwen-toolcalling.md` (findings)

**Step 1:** Write a minimal script that loads `mlx-community/Qwen3-4B-Instruct-2507-4bit`, gives it ONE tool (`search_email(query, top_n)` that returns a hardcoded list of 3 fake email dicts), and prompts: "Decide if 'Alex Rivera' is a work contact. Use the tool if helpful." Parse the model's tool-call output (Qwen's `<tool_call>` JSON or function-call format), execute the fake tool, feed the result back, get a final verdict.

**Step 2:** Run it 10 times. Record: does it emit valid tool-call JSON? How often (reliability %)? Does it correctly stop after 1 call? Does the final verdict parse?

**Step 3:** Write findings to `docs/spikes/2026-06-10-qwen-toolcalling.md`:
- Exact tool-call format the model emits + the parsing approach that works.
- Reliability % over 10 runs; failure modes.
- **Decision:** is Qwen reliable enough to orchestrate (loops ≤2 calls), or do we fall back to *server-orchestrates / Qwen-judges-only*? (Design allows the fallback.)
- The minimal agent-loop pseudocode that worked.

**Step 4: Commit** `git add spikes/qwen_toolcall_spike.py docs/spikes/2026-06-10-qwen-toolcalling.md && git commit -m "spike: qwen tool-calling reliability in mlx-lm"`

> **Gate:** Phase 3's agent loop is written to match this finding. If reliability is poor, Phase 3 implements the server-orchestrated fallback (server decides when to fetch email for ambiguous people; Qwen only judges work-vs-personal from the sample). Either way the *external behavior and guardrails are identical*.

### Task 0.2: Spike — standalone Gmail & Outlook MCP servers + read-only OAuth

**Files:**
- Create: `docs/spikes/2026-06-10-email-mcp-servers.md` (findings)
- Create: `spikes/mcp_client_spike.py`

**Step 1:** Research and shortlist standalone, self-hostable MCP servers for Gmail and Outlook/Graph that (a) do their own local OAuth, (b) support **read-only** scopes (`gmail.readonly`, `Mail.Read`), (c) expose a *search* tool. Capture candidate repos, license, scopes, transport (stdio/SSE), and maturity.

**Step 2:** Stand up ONE (Gmail first — simpler consent), connect to it from `spikes/mcp_client_spike.py` using the Python MCP SDK client, complete OAuth with a **read-only scope only**, and issue one capped search (top_n=5) for a known sender. Confirm results come back and that no write tools are exposed/used.

**Step 3:** Write findings: chosen servers, exact OAuth flow (device/browser), token storage location/format, the search tool's signature + response shape, and how to constrain to read-only scope. Note Outlook equivalents.

**Step 4: Commit** `git add spikes/mcp_client_spike.py docs/spikes/2026-06-10-email-mcp-servers.md && git commit -m "spike: standalone gmail/outlook MCP servers with read-only oauth"`

> **Gate:** Phase 3's connection layer + tool layer are written against the concrete tool signatures and OAuth flow found here.

---

## Phase 1 — Test harness + iMessage read-only safety & backup

Foundational and fully knowable today. No external deps.

### Task 1.1: Add pytest

**Files:**
- Create: `requirements-dev.txt` (`pytest`)
- Create: `tests/__init__.py`, `tests/conftest.py`
- Create: `pytest.ini`

**Step 1:** `requirements-dev.txt` = `pytest>=8`. `pytest.ini`:
```ini
[pytest]
testpaths = tests
python_files = test_*.py
```
**Step 2:** `conftest.py` — fixture that builds a tiny throwaway SQLite file resembling `chat.db` (a `message` table with `ROWID`, `date`, `text`, `handle_id`; a `handle` table) in `tmp_path`, so DB-layer tests never touch real data:
```python
import sqlite3, pytest

@pytest.fixture
def fake_chat_db(tmp_path):
    p = tmp_path / "chat.db"
    con = sqlite3.connect(p)
    con.executescript("""
        CREATE TABLE handle(ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message(ROWID INTEGER PRIMARY KEY, text TEXT,
            date INTEGER, handle_id INTEGER, is_from_me INTEGER DEFAULT 0);
        INSERT INTO handle(ROWID,id) VALUES (1,'+15551234567');
        INSERT INTO message(ROWID,text,date,handle_id) VALUES (1,'hi',700000000000000000,1);
    """)
    con.commit(); con.close()
    return p
```
**Step 3:** Run `python -m pytest -q`. Expected: `no tests ran` (0 tests), exit clean.
**Step 4: Commit** `git add requirements-dev.txt pytest.ini tests && git commit -m "test: add pytest harness with fake chat.db fixture"`

### Task 1.2: Read-only/immutable iMessage open (TDD)

**Files:**
- Create: `imessage_db.py` (new module — centralizes ALL chat.db access)
- Test: `tests/test_imessage_db.py`

**Step 1: Failing test** — opening returns a working read connection, and writes raise:
```python
import sqlite3, pytest
from imessage_db import open_readonly

def test_open_readonly_can_read(fake_chat_db):
    con = open_readonly(fake_chat_db)
    assert con.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 1

def test_open_readonly_blocks_writes(fake_chat_db):
    con = open_readonly(fake_chat_db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM message")
```
**Step 2:** Run `python -m pytest tests/test_imessage_db.py -v` → FAIL (no module).
**Step 3: Implement**:
```python
import sqlite3
from pathlib import Path

def open_readonly(path):
    """Open chat.db strictly read-only + immutable. Physically cannot write."""
    uri = f"file:{Path(path).resolve()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)
```
**Step 4:** Run tests → PASS.
**Step 5: Commit** `git add imessage_db.py tests/test_imessage_db.py && git commit -m "feat: read-only immutable chat.db open"`

### Task 1.3: Timestamped backup on connect (TDD)

**Files:**
- Modify: `imessage_db.py`
- Test: `tests/test_imessage_db.py`

**Step 1: Failing test:**
```python
from imessage_db import backup_db

def test_backup_creates_timestamped_copy(fake_chat_db, tmp_path):
    dest_dir = tmp_path / "backups"
    out = backup_db(fake_chat_db, dest_dir, stamp="20260610-120000")
    assert out.exists() and out.name == "chat-20260610-120000.db"
    assert out.read_bytes() == fake_chat_db.read_bytes()
```
**Step 2:** Run → FAIL.
**Step 3: Implement** `backup_db(src, dest_dir, stamp)` using `shutil.copy2`, `dest_dir.mkdir(parents=True, exist_ok=True)`. `stamp` is injected (no `Date.now()` in tests). Caller passes real timestamp.
**Step 4:** Run → PASS.
**Step 5: Commit** `git commit -am "feat: timestamped chat.db backup on connect"`

### Task 1.4: Route build.py + server.py through imessage_db (refactor)

**Files:**
- Modify: `build.py` (every `sqlite3.connect(chat_db...)` → `imessage_db.open_readonly`)
- Modify: `server.py` (any direct chat.db open → `open_readonly`)

**Step 1:** Grep for chat.db opens: `grep -n "connect(" build.py server.py`. Replace each chat.db open with `open_readonly`. Leave `data/`-writing SQLite/JSON untouched.
**Step 2:** Run a build against the real copy to confirm parity: `python build.py` (existing flow). Expected: completes, `out/people.json` regenerates with same row count as before (compare `jq length` before/after).
**Step 3: Manual verify:** confirm no code path opens chat.db writable anymore (`grep -n "chat" build.py server.py` review).
**Step 4: Commit** `git commit -am "refactor: all chat.db access goes through read-only imessage_db"`

---

## Phase 2 — Categories = seeded filters + bulk classification reframe

Mostly reframing existing filter machinery. Uses the model but no external network.

### Task 2.1: Seed the 5 category-filters + Catch-up as defaults (TDD)

**Files:**
- Modify: `build.py` (`DEFAULT_FILTERS` / `sync_filters`)
- Test: `tests/test_default_filters.py`

**Step 1: Failing test** — defaults include exactly the seeded categories with stable ids:
```python
from build import DEFAULT_FILTERS
def test_seeded_categories_present():
    ids = {f["id"] for f in DEFAULT_FILTERS}
    assert {"family","personal","work","service","junk","catchup"} <= ids
```
**Step 2:** Run → FAIL (current defaults differ).
**Step 3:** Update `DEFAULT_FILTERS` to the seeded set with the design's descriptions (Family/Personal/Work/Service/Junk + Catch-up). Junk keeps `hidden` semantics. Each is an ordinary redefinable filter.
**Step 4:** Run → PASS. Then `python build.py` and confirm `out/filters.json` contains them.
**Step 5: Commit** `git commit -am "feat: seed common PRM categories as default filters"`

### Task 2.1b: Suppress seeded duplicates of renamed user filters (TDD)

> **Decision (2026-06-10):** User keeps their refined `catch-up` (70+ include_keys); the seeded `catchup` must be suppressed when an equivalent user filter exists.

**Files:** Modify: `build.py` (`sync_filters` backfill); Test: `tests/test_default_filters.py`.

**Step 1: Failing test** — backfill skips a seeded default whose *normalized slug* (lowercased, hyphens/spaces/underscores stripped) matches an existing filter's id/slug, so an existing `catch-up` suppresses seeded `catchup`:
```python
def test_renamed_filter_suppresses_seeded_duplicate():
    existing = [{"id":"catch-up","name":"Catch-up","type":"semantic"}]
    out = backfill_defaults(existing)  # extract the backfill into a pure fn
    ids = [f["id"] for f in out]
    assert "catchup" not in ids and "catch-up" in ids
```
**Step 2:** Run → FAIL. **Step 3:** Extract the backfill loop into a pure `backfill_defaults(existing)` and match on normalized slug, not exact id. **Step 4:** Run → PASS + full suite green. **Step 5: Commit** `git commit -am "feat: suppress seeded category duplicates of renamed user filters"`

### Task 2.2: Present seeded filters as categories + unify Junk with Hidden

> **Decision (2026-06-10):** Junk = the hidden set. Selecting **Junk** shows everyone the user hid (EXCLUDED/isHidden) ∪ anyone the model flags spam/automated (category Junk). The **Hide** action files someone under Junk; reversible (un-junk = un-hide). The existing `hidden` pseudo-filter becomes the `junk` category.

**Files:** Modify: `app.js` (`renderFilters`, `passesFilter`, Hide button + context menu, `hidden` pseudo-filter), `index.html`/`styles.css` as needed.

**Step 1 (presentation):** In `renderFilters`, render seeded filters under a "Categories" affordance, user filters as before — purely presentational, overlapping preserved.
**Step 2 (junk unification):** Make the `junk` filter's membership = `EXCLUDED` ∪ people with category `Junk`. Replace the hardcoded `hidden` pseudo-filter with the seeded `junk` chip. Reframe **Hide** as "Mark as Junk" filing into the same EXCLUDED set. Selecting Junk lists them with an **un-junk** affordance. Junk stays excluded from every other category's counts/lists.
**Step 3: Manual verify** in browser: seeded ones read as categories; add/rename/delete works; a person appears in multiple; Hiding files under Junk; opening Junk shows hidden + spam; un-junk restores.
**Step 4: Commit** `git commit -am "feat: categories presentation + unify Junk with hidden set"`

### Task 2.3: Initial bulk classification pass

**Files:** Modify: `build.py` (classification), `classify.py` if needed.

**Step 1:** Ensure the build runs the local model to assign every person into the seeded categories (reusing existing semantic-filter evaluation). iMessage-only signals here — email comes in Phase 3.
**Step 2: Manual verify:** after `python build.py`, spot-check 10 people land in sensible categories; Junk hidden.
**Step 3: Commit** `git commit -am "feat: bulk-classify people into seeded categories on build"`

---

## Phase 3 — Email enrichment via MCP (Qwen-orchestrated, capped)

**Depends on Phase 0 findings.** Finalize signatures/flows against `docs/spikes/*`.

### Task 3.1: Capped tool layer (TDD — the guardrail core)

**Files:**
- Create: `mcp_tools.py` (the capped executor wrapping MCP calls)
- Test: `tests/test_mcp_tools.py`

**Step 1: Failing tests** for the budget/sampling caps (pure logic, mock the MCP call):
```python
from mcp_tools import CappedToolLayer, BudgetExceeded
import pytest

def fake_search(query, top_n): return [{"from":"x@co.com"}]*50  # server returns many

def test_top_n_is_clamped():
    layer = CappedToolLayer(search_fn=fake_search, max_top_n=5, run_budget=100)
    out = layer.search_email("alex", top_n=999)
    assert len(out) == 5            # clamped regardless of model request

def test_run_budget_enforced():
    layer = CappedToolLayer(search_fn=fake_search, max_top_n=5, run_budget=2)
    layer.search_email("a"); layer.search_email("b")
    with pytest.raises(BudgetExceeded):
        layer.search_email("c")     # 3rd call past budget refused

def test_skips_are_logged():
    layer = CappedToolLayer(search_fn=fake_search, max_top_n=5, run_budget=1)
    layer.search_email("a")
    try: layer.search_email("b")
    except BudgetExceeded: pass
    assert layer.skipped == ["b"]   # no silent truncation
```
**Step 2:** Run → FAIL.
**Step 3: Implement** `CappedToolLayer`: clamps `top_n` to `max_top_n`, decrements `run_budget` per call, raises `BudgetExceeded` and appends to `self.skipped` when exhausted. This enforces "Qwen orchestrates *what*, harness enforces *how much*."
**Step 4:** Run → PASS.
**Step 5: Commit** `git commit -am "feat: capped MCP tool layer with top-n clamp + run budget"`

### Task 3.2: Embedded MCP client connection (against spike findings)

**Files:** Create: `mcp_client.py`; Modify: `config.json` (connections config).

**Step 1:** Implement a thin wrapper over the Python MCP SDK that launches/connects the chosen Gmail/Outlook server (from Task 0.2), exposing only its **read** search tool to `CappedToolLayer`. Read-only scope only.
**Step 2: Manual verify:** with a test account authed, fetch top-5 for one known sender; confirm read-only (no write tools registered).
**Step 3: Commit** `git commit -am "feat: embedded MCP client for read-only email search"`

### Task 3.3: Qwen-orchestrated agent loop

> **RESOLVED by Task 0.1 (2026-06-10): Qwen-orchestration, not the fallback.** 10/10 valid tool calls, correct stop, correct verdict. See `docs/spikes/2026-06-10-qwen-toolcalling.md`.
> **Load-bearing parser requirement:** mlx-lm's stop token truncates the closing `</tool_call>` tag. The parser MUST anchor on the opening `<tool_call>` tag and brace-balance-scan the JSON object after it; never require the closing tag. Build the loop on `tokenizer.apply_chat_template(..., tools=TOOLS)` (native tool + `<tool_response>` round-trip).

**Files:** Create: `agent_loop.py`; Modify: `classify.py` (add the work-vs-personal judgment using email sample).

**Step 1:** Implement the bounded Qwen loop (≤2 tool calls) using `apply_chat_template(..., tools=TOOLS)` and the opening-tag/brace-balance parser above. On malformed output after retry, fall back to iMessage-only classification for that person (don't crash the build).
**Step 2:** Only query email for the **ambiguous middle** (design guardrail #3) — gate on model uncertainty / Work-Personal boundary.
**Step 3: Manual verify** on the ~5 boundary contacts validated earlier (Alex=work, Jordan=work, Priya=personal): correct verdicts, ≤ a handful of calls total.
**Step 4: Commit** `git commit -am "feat: email-enhanced work/personal classification (capped)"`

### Task 3.4: Cache the verdict, not the emails (TDD)

**Files:** Modify: `classify.py`/`build.py`; Test: `tests/test_work_signal_cache.py`.

**Step 1: Failing test:** writing a verdict stores `{value,confidence,sampled_at,basis}` and re-run reuses it without re-calling search.
**Step 2:** Run → FAIL.
**Step 3: Implement** a `data/work_signal.json` cache keyed by person; never store email bodies. Re-runs read cache; a `refresh` clears one entry.
**Step 4:** Run → PASS.
**Step 5: Commit** `git commit -am "feat: cache work-signal verdicts, never email contents"`

---

## Phase 4 — Right pane Messages|Email toggle + live update + connections UI

### Task 4.1: Right-pane Messages|Email segmented toggle (email lazy-loaded)

**Files:** Modify: `index.html` (segmented control in `#thread`), `app.js` (tab logic + lazy fetch), `styles.css`.

**Step 1:** Add `Messages | Email` segment to the thread header. Messages = default (existing render). Email tab is empty until clicked.
**Step 2:** On first Email-tab click for a person, call a new `GET /api/email/<key>?top_n=5` (server → `CappedToolLayer`), render headers/snippets **read-only**. The click is the quota trigger — no prefetch.
**Step 3: Manual verify:** Messages shows by default; clicking Email fetches once, caches in-memory for the session, read-only, no edit affordances.
**Step 4: Commit** `git commit -am "feat: messages|email read-only right-pane toggle, email lazy-loaded"`

### Task 4.2: `/api/email/<key>` endpoint

**Files:** Modify: `server.py`.

**Step 1:** Add endpoint that resolves the person, calls the capped tool layer (top_n bounded, budget-aware), returns sanitized read-only fields (from, subject, date, snippet). On budget exceeded → 200 with `{"skipped": true}` (no silent failure).
**Step 2: Manual verify** via curl with a test account.
**Step 3: Commit** `git commit -am "feat: capped read-only email endpoint"`

### Task 4.3: Live iMessage delta detection (TDD)

**Files:** Create: `live.py`; Test: `tests/test_live.py`.

**Step 1: Failing test** using `fake_chat_db`: `new_message_rowids(con, since_rowid)` returns only rows with `ROWID > since_rowid`; inserting a new row then returns it.
**Step 2:** Run → FAIL.
**Step 3: Implement** `new_message_rowids` (read-only query) + a `last_seen_rowid` persisted in `data/`.
**Step 4:** Run → PASS.
**Step 5: Commit** `git commit -am "feat: incremental iMessage delta detection"`

### Task 4.4: Live poller wiring (incremental re-classify of changed people)

**Files:** Modify: `server.py` (background poller), `app.js` (refresh changed rows).

**Step 1:** Background thread polls `chat.db` (read-only) every N seconds; on delta, ingest just new messages and re-run cheap classification only for affected people; push to UI (poll endpoint or SSE). Email is NOT polled (quota) — on-demand only.
**Step 2: Manual verify:** send yourself a message; the person's recency/row updates within a poll cycle without a full rebuild.
**Step 3: Commit** `git commit -am "feat: live iMessage auto-update for changed people"`

### Task 4.5: Connections / onboarding wizard

**Files:** Create: `onboarding.*` (or a `#connections` modal in existing UI); Modify: `index.html`, `app.js`, `server.py`.

**Step 1:** First-run wizard (re-openable from ⋯ → "Connections"):
1. Connect iMessage — detect Full Disk Access; on success backup + read-only open; else "point me at a copy" fallback.
2. Add email accounts (Gmail/Outlook, 1..n) — each triggers its MCP server's OAuth (read-only scope); show as removable chips.
3. Connect Contacts — trigger Automation permission.
4. Build — run pipeline.
**Step 2:** Copy states verbatim: *"This is an iMessage-oriented CRM. People come only from iMessage; email only enriches them."*
**Step 3: Manual verify** full first-run on a clean profile.
**Step 4: Commit** `git commit -am "feat: connections onboarding wizard"`

### Task 4.6: Harden Contacts writes as additive-only (TDD where possible)

**Files:** Modify: `server.py` (Contacts AppleScript paths); Test: `tests/test_contacts_safety.py`.

**Step 1: Failing test:** assert the generated AppleScript for save/merge contains no destructive verbs (`delete`, `remove`) — string-level guard on the script builder.
**Step 2:** Run → FAIL (extract script building into a testable pure function first if needed).
**Step 3:** Refactor Contacts script generation into a pure builder; assert additive-only; only fills empty fields / adds, never overwrites or deletes.
**Step 4:** Run → PASS. **Do NOT execute against real Contacts in tests** — string assertions only.
**Step 5: Commit** `git commit -am "feat: enforce additive-only, non-destructive Contacts writes"`

---

## Final verification checklist (manual, end-to-end)

- [ ] chat.db cannot be written by any code path (grep + read-only proof test green).
- [ ] A timestamped backup exists in `data/backups/` after connect.
- [ ] Email is fetched ONLY on Email-tab click / explicit question; per-run budget honored; skips logged.
- [ ] No email contents persisted to disk — only `work_signal` verdicts.
- [ ] Contacts writes are additive-only; no delete/overwrite path exists.
- [ ] Seeded categories appear; a person can be in several; add/redefine works.
- [ ] Live update reflects a new iMessage within one poll cycle without full rebuild.
- [ ] Onboarding states the iMessage-only-population boundary verbatim.

---

## Notes on scope & sequencing

- **Phase 0 gates Phase 3.** Do not write OAuth/agent code before the spikes resolve the MCP server choice and the model's tool-calling reliability.
- Ship-to-other-users (registered Gmail/Outlook apps + Google security review) is **out of scope** — own-use unverified-app flow only.
- Interactive "ask anything" Qwen agent is **v2**, not here.
