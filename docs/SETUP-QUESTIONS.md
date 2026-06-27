# Setup questions (for an installable version)

These are the things the app needs to know about *this* user/machine that can't
be reliably guessed. Today they live in `config.json` + the `data/` folder; an
installer/onboarding flow would ask them once.

## Identity
- **Your name** (`config.json: user_name`) — used to (a) exclude *you* from name
  inference (so "Hi Sam" doesn't make the contact "Sam"), (b) anchor family by
  surname. Currently defaults to the first card in the exported vCard.

## Data sources
- **Messages database** (`config.json: chat_db`) — a copy of
  `~/Library/Messages/chat.db`, or the live path (needs Full Disk Access).
- **Contacts** (`config.json: contacts_vcf`) — exported vCard, for names + photos.

## Model
- **Local model** (`config.json: model`) — default Qwen3-4B-Instruct-2507-4bit
  (benchmarked best for this task). Downloaded on first run via MLX.

## Permissions (macOS, prompted on first use)
- **Full Disk Access** — to read the live Messages DB (not needed for a copy).
- **Automation → Contacts** — only if using direct write-back to Contacts.app.

## Where state lives (all on disk, under `data/`)

- **Filters** → `data/filters.json` (+ semantic match-sets in `data/enrich_parts/filter_*.json`)
- **Hidden / approved-names / saved-to-contacts** → `data/userstate.json` (via the
  server; localStorage is only a fallback in static mode)
- **Junk feedback** → `data/exclude.json`
- **Categories / names / birthdays** → `data/enrich_parts/`

All of it survives rebuilds, restarts, and code changes. `build.py` merges, it
never wipes user state. (Generated, throwaway: everything in `out/`.)

## Open questions to resolve before packaging
- **Family detection** — currently inferred from group chats with kinship-named
  anchors (Mom/Dad). Should onboarding let the user confirm/seed family members?
- **Write-back default** — direct Contacts write vs. vCard export. Which is the
  safe default to ship?
- **Filter precision** — custom filters need good descriptions; should we ship a
  starter set (Contractors, Doctors, …) with tuned prompts?
- **Re-run / auto-update** — when new messages arrive, what retriggers build +
  classify? (cron, on-launch, manual)
- **Bundling the model** — ship weights with the installer, or download on first
  run? (~2.5 GB)
