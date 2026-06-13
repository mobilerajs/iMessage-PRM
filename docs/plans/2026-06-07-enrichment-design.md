# Enrichment: categories, names, birthdays (2026-06-07)

Adds "intelligent" layers on top of the CRM. Inference is done **locally**:
mechanical signals by Python heuristics, judgment calls by the agent (Claude in
this session) writing a cached `enrichment.json`. No external API, no per-call
cost, no message text sent off-machine. Future unattended re-runs would need an
API or local model; for now the agent does it.

## Components

1. **Birthdays (heuristic, `build.py`).** In a 1:1, a message where *you* wrote
   "happy birthday" pins *their* birthday to that month/day. Mode across years →
   confidence. Surfaced as an "Upcoming birthdays" sort/filter.

2. **First names (heuristic, `build.py`).** Address patterns ("Hi John") and
   sign-offs ("—John", "this is John") seed a first-name guess for non-contacts.

3. **Full names + categories (agent → `enrichment.json`).** `build.py` emits a
   compact `out/digests.json` (per convo: counts + a few sample messages +
   heuristic guesses, NOT full transcripts). The agent reads digests, derives a
   **dynamic taxonomy** from the data, tags each conversation, and proposes full
   names with a reason/confidence. `build.py` merges this into `people.json`.

4. **Contacts write-back (review → vCard).** A Suggestions view lists proposed
   names; approved ones export a `.vcf` the user imports. Nothing auto-written.

## Data flow

```
build.py  ──► out/digests.json           (for the agent)
          ──► out/people.json            (+ heuristic birthday / first-name)
agent     ──► data/enrichment.json       (category, full_name, confidence, reason)
build.py  ──► merges enrichment into people.json
UI        ──► category chips, birthday sort, suggestions → export vCard
```

`enrichment.json` keyed by stable conversation `key`; cached and gitignored.

## Rollout

Pilot ~30 conversations first → validate taxonomy + sample inferences with the
user → then the full run. If inline analysis of ~1,000 is too heavy, offer a
workflow fan-out.

## people.json additions

`category`, `category_reason`, `suggested_name`, `name_confidence`,
`name_reason`, `birthday` `{month, day, confidence, source}`.
