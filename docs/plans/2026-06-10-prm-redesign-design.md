# Design: iMessage-Oriented PRM — Connections, Categories & Live View

**Date:** 2026-06-10
**Status:** Approved (brainstorming → ready for implementation plan)

## Organizing principle

**This app is a *view* (a read-only lens), not a *system of record*.**

The user does not want to own and maintain profiles. A "dossier" creates curation
guilt — fields that nag to be correct. Instead, the app is a categorized index of
the people you actually talk to, with read-only context to help you place them. It
pivots around iMessage; everything else only enhances that view.

Every decision below follows from this principle: read-only everywhere, suggestions
not requirements, no editable profile, safe by construction.

## What this is (and is not)

- **iMessage-oriented CRM.** People come *only* from iMessage. Email never adds a
  person — it only enriches someone already present in iMessage. Contacts.app
  supplies real names/photos and receives explicit saves. Onboarding states this
  boundary verbatim.
- **A local AI agent.** Installed by the user; serves a local web page backed by a
  local Qwen model (Qwen3-4B-Instruct-2507-4bit via MLX). Fully offline — data,
  tokens, and model never leave the machine.
- **Not** a messaging client (cannot send), not an email client, not a contact
  manager you curate. It is a lens.

## Core UX model

- **Left = a categorized index of people** — the thing you scan to find someone.
  Rich rows with facts (name, category chips, recency, birthday, message count) and
  *dismissible suggestions* (Possible name → Save; possible birthday). Suggestions
  are acted on if you want and ignored otherwise — never fields that nag.
- **Right = read-only context** — `Messages | Email` segmented toggle.
  - **Messages** is the default (best signal for "who *is* this number").
  - **Email** is a second tab, **lazy-loaded only on click**, showing the person's
    email headers/snippets read-only. The click *is* the quota guardrail — email is
    never fetched until you choose to look, then only a top-N sample.
- **No dossier, no editable table.** A table implies cells-to-complete (more
  curation pressure); the messages/email panes are clearly *their* content, carrying
  zero obligation.

### Retained features (unchanged)

- Suggested full names → write/merge into Contacts.app on explicit Save.
- Birthday extraction (show + sort by upcoming).
- Sort by Newest / Oldest / Most / Least contacted — central to "have I lost touch?"
- Open in Messages (`imessage://`), Open in Contacts.app.
- Per-filter recourse (move/remove/refine), in-app modals.

## Categories = filters (overlapping), seeded at onboarding

- Categories *are* the existing filter engine, presented as categories.
  **Overlapping**: a person can be in many (Work **and** Catch-up). No new
  mutually-exclusive "one type per person" concept.
- Onboarding **seeds the common PRM defaults** as ordinary, redefinable filters:
  **Family, Personal, Work, Service, Junk** (Junk hidden as today). Plus the
  cross-cutting **Catch-up** (gone quiet) which the user relies on.
- User can add / remove / rename / redefine any of them. Every filter is a
  user-authored semantic/keyword/computed query — "the description IS the filter."
- **"Reclassify" = the existing move/remove**, reframed as recategorize.
- **Initial bulk classification** runs the local model over everyone to drop them
  into the seeded categories on first build.

## Connections (the new layer)

A first-run onboarding wizard (re-openable later as "Connections" from ⋯). Three
sources, each with a distinct role:

| Source | Role | Signal |
|---|---|---|
| **iMessage** | Read (the spine) | Who you talk to, recency, volume, content |
| **Email (Gmail + Outlook, 1..n)** | Read (enrich only) | Work-vs-personal, which inbox knows them |
| **Contacts.app** | Read **+ write** | Real names, photos; receives saves/merges |

Onboarding steps: connect iMessage → add one or more email accounts (Gmail/Outlook)
→ connect Contacts → build (parse iMessage, enrich, sync Contacts, bulk-classify).

### Email via MCP, Qwen as orchestrator

- The app embeds its **own MCP client** and runs **standalone** Gmail/Outlook MCP
  servers (open-source, local OAuth). The user just clicks "Auth"; the MCP server
  handles its own OAuth. The app does **not** reuse Claude.ai-hosted connectors —
  those are bound to the agent's session, not the app.
- **Qwen is the orchestrator**: the server exposes the connected MCP tools to Qwen;
  Qwen decides *which* MCP to call and *when*, via a bounded agent loop
  (model emits tool call → server executes through a capped tool layer → result fed
  back → model continues). Same loop powers bulk classification and ad-hoc questions.
- A 4B model orchestrating is fragile, so loops are **short and bounded** (1–2 tool
  calls/task), the toolset is tight and well-described, tool-call output is validated
  against schemas with retry, and the fallback is "classify from iMessage alone."

## Guardrails (hard rules)

**MCP usage / quotas — email is a surgical specialist, never a bulk reader:**

1. **Targeted queries only** — scoped search by a person's email/name/phone; request
   **top-N** recent matches (N≈5–10), never thread history or inbox pages.
2. **Sample then stop** — classify from the head sample; early-exit once confident.
3. **Spend only where it changes the answer** — most people are classified by
   iMessage alone; email is queried only for the **ambiguous middle** (Work/Personal
   /Catch-up boundary) — tens of lookups, not the full population.
4. **Cache the verdict, not the emails** — store a tiny
   `work_signal: {value, confidence, sampled_at, basis}` per person in `data/`;
   never store email contents. Re-runs reuse it; refresh on demand.
5. **Hard per-run budget** — e.g. ≤200 MCP calls/build; on cap, stop and **log what
   was skipped** (no silent truncation). *Qwen orchestrates what; the harness
   enforces how much* — the executor caps results and refuses calls past budget
   regardless of what the model wants.
6. **Ad-hoc questions are entity-scoped** — answering "is X work?" touches only X,
   top-N.

**Read-only / non-destructive everywhere:**

- **iMessage**: opened `mode=ro&immutable=1` — physically cannot write/delete. Plus a
  timestamped backup to `data/backups/` on first connect.
- **Email**: strictly read-only — only *search/read* MCP scopes. No send, modify,
  archive, or delete code path exists.
- **Contacts**: additive and non-destructive only — create or merge-*add* names/
  birthdays on explicit user action. **Never** delete a contact, remove a field, or
  overwrite existing data. No destructive AppleScript verbs.
- The only writes in the whole system: (1) Contacts.app on explicit Save, (2) the
  app's own `data/` folder.

## Live auto-update

- **iMessage is live** (local + free): a light poller checks `chat.db` for new
  messages since the last-seen rowid, incrementally ingests the delta, and re-runs
  cheap classification only on people who changed.
- **Email is not live** (quota'd): refreshed on-demand (clicking the Email tab) or on
  a slow schedule. Never polled per-message.

## Open / deferred (YAGNI for v1)

- Shipping to *other* users requires registered Gmail/Outlook apps and Google's
  `Mail.Read`-class security review. For the user's own use this is a click-through
  on an unverified app — designed so own-use works immediately; ship-to-others is a
  later verification step, not a rebuild.
- Interactive "ask anything" agent box (Qwen with full tool access) is a v2 nicety,
  not the classification spine.
- Other identity sources (LinkedIn/WhatsApp/Twitter DMs) are walled off — out of
  scope.

---

## Revision 2026-06-13 — Categories become a mutually-exclusive partition

**Supersedes the overlapping-filters model above.** Seeing it on real data, the user
found overlap confusing ("I scan Work, then the same person shows up in Personal").
Categories are now a **partition: one person, exactly one category.**

- **Set: Family · Personal · Work · Contractors.** Service folds into Contractors.
  Extensible — adding a category **re-partitions** (moves matching people out of their
  current category into the new one; never duplicates).
- **Single assignment by priority (highest wins): `Contractors → Family → Work →
  Personal`.** Each person gets the highest-priority category they qualify for and
  appears under that chip only. Personal is the default catch-all.
  - **Contractors** = old `Service` (vendors/transactional) ∪ the curated 128-key
    contractor list.
  - **Family** = existing kinship-anchor heuristic.
  - **Work** = the 76-person curated list (recovered from the original Catch-up
    `include_keys`) ∪ people whose conversation the local model reads as
    **work-related / transactional** (content tone signal). 
  - **Personal** = everyone else.
- **Uncertainty: best-stab, flag — don't bucket.** No "Not sure" chip (it'd be a
  chore). Low-confidence cases **default to Work** (the majority class, minimizing
  wrong assignments) and carry a quiet **`unsure` flag** (subtle row marker, not its
  own chip). The user can see which Work entries are guesses.
- **The `unsure` flag is the Phase 3 email trigger.** Email lookups run *only* on
  unsure-flagged people (the "ambiguous middle" guardrail), so the uncertainty bucket
  **self-heals** as email comes online — each unsure person gets confirmed Work or
  moved to Personal. This replaces "query the ambiguous middle" with a concrete signal.
- **Catch-up is no longer a chip.** "Who do I catch up with" = open a category, sort
  **Oldest**. Both Personal and Work are recency-sortable. (Reverts the
  2026-06-13 catch-up-as-cadence change.)
- **Groups is no longer a category** — it's a **"Show groups" toggle** (default
  hidden), orthogonal to categories. Junk/spam stays removed upstream.

**What this changes in code:** `build.py` computes a single `category` per person via
the priority partition (incl. a model Work-vs-Personal content judgment + `unsure`
flag); `app.js` renders exclusive chips from that single field, drops the Catch-up and
Groups chips, adds a Groups toggle + an unsure row marker, and removes the overlapping
"in: X" membership tags. The overlapping `include_keys`/multi-filter membership UI is
retired for the core categories (user-added custom filters may still overlap as a
separate, secondary concept — TBD).

### Considered: card-grid layout (deferred, isolated swap)

The user floated replacing the list + right-hand chat pane with a **dense card grid**
(~8 cards/row) to scan many people at once — strong fit for the "find someone / who
have I lost touch with" job, especially with exclusive categories + recency sort (pick
Work → wall of cards sorted Oldest). **Deferred, not rejected.** Rationale: the
partition is a data + sidebar change that is *layout-agnostic* — it's needed whether
the body is a list or a card grid. So build the partition first; the card grid then
becomes an isolated swap of only the list renderer, easy to A/B against the current
list+chat. Keep chat-on-right for now (user: "we can leave it as is").
