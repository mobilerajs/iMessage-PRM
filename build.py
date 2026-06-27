#!/usr/bin/env python3
"""
Build the iMessage CRM dataset from a copy of chat.db + an exported vCard.

Reads (all under ./data):
    data/chat.db          a copy of ~/Library/Messages/chat.db
    data/contacts.vcf     Contacts.app -> File -> Export -> Export vCard...

Writes (under ./out):
    out/people.json           the sidebar: one entry per kept person / group
    out/messages/<id>.json    the full conversation for each entry (lazy-loaded)
    out/photos/<id>.jpg       contact photos, where the vCard had one

"Kept" means it passed the junk filter: a person is shown only if you've had a
two-way exchange (you sent AND received at least once) OR they're in your
Contacts. All group chats are kept. Shortcodes / OTP / marketing / inbound-only
strangers are dropped.

Re-run any time the source data changes — this is the seam a future auto-update
would hook into. It only reads the database; it never writes to it.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import platform
import re
import sqlite3
import sys
from collections import defaultdict

import appconfig
import imessage_db

from typedstream_text import decode as decode_body

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "out")

# Source files default to ./data (a copy), but can point anywhere — including
# the live database — via env vars, so nothing here is hardcoded to the copy:
#
#   CHAT_DB=~/Library/Messages/chat.db CONTACTS_VCF=~/contacts.vcf ./start.sh --rebuild
#
# Reading the live ~/Library/Messages/chat.db requires Full Disk Access for the
# terminal. The database is opened strictly read-only either way.
DB_PATH = os.path.expanduser(appconfig.resolve("chat_db", "CHAT_DB", os.path.join(DATA, "chat.db")))
VCF_PATH = os.path.expanduser(appconfig.resolve("contacts_vcf", "CONTACTS_VCF", os.path.join(DATA, "contacts.vcf")))
EXCLUDE_PATH = os.path.expanduser(os.environ.get("EXCLUDE_FILE", os.path.join(DATA, "exclude.json")))
ENRICH_PATH = os.path.expanduser(os.environ.get("ENRICH_FILE", os.path.join(DATA, "enrichment.json")))

APPLE_EPOCH = 978307200  # seconds between 1970-01-01 and 2001-01-01


# --------------------------------------------------------------------------- #
# Phone / email normalization
# --------------------------------------------------------------------------- #
def norm_key(raw: str) -> str:
    """Canonical key for matching a handle id to a contact.

    Emails -> lowercased as-is. Phone numbers -> last 10 digits (drops country
    code and all formatting) so "+1 (555) 123-4567" and "5551234567" match.
    """
    if not raw:
        return ""
    if "@" in raw:
        return raw.strip().lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 10:
        return digits[-10:]
    return digits  # short codes etc. stay as-is


def is_shortcode(raw: str) -> bool:
    """True for SMS short codes / non-personal numeric senders (e.g. 262966)."""
    if "@" in raw:
        return False
    digits = re.sub(r"\D", "", raw)
    return 0 < len(digits) <= 6


# --------------------------------------------------------------------------- #
# User feedback / exclusions
# --------------------------------------------------------------------------- #
def load_exclusions(path: str) -> dict:
    """Load data/exclude.json — the persistent 'mark as junk' feedback.

    Shape (all keys optional):
        {
          "keys":            ["p5551234567", "g42"],   # hide these exact convos
          "name_contains":   ["amazon", "doordash"],   # hide by name substring
          "snippet_contains":["unsubscribe", "opted out", "verification code"]
        }
    Per-convo `keys` come from the "Hide" button; the *_contains patterns are how
    a few hidden examples get generalized into dropping a whole type.
    """
    base = {"keys": set(), "name_contains": [], "snippet_contains": [], "keep": set()}
    if not os.path.exists(path):
        return base
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception as exc:
        print(f"  ! could not read {path}: {exc}")
        return base
    base["keys"] = set(cfg.get("keys", []))
    base["name_contains"] = [s.lower() for s in cfg.get("name_contains", [])]
    base["snippet_contains"] = [s.lower() for s in cfg.get("snippet_contains", [])]
    # Force-keep list: rescues any false positive from the auto spam/alert rules.
    base["keep"] = set(cfg.get("keep", []))
    return base


def is_excluded(entry_key: str, name: str, snippet: str, ex: dict) -> bool:
    if entry_key in ex["keys"]:
        return True
    nl = (name or "").lower()
    if any(p in nl for p in ex["name_contains"]):
        return True
    sl = (snippet or "").lower()
    if any(p in sl for p in ex["snippet_contains"]):
        return True
    return False


# --------------------------------------------------------------------------- #
# Automated spam / alert detection
#
# These two rules target the cases real humans don't produce, while protecting
# real conversations. Edit the word/phrase lists to taste; the `keep` list in
# exclude.json rescues any false positive.
# --------------------------------------------------------------------------- #

# Standalone outbound replies that mean "this is an automated/marketing line."
# Kept deliberately tiny and exact — "no", "cancel", "stop by" are NOT here
# because real people say them.
OPT_OUT_WORDS = {"stop", "stop all", "stopall", "unsubscribe", "unsub", "stop2quit"}

# High-signal phrases automated senders use that humans almost never type.
ALERT_RE = re.compile(
    r"(do not reply|donotreply|reply stop|txt stop|text stop|reply 'stop'|"
    r"network msg: you replied|"
    r"verification code|your code is|one-time (code|passcode)|security code|"
    r"now boarding|your flight|flight \w+\d+|gate \w?\d|boarding (begins|time)|"
    r"appointment (reminder|is|with|on)|your appointment|due for a visit|"
    r"reply (c|y|yes|1) to confirm|to confirm your|to reschedule|"
    r"out for delivery|your (package|order|shipment)|has shipped|tracking (number|#)|"
    r"prescription|ready for pickup|refill|copay|"
    r"free msg|payment (is )?due|past due|autopay|balance of \$|amount due|"
    r"to opt out|msg&data rates|message and data rates|std msg rates)",
    re.I,
)

# An alert feed is heavily one-directional: it texts you a lot, you barely reply.
# Real humans are balanced. Require both a low reply count and real inbound volume
# so a balanced conversation that merely mentions a keyword is never auto-dropped.
ALERT_MAX_SENT = 2
ALERT_MIN_RECV = 8


def replied_optout(msgs) -> bool:
    """True if any outbound message is, by itself, an opt-out word."""
    for m in msgs:
        if m["me"]:
            t = (m["text"] or "").strip().lower().rstrip(".!").strip()
            if t in OPT_OUT_WORDS:
                return True
    return False


def looks_like_alert(msgs, in_contacts: bool, sent: int, recv: int) -> bool:
    """True for automated alert/notification feeds from unknown senders.

    Gated to non-contacts that are heavily inbound-skewed (you replied <= 2 times
    out of >= 8 received) AND match a specific automated phrase — so real
    back-and-forth, even with people not in Contacts, always survives.
    """
    if in_contacts or sent > ALERT_MAX_SENT or recv < ALERT_MIN_RECV:
        return False
    inbound = " ".join(m["text"] for m in msgs if not m["me"] and m["text"])
    return bool(ALERT_RE.search(inbound))


# --------------------------------------------------------------------------- #
# Local inference: birthdays + first-name guesses (no model needed)
# --------------------------------------------------------------------------- #
BDAY_RE = re.compile(r"happy\s*(?:belated\s*)?b(?:irth)?day|happy\s*bday|\bhbd\b", re.I)


def infer_birthday(msgs):
    """Their birthday ~ the date(s) on which YOU said 'happy birthday' to them.

    Only meaningful 1:1 (caller passes a person's messages). Returns
    {month, day, confidence, years} or None. Clusters by (month, day); a date
    confirmed in >= 2 different years is high confidence.
    """
    from collections import Counter

    hits = []  # (month, day, year)
    for m in msgs:
        if m["me"] and m["text"] and BDAY_RE.search(m["text"]):
            d = m["date"]
            if len(d) >= 10:
                try:
                    y, mo, da = int(d[0:4]), int(d[5:7]), int(d[8:10])
                    hits.append((mo, da, y))
                except ValueError:
                    pass
    if not hits:
        return None
    md_counts = Counter((mo, da) for mo, da, _ in hits)
    (mo, da), _ = md_counts.most_common(1)[0]
    years = sorted({y for m2, d2, y in hits if (m2, d2) == (mo, da)})
    conf = "high" if len(years) >= 2 else "medium"
    return {"month": mo, "day": da, "confidence": conf, "years": years}


_NAME_STOP = {
    "there", "all", "you", "guys", "everyone", "team", "man", "bro", "dude",
    "sir", "maam", "again", "back", "hi", "hey", "ok", "thanks", "yes", "no",
    "good", "happy", "yeah", "sure", "love", "the", "this", "that", "and",
}
_ADDR_RE = re.compile(r"\b(?:hi|hey|hello|thanks|thank you|dear)[\s,]+([A-Z][a-z]+)\b")
_SIGN_RE = re.compile(
    r"(?:this is|it'?s|i'?m|my name is|-|–|—)\s*([A-Z][a-z]+)(?:\s+([A-Z][a-z]+))?\s*$",
    re.M,
)


def infer_first_name(msgs):
    """Best-effort first name for a non-contact from how they're addressed /
    how they sign off. Returns a string or None (seed for full-name inference)."""
    from collections import Counter

    cand = Counter()
    for m in msgs:
        t = m["text"] or ""
        if m["me"]:
            for name in _ADDR_RE.findall(t):
                if name.lower() not in _NAME_STOP:
                    cand[name] += 2  # how you address them is a strong signal
        else:
            for first, _last in _SIGN_RE.findall(t):
                if first.lower() not in _NAME_STOP:
                    cand[first] += 1
    if not cand:
        return None
    return cand.most_common(1)[0][0]


def load_enrichment(path: str) -> dict:
    """Agent-produced {key: {category, full_name, name_confidence, ...}}.

    Merges the single enrichment.json (if present) with any per-batch files under
    data/enrich_parts/*.json, so classification can be added incrementally.
    """
    merged: dict = {}

    def absorb(data):
        # Per-key merge so category (local.json) and name (names.json) for the
        # same conversation combine instead of clobbering each other. Enrichment
        # records are always dicts; ignore any non-dict value so a stray cache
        # file (e.g. work_personal.json: {key: "work"}) can't overwrite a record.
        for k, v in data.items():
            if isinstance(v, dict):
                merged.setdefault(k, {}).update(v)

    if os.path.exists(path):
        try:
            absorb(json.load(open(path, encoding="utf-8")))
        except Exception as exc:
            print(f"  ! could not read {path}: {exc}")
    parts_dir = os.path.join(os.path.dirname(path), "enrich_parts")
    if os.path.isdir(parts_dir):
        for fn in sorted(os.listdir(parts_dir)):
            # filter_*.json are custom-filter results, work_personal.json is the
            # work/personal verdict cache — both have a different shape; skip.
            if fn.endswith(".json") and not fn.startswith("filter_") \
                    and fn != "work_personal.json":
                try:
                    absorb(json.load(open(os.path.join(parts_dir, fn), encoding="utf-8")))
                except Exception as exc:
                    print(f"  ! could not read {fn}: {exc}")
    return merged


def load_custom_filters(parts_dir: str):
    """Read filter_<slug>.json files (from `classify.py --filter`).

    Each is {"_filter": label, "keys": [...]}. Returns
    {label: set(keys)} so build.py can tag matching conversations.
    """
    out = {}
    if not os.path.isdir(parts_dir):
        return out
    for fn in sorted(os.listdir(parts_dir)):
        if fn.startswith("filter_") and fn.endswith(".json"):
            try:
                cfg = json.load(open(os.path.join(parts_dir, fn), encoding="utf-8"))
                if cfg.get("_filter"):
                    out[cfg["_filter"]] = set(cfg.get("keys", []))
            except Exception as exc:
                print(f"  ! could not read {fn}: {exc}")
    return out


# Kinship terms used to find the "family" group chats.
KIN_TERMS = {"mom", "mum", "mommy", "mother", "dad", "daddy", "father", "papa",
             "pa", "ma", "bhai", "bhaiya", "didi", "beta", "nani", "nana",
             "dadi", "dada", "mama", "mami", "masi", "chacha", "bua"}


def compute_family(convos, user_name: str) -> set:
    """Family = members of group chats that contain a kinship-named anchor
    (Mom/Dad/...). Far more precise than guessing from surnames. Returns the set
    of conversation keys (people + the family groups themselves)."""
    name2key = {c["name"]: c["key"] for c in convos.values() if c["kind"] == "person"}

    def is_anchor(name):
        toks = name.lower().replace("(", " ").split()
        return bool(toks) and toks[0] in KIN_TERMS

    anchors = {c["name"] for c in convos.values()
               if c["kind"] == "person" and is_anchor(c["name"])}
    fam_keys = set()
    fam_names = set(anchors)
    for c in convos.values():
        if c["kind"] == "group" and (set(c["members"]) & anchors):
            fam_keys.add(c["key"])            # the family group itself
            fam_names |= set(c["members"])
    for nm in fam_names:
        if nm in name2key:
            fam_keys.add(name2key[nm])
    fam_keys.discard(name2key.get(user_name))  # never the user themselves
    return fam_keys


# --------------------------------------------------------------------------- #
# Mutually-exclusive category partition
#
# Every person gets EXACTLY ONE category from {Contractors, Family, Work,
# Personal}, assigned by PRIORITY (highest wins): Contractors -> Family -> Work
# -> Personal. Adding a category re-partitions; it never duplicates. This is the
# deterministic first pass — no model Work-vs-Personal judgment yet (Work for now
# is only the curated 76-key list). Personal is the catch-all default.
# --------------------------------------------------------------------------- #
def assign_category(person, *, contractor_keys, family_keys, work_keys,
                    base_category=None) -> str:
    """Return the single category for a person by priority.

    `base_category` is whatever the model/old logic produced (Family / Personal /
    Service / None). A base of "Service" folds into Contractors (Service is gone).

    Priority (highest first):
      1. key in contractor_keys OR base_category == "Service" -> Contractors
      2. key in family_keys (from compute_family)             -> Family
      3. key in work_keys                                     -> Work
      4. else                                                 -> Personal
    """
    key = person.get("key")
    if key in contractor_keys or base_category == "Service":
        return "Contractors"
    if key in family_keys:
        return "Family"
    if key in work_keys:
        return "Work"
    return "Personal"


def refine_personal(model_verdict, in_contacts):
    """Split a deterministic-Personal person into Work vs Personal.

    Applied ONLY to people whose base category is Personal (Contractors / Family /
    curated-Work are never re-judged). Combines the local model's work/personal/
    unsure read of the conversation with the in_contacts prior — a resolved
    address-book name is a strong PERSONAL signal; a bare unnamed number leans
    transactional/Work.

    Returns (category, unsure):
      - "work"     -> ("Work", False)        confident work
      - "personal" -> ("Personal", False)    confident personal
      - "unsure" + in_contacts (named) -> ("Personal", True)   named prior wins
      - "unsure" + bare number         -> ("Work", True)       lean Work, flag it

    The `unsure` flag is the Phase-3 email-lookup trigger: those rows self-heal as
    email comes online (confirmed Work or moved to Personal).
    """
    if model_verdict == "work":
        return ("Work", False)
    if model_verdict == "personal":
        return ("Personal", False)
    # unsure: lean on the in_contacts prior, and flag the guess.
    if in_contacts:
        return ("Personal", True)
    return ("Work", True)


WORK_JUDGE_CACHE = os.path.join(DATA, "enrich_parts", "work_personal.json")


def refine_work_personal(personal_people, digest_by_key):
    """Mutate the deterministic-Personal people in place: set final category +
    `unsure` from the local model's work/personal/unsure verdict.

    Verdicts are cached in data/enrich_parts/work_personal.json so a rebuild only
    runs the model on people not yet judged. CRM_SKIP_WORKJUDGE=1 skips the model
    (people without a cached verdict stay Personal, unsure=False). Loading the
    model is deferred until there's actually something to judge.
    """
    if not personal_people:
        return

    # Cached verdicts from prior runs / a standalone `classify.py --workpersonal`.
    cache = {}
    if os.path.exists(WORK_JUDGE_CACHE):
        try:
            cache = json.load(open(WORK_JUDGE_CACHE, encoding="utf-8"))
        except Exception as exc:
            print(f"  ! could not read {WORK_JUDGE_CACHE}: {exc}")

    todo = [p for p in personal_people if p["key"] not in cache]
    skip_model = os.environ.get("CRM_SKIP_WORKJUDGE") == "1"
    if todo and not skip_model:
        digests_todo = [digest_by_key[p["key"]] for p in todo
                        if p["key"] in digest_by_key]
        print(f"  work/personal: judging {len(digests_todo)} Personal people "
              f"({len(personal_people) - len(todo)} cached)...")
        try:
            import classify
            model, tok = classify.load_model()
            new = classify.judge_work_personal(model, tok, digests_todo)
            cache.update(new)
            # Snapshot the prior cache before overwriting, then persist.
            if os.path.exists(WORK_JUDGE_CACHE):
                import shutil
                shutil.copy(WORK_JUDGE_CACHE, WORK_JUDGE_CACHE + ".bak")
            json.dump(cache, open(WORK_JUDGE_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=0)
        except Exception as exc:
            print(f"  ! work/personal judgment unavailable ({exc}); "
                  "leaving these Personal.")
    elif todo and skip_model:
        print(f"  work/personal: CRM_SKIP_WORKJUDGE=1 — {len(todo)} people "
              "stay Personal (no verdict).")

    n_to_work = n_flagged = 0
    for p in personal_people:
        verdict = cache.get(p["key"], "personal")  # no verdict -> stay Personal
        category, unsure = refine_personal(verdict, p["in_contacts"])
        p["category"] = category
        p["unsure"] = unsure
        if category == "Work":
            n_to_work += 1
        if unsure:
            n_flagged += 1
    print(f"  work/personal: {n_to_work} Personal -> Work, {n_flagged} flagged unsure.")


# --------------------------------------------------------------------------- #
# Family detection post-pass (grows Family beyond the kinship-group heuristic)
#
# compute_family() only finds relatives in Mom/Dad-anchored GROUP chats, missing
# 1:1 family (spouse/parents/siblings/kids/in-laws). After the partition + the
# work/personal refinement, a conservative model pass re-judges people who are
# currently Work or Personal and promotes clear relatives to Family. Family
# outranks Work/Personal (priority Contractors > Family > Work > Personal), so a
# `family` verdict wins over those — but Contractors and already-Family people
# are NEVER re-judged. Verdicts cache at data/family_judge.json — deliberately
# OUTSIDE data/enrich_parts/ so load_enrichment never merges it.
# --------------------------------------------------------------------------- #
FAMILY_JUDGE_CACHE = os.path.join(DATA, "family_judge.json")


def apply_family_verdict(category, verdict):
    """Pure: the person's category after the family verdict.

    Only Work / Personal people can be promoted to Family. Contractors and
    already-Family people are returned unchanged regardless of the verdict.
    """
    if category in ("Work", "Personal") and verdict == "family":
        return "Family"
    return category


# The 4B model, despite the explicit instruction in FAMILY_SYSTEM, still mislabels
# a few obvious non-relatives as family: the user themselves, and "school/activity
# parent" / role contacts whose NAME gives them away ("Pat (Riley's Dad)",
# "Robin (Dad Realtor)"). Veto those structurally — a name-level signal the model
# won't follow on its own.
_FAMILY_VETO_RE = re.compile(
    r"'s\s+(dad|mom|mother|father|parent|son|daughter)\b"
    r"|\b(realtor|realator|dentist|doctor|agent|coach|teacher|nanny|tutor|principal)\b",
    re.I)


def _family_vetoed(name: str, user_name: str) -> bool:
    """True if `name` is an obvious non-relative the model wrongly called family:
    the user's own contact, or a parenthetical that marks them as someone else's
    parent / a professional role."""
    nm = (name or "").strip()
    if user_name and nm.lower() == user_name.strip().lower():
        return True
    m = re.search(r"\(([^)]*)\)", nm)
    return bool(m and _FAMILY_VETO_RE.search(m.group(1)))


def refine_family(people, verdicts, user_name=""):
    """Mutate `people` in place: promote Work/Personal people with a `family`
    verdict to Family. Returns the count newly promoted.

    Groups are untouched (they are not part of the person partition). Contractors
    and already-Family people are never changed (apply_family_verdict enforces it).
    Obvious non-relatives (self, school/activity parents) are vetoed structurally,
    and so are bare non-contacts: a real 1:1 relative is essentially always a saved
    contact, so an unsaved number/email (e.g. a marketplace stranger whose thread
    merely mentions "my mom") is never promoted to Family by the model.
    """
    promoted = 0
    for p in people:
        if p.get("kind") != "person":
            continue
        # Veto obvious non-relatives OUT of Family — covers the kinship-group
        # heuristic too (e.g. "Robin (Dad Realtor)" who appears in a family group
        # chat). A vetoed name was never a relative; demote to Personal.
        if p["category"] == "Family" and _family_vetoed(p.get("name", ""), user_name):
            p["category"] = "Personal"
            continue
        verdict = verdicts.get(p["key"], "not_family")
        new_cat = apply_family_verdict(p["category"], verdict)
        if (new_cat != p["category"]
                and p.get("in_contacts")
                and not _family_vetoed(p.get("name", ""), user_name)):
            p["category"] = new_cat
            promoted += 1
    return promoted


def refine_family_pass(people, digest_by_key, user_name=""):
    """Run the conservative family judgment over the Work/Personal people and
    promote relatives to Family. Verdicts cache at data/family_judge.json (load
    existing, judge only new keys, save). CRM_SKIP_FAMILYJUDGE=1 skips the model
    (no promotions). Loading the model is deferred until there's work to do."""
    candidates = [p for p in people if p.get("kind") == "person"
                  and p["category"] in ("Work", "Personal")]
    if not candidates:
        return

    cache = {}
    if os.path.exists(FAMILY_JUDGE_CACHE):
        try:
            cache = json.load(open(FAMILY_JUDGE_CACHE, encoding="utf-8"))
        except Exception as exc:
            print(f"  ! could not read {FAMILY_JUDGE_CACHE}: {exc}")

    todo = [p for p in candidates if p["key"] not in cache]
    skip_model = os.environ.get("CRM_SKIP_FAMILYJUDGE") == "1"
    if todo and not skip_model:
        digests_todo = [digest_by_key[p["key"]] for p in todo
                        if p["key"] in digest_by_key]
        print(f"  family: judging {len(digests_todo)} Work/Personal people "
              f"({len(candidates) - len(todo)} cached)...")
        try:
            import classify
            model, tok = classify.load_model()
            new = classify.judge_family(model, tok, digests_todo)
            cache.update(new)
            if os.path.exists(FAMILY_JUDGE_CACHE):
                import shutil
                shutil.copy(FAMILY_JUDGE_CACHE, FAMILY_JUDGE_CACHE + ".bak")
            json.dump(cache, open(FAMILY_JUDGE_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=0)
        except Exception as exc:
            print(f"  ! family judgment unavailable ({exc}); no promotions.")
    elif todo and skip_model:
        print(f"  family: CRM_SKIP_FAMILYJUDGE=1 — {len(todo)} people not judged.")

    promoted = refine_family(people, cache, user_name)
    print(f"  family: {promoted} Work/Personal -> Family.")


# Seeded PRM category-filters — ordinary, redefinable, OVERLAPPING filters a new
# user starts with (a person can be in several). These are the common PRM
# categories; the user can rename / redefine / delete any of them. "The
# description IS the filter." Hidden people are tracked via EXCLUDED (the Hide
# action) and reviewed from the ⋯ overflow menu — there is no Junk category.
#
# The category chips are now the FOUR mutually-exclusive computed category
# filters — each matches the single `category` field build.py assigns per person
# (Contractors > Family > Work > Personal). They do NOT overlap: a person appears
# under exactly one chip. Service folds into Contractors; Catch-up is replaced by
# an Oldest sort; Groups becomes a separate toggle (not a chip).
DEFAULT_FILTERS = [
    {"id": "family", "name": "Family", "type": "computed",
     "rule": {"kind": "person", "category": "Family"},
     "color": "#ff2d55",
     "description": "my family members — relatives I'm related to by blood or marriage"},
    {"id": "personal", "name": "Personal", "type": "computed",
     "rule": {"kind": "person", "category": "Personal"},
     "color": "#34c759",
     "description": "friends and personal acquaintances I relate to socially — people in my "
                    "personal life, not work contacts and not vendors"},
    {"id": "work", "name": "Work", "type": "computed",
     "rule": {"kind": "person", "category": "Work"},
     "color": "#0b84fe",
     "description": "work and professional contacts — colleagues, clients, business partners, "
                    "and anyone I know primarily through my job or profession"},
    {"id": "contractors", "name": "Contractors", "type": "computed",
     "rule": {"kind": "person", "category": "Contractors"},
     "color": "#ff9500",
     "description": "vendors, service providers, contractors, and transactional contacts — a "
                    "business reaching out about a job, quote, scheduling, or one-off "
                    "transaction, not personal life"},
]


def _norm_slug(s) -> str:
    """Normalize a filter id/slug for equivalence: lowercase, strip hyphens,
    spaces, and underscores. So 'catch-up' ≡ 'catchup' ≡ 'Catch_Up'."""
    return re.sub(r"[-_\s]+", "", str(s or "").lower())


def backfill_defaults(existing: list) -> list:
    """Additively seed any DEFAULT_FILTERS the user doesn't already have, matching
    on NORMALIZED slug (not exact id) so a renamed equivalent suppresses the seed.

    NEVER overwrites or removes a user's existing filter — it only appends missing
    seeded categories. A seeded default is skipped when its normalized slug matches
    the normalized slug of any existing filter's id, so e.g. an existing 'catch-up'
    suppresses the seeded 'catchup'. Returns the (same) list with seeds appended.
    """
    have_slugs = {_norm_slug(f.get("id")) for f in existing}
    for d in DEFAULT_FILTERS:
        if _norm_slug(d["id"]) not in have_slugs:
            existing.append(copy.deepcopy(d))
            have_slugs.add(_norm_slug(d["id"]))
    return existing


# The category partition retires these as chips. They are reconciled OUT of the
# live filter list on every build so a rebuild reproduces exactly the four
# computed category chips: Service folds into Contractors, Catch-up becomes an
# Oldest sort, Groups becomes a separate toggle, and the old SEMANTIC work /
# contractors chips are replaced by the COMPUTED partition chips of the same id.
RETIRED_FILTER_SLUGS = {_slug for _slug in
                        (_norm_slug(s) for s in
                         ("catch-up", "catchup", "service", "groups"))}

# Curated category seed filters (filter_<slug>.json) that drive the partition in
# build.py — they must NOT also be auto-registered as their own semantic chips.
PARTITION_SEED_LABELS = {"Contractors", "Catch-up"}


def sync_filters(custom_filters: dict):
    """Source of truth is data/filters.json (created from defaults once, then
    user-owned). The four computed category chips are reconciled to the current
    DEFAULT_FILTERS; retired category chips (catch-up/service/groups and the old
    semantic work/contractors) are dropped. A read-only copy is written to out/
    for the web UI to fetch."""
    fpath = os.path.join(DATA, "filters.json")
    if os.path.exists(fpath):
        try:
            filters = json.load(open(fpath, encoding="utf-8"))
        except Exception:
            filters = list(DEFAULT_FILTERS)
    else:
        filters = list(DEFAULT_FILTERS)
    # name -> description from the semantic filter key-files (for editing).
    descs = {}
    pdir = os.path.join(DATA, "enrich_parts")
    if os.path.isdir(pdir):
        for fn in os.listdir(pdir):
            if fn.startswith("filter_") and fn.endswith(".json"):
                try:
                    c = json.load(open(os.path.join(pdir, fn), encoding="utf-8"))
                    if c.get("_filter"):
                        descs[c["_filter"]] = c.get("description", "")
                except Exception:
                    pass
    # Reconcile the four category chips to the current partition definition:
    #   - drop retired category chips (catch-up / service / groups)
    #   - replace any chip whose slug matches a DEFAULT_FILTER (e.g. the old
    #     SEMANTIC work / contractors) with the COMPUTED partition default, so the
    #     chip becomes the exclusive computed category. User-added non-category
    #     custom filters (slugs not in defaults or retired set) are preserved.
    default_by_slug = {_norm_slug(f["id"]): f for f in DEFAULT_FILTERS}
    reconciled = []
    seen_default_slugs = set()
    for f in filters:
        slug = _norm_slug(f.get("id"))
        if slug in RETIRED_FILTER_SLUGS:
            continue  # retired category chip — drop it
        if slug in default_by_slug:
            reconciled.append(copy.deepcopy(default_by_slug[slug]))
            seen_default_slugs.add(slug)
            continue  # category chip — force to the computed partition default
        reconciled.append(f)  # user-added custom filter — preserve as-is
    # Seed any of the four category defaults that weren't present.
    for f in DEFAULT_FILTERS:
        if _norm_slug(f["id"]) not in seen_default_slugs:
            reconciled.append(copy.deepcopy(f))
            seen_default_slugs.add(_norm_slug(f["id"]))
    filters = reconciled
    have = {f.get("name") for f in filters}
    for label in custom_filters:  # register any semantic filter not yet listed
        # Skip curated category seeds — they drive the partition, not their own chip.
        if label in PARTITION_SEED_LABELS:
            continue
        if label not in have and _norm_slug(label) not in RETIRED_FILTER_SLUGS:
            filters.append({"id": re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-"),
                            "name": label, "type": "semantic",
                            "description": descs.get(label, ""), "color": "#5856d6"})
    json.dump(filters, open(fpath, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(filters, open(os.path.join(OUT, "filters.json"), "w", encoding="utf-8"), ensure_ascii=False)


def vcard_owner(path: str) -> str:
    """Owner of the phone. config.json: user_name wins; else first vCard FN."""
    cfg = os.path.join(HERE, "config.json")
    if os.path.exists(cfg):
        try:
            nm = (json.load(open(cfg)).get("user_name") or "").strip()
            if nm:
                return nm
        except Exception:
            pass
    if os.path.exists(path):
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("FN:"):
                    return line[3:].strip()
    return ""


# --------------------------------------------------------------------------- #
# vCard parsing (minimal, dependency-free)
# --------------------------------------------------------------------------- #
def parse_vcard(path: str):
    """Return (name_by_key, photo_by_key).

    name_by_key:  normalized phone/email -> display name
    photo_by_key: normalized phone/email -> raw JPEG/PNG bytes
    """
    name_by_key: dict[str, str] = {}
    photo_by_key: dict[str, bytes] = {}
    if not os.path.exists(path):
        print("\n" + "!" * 60)
        print(f"! NO vCARD at {path}")
        print("! Names will fall back to raw phone numbers. Export Contacts:")
        print("!   Contacts.app -> Cmd-A -> File -> Export -> Export vCard...")
        print("!   then save as data/contacts.vcf (or set CONTACTS_VCF).")
        print("!" * 60 + "\n")
        return name_by_key, photo_by_key

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    # Unfold: continuation lines begin with a space or tab.
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"\n[ \t]", "", raw)

    cards = re.split(r"BEGIN:VCARD", raw)
    n_cards = n_named = n_photo = 0
    for card in cards:
        if "FN" not in card:
            continue
        n_cards += 1
        fn = None
        tels: list[str] = []
        emails: list[str] = []
        photo_b64 = None

        for line in card.split("\n"):
            # Strip Apple item-group prefix: "item1.TEL;..." -> "TEL;..."
            line = re.sub(r"^item\d+\.", "", line)
            upper = line.upper()
            if upper.startswith("FN"):
                fn = line.split(":", 1)[-1].strip()
            elif upper.startswith("TEL"):
                tels.append(line.split(":", 1)[-1].strip())
            elif upper.startswith("EMAIL"):
                emails.append(line.split(":", 1)[-1].strip())
            elif upper.startswith("PHOTO"):
                photo_b64 = line.split(":", 1)[-1].strip()

        if not fn:
            continue
        n_named += 1
        photo_bytes = None
        if photo_b64:
            try:
                photo_bytes = base64.b64decode(photo_b64)
                n_photo += 1
            except Exception:
                photo_bytes = None

        for raw_id in tels + emails:
            key = norm_key(raw_id)
            if not key:
                continue
            # First contact to claim a key wins; don't clobber a real name.
            name_by_key.setdefault(key, fn)
            if photo_bytes and key not in photo_by_key:
                photo_by_key[key] = photo_bytes

    print(f"  parsed {n_cards} cards, {n_named} named, {n_photo} with photos")
    return name_by_key, photo_by_key


# --------------------------------------------------------------------------- #
# Database extraction
# --------------------------------------------------------------------------- #
def apple_ns_to_iso(date_val) -> str:
    """Apple timestamp (ns or s since 2001-01-01 UTC) -> ISO-8601 UTC string.
    Returns "" for missing/garbage values so one bad row never aborts a build."""
    import datetime as _dt
    try:
        if date_val is None:
            return ""
        date_val = int(date_val)
        secs = date_val / 1e9 if date_val > 1_000_000_000_000 else float(date_val)
        return _dt.datetime.fromtimestamp(
            secs + APPLE_EPOCH, tz=_dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError, TypeError):
        return ""


def merge_recency(last_1to1: str, group_iso: str) -> str:
    """A person's effective recency: the later of their 1:1 thread's last message
    and their own most-recent message in any shared group chat. Both are ISO
    timestamps (same format), which compare correctly lexicographically; either
    may be "" (no 1:1 thread, or no group activity)."""
    return max(last_1to1 or "", group_iso or "")


# --------------------------------------------------------------------------- #
# Incremental embedding cache
#
# A full rebuild re-embeds ALL chunks (~43s for ~4.5k chunks) even when almost
# nothing changed. We persist a per-conversation SIGNATURE map alongside the
# index (out/embed_sig.json = {key: sig}); on the next build, conversations whose
# signature is unchanged reuse their cached chunk vectors + texts and only
# new/changed conversations are re-embedded. The reuse decision is the pure
# embeddings.partition_reuse(); these helpers are the thin I/O + signature glue.
# NOTE: this makes EMBEDDING (and classification, below) incremental — the
# message PARSE is still full every run (a ROWID delta-read is a separate layer).
# --------------------------------------------------------------------------- #
EMBED_SIG_PATH = os.path.join(OUT, "embed_sig.json")


def build_signatures(convo_msgs) -> dict:
    """{key: signature} for everything being indexed this run.

    `convo_msgs` is the list of (key, msgs) assembled during the main loop. The
    signature is derived from message count + the latest message's date, both of
    which change when a conversation gains a message.
    """
    import embeddings as _embeddings
    sigs = {}
    for key, msgs in convo_msgs:
        last_date = msgs[-1]["date"] if msgs else ""
        sigs[key] = _embeddings.convo_signature(len(msgs), last_date)
    return sigs


def load_prev_signatures(path: str = EMBED_SIG_PATH) -> dict:
    """Load the prior run's {key: sig} map, or {} if missing/unreadable.

    A missing or corrupt map means "treat everything as new" -> full embed,
    which is the correct graceful fallback for a first run or a format change.
    """
    if not os.path.exists(path):
        return {}
    try:
        data = json.load(open(path, encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"  ! could not read {path}: {exc}; treating all convos as new.")
        return {}


def invalidate_changed_verdicts(new_sigs: dict, prev_sigs: dict) -> int:
    """Drop changed-signature keys from the classification verdict caches so they
    get re-judged this run. Unchanged keys keep their cached verdict.

    A conversation whose signature changed has new messages, so its cached
    work/personal + family verdicts may be stale. We delete ONLY those keys from
    data/enrich_parts/work_personal.json and data/family_judge.json (minimal +
    safe). Brand-new keys aren't in the caches yet, so nothing to drop there.
    Returns the number of cache entries removed across both files.
    """
    # Keys present last run whose signature differs this run = changed convos.
    changed = {k for k, sig in new_sigs.items()
               if k in prev_sigs and prev_sigs[k] != sig}
    if not changed:
        return 0
    removed = 0
    for cache_path in (WORK_JUDGE_CACHE, FAMILY_JUDGE_CACHE):
        if not os.path.exists(cache_path):
            continue
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception as exc:
            print(f"  ! could not read {cache_path} for invalidation: {exc}")
            continue
        if not isinstance(cache, dict):
            continue
        drop = [k for k in changed if k in cache]
        if not drop:
            continue
        for k in drop:
            del cache[k]
            removed += 1
        json.dump(cache, open(cache_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=0)
    if removed:
        print(f"  incremental: invalidated {removed} stale classification "
              f"verdict(s) for {len(changed)} changed convo(s).")
    return removed


def load_handles(conn) -> dict[int, str]:
    return {rid: hid for rid, hid in conn.execute("SELECT ROWID, id FROM handle")}


def load_chat_members(conn) -> dict[int, list[int]]:
    members: dict[int, list[int]] = defaultdict(list)
    for chat_id, handle_id in conn.execute(
        "SELECT chat_id, handle_id FROM chat_handle_join"
    ):
        members[chat_id].append(handle_id)
    return members


def main() -> None:
    if not os.environ.get("CRM_SKIP_ARCH_CHECK") and (
            platform.system() != "Darwin" or platform.machine() != "arm64"):
        sys.exit("build.py requires an Apple-Silicon Mac (MLX is arm64-only). "
                 "Set CRM_SKIP_ARCH_CHECK=1 to override.")
    # Clear prior generated output so out/ always matches the current data
    # (entry ids are positional and shift between runs). Only ever touches
    # build artifacts under out/, never source data.
    for sub in ("messages", "photos"):
        d = os.path.join(OUT, sub)
        if os.path.isdir(d):
            for fn in os.listdir(d):
                os.remove(os.path.join(d, fn))
    os.makedirs(os.path.join(OUT, "messages"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "photos"), exist_ok=True)

    live = os.path.realpath(DB_PATH).startswith(os.path.expanduser("~/Library/Messages"))
    print(f"Source db:  {DB_PATH}{'  (LIVE)' if live else ''}")
    print(f"Source vcf: {VCF_PATH}")
    if not os.path.exists(DB_PATH):
        raise SystemExit(f"\nERROR: no database at {DB_PATH}\n"
                         "Set CHAT_DB to a valid chat.db, or place a copy at data/chat.db.")

    print("Parsing vCard...")
    name_by_key, photo_by_key = parse_vcard(VCF_PATH)

    # Live names synced from Contacts.app (server's "Sync Contacts") win over the
    # vCard snapshot, so renames in Contacts show up after a sync.
    live_path = os.path.join(DATA, "contacts_live.json")
    if os.path.exists(live_path):
        try:
            live_names = json.load(open(live_path, encoding="utf-8"))
            for k, nm in live_names.items():
                if nm:
                    name_by_key[k] = nm
            print(f"  applied {len(live_names)} live Contacts names")
        except Exception as exc:
            print(f"  ! could not read contacts_live.json: {exc}")

    exclusions = load_exclusions(EXCLUDE_PATH)
    if exclusions["keys"] or exclusions["name_contains"] or exclusions["snippet_contains"]:
        print(f"  exclusions: {len(exclusions['keys'])} keys, "
              f"{len(exclusions['name_contains'])} name patterns, "
              f"{len(exclusions['snippet_contains'])} snippet patterns")

    print("Opening database (read-only)...")
    conn = imessage_db.open_readonly(DB_PATH)
    conn.text_factory = bytes  # need raw bytes for attributedBody
    handles = load_handles(conn)  # ROWID -> id (bytes)
    handles = {k: (v.decode("utf-8", "replace") if v else "") for k, v in handles.items()}
    chat_members = load_chat_members(conn)

    # Chat metadata
    chats = {}
    for rid, style, display_name, chat_id in conn.execute(
        "SELECT ROWID, style, display_name, chat_identifier FROM chat"
    ):
        chats[rid] = {
            "style": style,
            "display_name": (display_name or b"").decode("utf-8", "replace"),
            "chat_identifier": (chat_id or b"").decode("utf-8", "replace"),
        }

    def contact_label(handle_id: int) -> tuple[str, str, bool]:
        """Return (display_name, raw_id, in_contacts) for a handle ROWID."""
        raw = handles.get(handle_id, "")
        key = norm_key(raw)
        name = name_by_key.get(key)
        return (name or raw, raw, name is not None)

    # ------------------------------------------------------------------- #
    # Group all messages into conversations.
    #   1:1  -> keyed by the other party's normalized identity (merges
    #           SMS + iMessage + multiple chats for the same person)
    #   group -> keyed by "g<chat ROWID>"
    # ------------------------------------------------------------------- #
    print("Reading messages...")
    convos: dict[str, dict] = {}
    # Most recent date each person was active in ANY shared group chat (their own
    # messages only). Used so someone you chat with daily in a group isn't flagged
    # "lost touch" just because the 1:1 thread is old. See last_active below.
    group_active: dict[str, str] = {}

    def get_convo(key, kind, name, raw_id="", in_contacts=False):
        c = convos.get(key)
        if c is None:
            c = {
                "key": key,
                "kind": kind,
                "name": name,
                "raw_id": raw_id,
                "in_contacts": in_contacts,
                "sent": 0,
                "recv": 0,
                "msgs": [],
                "members": [],
            }
            convos[key] = c
        return c

    rows = conn.execute(
        """
        SELECT cmj.chat_id, m.handle_id, m.is_from_me, m.date,
               m.text, m.attributedBody, m.cache_has_attachments
        FROM message m
        JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
        ORDER BY m.date ASC
        """
    )

    for chat_id, handle_id, is_from_me, date_val, text, body, has_attach in rows:
        chat = chats.get(chat_id)
        if not chat:
            continue
        is_group = chat["style"] == 43 or len(chat_members.get(chat_id, [])) > 1

        if is_group:
            key = f"g{chat_id}"
            if key not in convos:
                member_ids = chat_members.get(chat_id, [])
                labels = [contact_label(h) for h in member_ids]
                name = chat["display_name"] or ", ".join(
                    l[0] for l in labels[:4]
                ) or chat["chat_identifier"] or "Group"
                c = get_convo(key, "group", name)
                c["members"] = [l[0] for l in labels]
            else:
                c = convos[key]
        else:
            # 1:1 — identify the other party.
            member_ids = chat_members.get(chat_id, [])
            other = member_ids[0] if member_ids else handle_id
            if other is None:
                continue
            name, raw, in_contacts = contact_label(other)
            key = "p" + norm_key(raw)
            c = get_convo(key, "person", name, raw, in_contacts)
            # Prefer a real contact name if we learn one later.
            if in_contacts and not c["in_contacts"]:
                c["name"], c["in_contacts"], c["raw_id"] = name, True, raw

        # Decode text.
        plain = None
        if text:
            plain = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
            plain = plain or None
        if not plain:
            plain = decode_body(body)
        if not plain:
            if has_attach:
                plain = "[attachment]"
            else:
                # Empty body with no attachment: a system event (someone added/
                # removed/renamed the group, etc.), not a real message. Skip it
                # entirely so it doesn't render as a blank bubble or keep an
                # otherwise-empty conversation alive.
                continue

        if is_from_me:
            c["sent"] += 1
        else:
            c["recv"] += 1

        iso = apple_ns_to_iso(date_val)
        sender = "me"
        if is_group and not is_from_me:
            s_name, s_raw, _ = contact_label(handle_id)
            sender = s_name
            # Credit the sender with group activity so their recency reflects it.
            if s_raw:
                gk = "p" + norm_key(s_raw)
                if iso > group_active.get(gk, ""):
                    group_active[gk] = iso
        c["msgs"].append(
            {
                "me": bool(is_from_me),
                "from": sender,
                "text": plain,
                "date": iso,
            }
        )

    conn.close()

    # ------------------------------------------------------------------- #
    # Apply the junk filter and emit.
    # ------------------------------------------------------------------- #
    print(f"Filtering {len(convos)} raw conversations...")
    enrichment = load_enrichment(ENRICH_PATH)
    if enrichment:
        print(f"  merging {len(enrichment)} enrichment records")
    custom_filters = load_custom_filters(os.path.join(DATA, "enrich_parts"))
    family_keys = compute_family(convos, vcard_owner(VCF_PATH))

    # Seed key sets for the mutually-exclusive category partition.
    #   contractor: curated list (filter_contractors.json) + base "Service" people
    #   work:       an optional curated Work key list at
    #               data/enrich_parts/filter_work.json (same {"keys":[...]} shape
    #               as the other filter_*.json). If absent, there is no Work seed
    #               and Work comes purely from the model's work/personal pass.
    contractor_keys = set(load_custom_filters(
        os.path.join(DATA, "enrich_parts")).get("Contractors", set()))
    work_keys: set = set()
    work_seed = os.path.join(DATA, "enrich_parts", "filter_work.json")
    if os.path.exists(work_seed):
        try:
            work_keys = set(json.load(open(work_seed, encoding="utf-8")).get("keys", []))
        except Exception as exc:
            print(f"  ! could not read work seed keys from {work_seed}: {exc}")
    print(f"  family: {len(family_keys)} conversations; "
          f"contractor seed: {len(contractor_keys)}, work seed: {len(work_keys)}; "
          f"custom filters: {list(custom_filters) or 'none'}")
    people = []
    digests = []  # compact per-convo summaries for agent analysis
    convo_msgs = []  # (key, full msgs) for the chunked full-content embed index
    kept = dropped = excluded = empty = spam = alert = 0
    filtered_review = []  # what the auto rules removed, for the user to audit
    idx = 0
    for c in convos.values():
        msgs = c["msgs"]

        # No readable content (only system events / blank) -> not a conversation.
        if not msgs:
            empty += 1
            continue

        last = msgs[-1] if msgs else {"text": "", "date": ""}
        snippet = next(
            (m["text"] for m in reversed(msgs) if m["text"] and m["text"] != "[attachment]"),
            last["text"],
        )

        # Force-keep wins over every auto rule (rescues false positives).
        if c["key"] not in exclusions["keep"]:
            # User feedback: explicit "mark as junk" + generalized type patterns.
            if is_excluded(c["key"], c["name"], snippet, exclusions):
                excluded += 1
                continue
            # Replied STOP/unsubscribe to a non-contact -> marketing line.
            # (Never for saved contacts: a stray "stop" mid-thread isn't an opt-out.)
            if not c["in_contacts"] and replied_optout(msgs):
                spam += 1
                filtered_review.append(
                    {"reason": "opt-out reply", "key": c["key"], "name": c["name"],
                     "raw_id": c["raw_id"], "sent": c["sent"], "recv": c["recv"],
                     "snippet": snippet[:100]})
                continue
            # Appointment/flight/delivery/code style alert feed from a stranger.
            if c["kind"] == "person" and looks_like_alert(
                    msgs, c["in_contacts"], c["sent"], c["recv"]):
                alert += 1
                filtered_review.append(
                    {"reason": "alert/notification", "key": c["key"], "name": c["name"],
                     "raw_id": c["raw_id"], "sent": c["sent"], "recv": c["recv"],
                     "snippet": snippet[:100]})
                continue

        if c["kind"] == "person":
            raw = c["raw_id"]
            if is_shortcode(raw) and not c["in_contacts"]:
                dropped += 1
                continue
            two_way = c["sent"] > 0 and c["recv"] > 0
            if not (two_way or c["in_contacts"]):
                dropped += 1
                continue
        # groups always kept
        kept += 1
        idx += 1
        out_id = f"{c['kind'][0]}{idx:04d}"

        # Photo, if we have one for this person.
        photo_rel = None
        if c["kind"] == "person":
            pbytes = photo_by_key.get(norm_key(c["raw_id"]))
            if pbytes:
                photo_rel = f"photos/{out_id}.jpg"
                with open(os.path.join(OUT, photo_rel), "wb") as pf:
                    pf.write(pbytes)

        # Local inference (persons only).
        birthday = first_name = None
        if c["kind"] == "person":
            birthday = infer_birthday(msgs)
            if not c["in_contacts"]:
                first_name = infer_first_name(msgs)

        # Enrichment (category from the local model), keyed by stable key.
        enr = enrichment.get(c["key"], {})
        if c["kind"] == "person":
            # Mutually-exclusive partition: exactly one of Contractors / Family /
            # Work / Personal, by priority. base_category is the old model output
            # (Family/Personal/Service) — "Service" folds into Contractors.
            category = assign_category(
                c,
                contractor_keys=contractor_keys,
                family_keys=family_keys,
                work_keys=work_keys,
                base_category=enr.get("category"),
            )
        else:
            # Groups are not part of the person partition (toggled separately
            # later); leave their category as the model/old value or unset.
            category = enr.get("category")
        tags = [lbl for lbl, ks in custom_filters.items() if c["key"] in ks]

        entry = {
            "id": out_id,
            "key": c["key"],
            "name": c["name"],
            "kind": c["kind"],
            "raw_id": c["raw_id"],
            "in_contacts": c["in_contacts"],
            "members": c["members"],
            "count": len(msgs),
            "sent": c["sent"],
            "recv": c["recv"],
            "last_date": last["date"],
            # Recency for the "Last contact" column / sort / lost-touch view:
            # the later of the 1:1 thread and the person's own group activity.
            "last_active": merge_recency(last["date"], group_active.get(c["key"], "")),
            "snippet": (snippet or "")[:120],
            "photo": photo_rel,
            "first_name": first_name,
            "birthday": birthday,
            "category": category,
            # True only for low-confidence Work/Personal guesses (set below in the
            # work/personal refinement pass). Everyone else stays False.
            "unsure": False,
            "tags": tags,
            # Lowercased text for instant client-side keyword filters.
            "blob": (" ".join([c["name"]] + c["members"][:6]
                     + [m["text"] or "" for m in msgs[:6]]
                     + [m["text"] or "" for m in msgs[-6:]]).lower())[:600],
            "suggested_name": enr.get("full_name") or enr.get("suggested_name"),
            "name_confidence": enr.get("name_confidence"),
            "name_reason": enr.get("name_reason"),
        }
        people.append(entry)

        with open(os.path.join(OUT, "messages", f"{out_id}.json"), "w", encoding="utf-8") as mf:
            json.dump(msgs, mf, ensure_ascii=False)

        # Compact digest for agent analysis (not full transcripts).
        def _trim(m):
            return {"me": m["me"], "from": m["from"],
                    "text": (m["text"] or "")[:140]}
        sample = [_trim(m) for m in msgs[:2]]
        if len(msgs) > 5:
            mid = len(msgs) // 2
            sample += [_trim(m) for m in msgs[mid:mid + 1]]
        sample += [_trim(m) for m in msgs[-3:]]
        digests.append({
            "key": c["key"],
            "name": c["name"],
            "kind": c["kind"],
            "raw_id": c["raw_id"],
            "in_contacts": c["in_contacts"],
            "members": c["members"][:8],
            "count": len(msgs),
            "sent": c["sent"],
            "recv": c["recv"],
            "span": [msgs[0]["date"][:10], last["date"][:10]],
            "first_name_guess": first_name,
            "birthday_guess": birthday,
            "sample": sample,
        })
        # Full message text for the chunked embedding index (built after refine).
        convo_msgs.append((c["key"], msgs))

    # ------------------------------------------------------------------- #
    # Work-vs-Personal refinement of the deterministic-Personal catch-all.
    #
    # Only people the partition left as Personal are re-judged (Contractors /
    # Family / curated-Work are never touched). The local model reads each
    # conversation -> work/personal/unsure; refine_personal() combines that with
    # the in_contacts prior and sets the final category + the `unsure` flag.
    # Verdicts are cached so a rebuild doesn't re-run the model on every person.
    # Set CRM_SKIP_WORKJUDGE=1 to skip the model entirely (e.g. fast/test builds);
    # without verdicts, Personal people simply stay Personal (unsure=False).
    # ------------------------------------------------------------------- #
    # Incremental signatures: compare this run's per-conversation signatures to
    # the prior run's (out/embed_sig.json). Changed-signature conversations have
    # new messages, so their cached work/personal + family verdicts may be stale
    # — drop ONLY those keys from the verdict caches so they get re-judged below.
    # Unchanged keys stay cached. (The same prev/new sig comparison drives the
    # incremental embed step further down.) These are computed once and reused.
    new_sigs = build_signatures(convo_msgs)
    prev_sigs = load_prev_signatures()
    invalidate_changed_verdicts(new_sigs, prev_sigs)

    digest_by_key = {d["key"]: d for d in digests}
    personal_people = [p for p in people
                       if p["kind"] == "person" and p["category"] == "Personal"]
    refine_work_personal(personal_people, digest_by_key)

    # Family detection post-pass: a conservative model judgment promotes 1:1
    # relatives (missed by the kinship-GROUP heuristic) from Work/Personal to
    # Family. Runs after work/personal so it sees final Work assignments, and
    # Family outranks them. Contractors / already-Family are never re-judged.
    refine_family_pass(people, digest_by_key, vcard_owner(VCF_PATH))

    # Sort sidebar by most recent activity.
    people.sort(key=lambda p: p.get("last_active") or p["last_date"], reverse=True)
    with open(os.path.join(OUT, "people.json"), "w", encoding="utf-8") as f:
        json.dump(people, f, ensure_ascii=False, indent=0)

    sync_filters(custom_filters)

    # Audit trail: what the automatic spam/alert rules removed.
    filtered_review.sort(key=lambda r: (r["reason"], -r["recv"]))
    with open(os.path.join(OUT, "filtered_out.json"), "w", encoding="utf-8") as f:
        json.dump(filtered_review, f, ensure_ascii=False, indent=2)

    # Digests for agent analysis (categories + full names).
    with open(os.path.join(OUT, "digests.json"), "w", encoding="utf-8") as f:
        json.dump(digests, f, ensure_ascii=False)
    n_bday = sum(1 for p in people if p["birthday"])
    print(f"Inferred {n_bday} birthdays; wrote {OUT}/digests.json "
          f"({len(digests)} convos) for enrichment.")

    # ------------------------------------------------------------------- #
    # Chunked full-content embedding index for hybrid semantic search.
    #
    # ROOT-CAUSE FIX: the old index embedded only the 6-message digest SAMPLE per
    # conversation, so a topic mentioned in passing (e.g. "pizza") was invisible
    # to search. We now embed the FULL conversation, split into CHUNK windows of
    # CHUNK_WINDOW messages each. Caps keep one giant thread from dominating:
    # only the most-recent CHUNK_MAX_MESSAGES are considered, and at most
    # CHUNK_MAX_CHUNKS windows per conversation (sampled evenly when over). Each
    # chunk is one row; many rows share a conversation key.
    #
    # We write THREE parallel artifacts to out/ (gitignored):
    #   embeddings.npy        (N_chunks, d) float32, L2-normalized
    #   embedding_keys.json   conversation key for each chunk row
    #   embedding_chunks.json the chunk TEXT for each row (so the confirm step
    #                         can judge the real matched content, not the sample)
    # server.py loads all three, does cosine top-K over CHUNKS, then aggregates
    # chunks -> persons (best score + best chunk per key) before the LLM confirm.
    # bge-small is fast (~thousands of short chunks in seconds). Skippable for
    # fast/test builds via CRM_SKIP_EMBED=1.
    #
    # Window size chosen: ~25 messages per chunk (short threads -> one chunk;
    # long ones -> evenly-sampled windows). For this corpus (~1000 convos,
    # ~150k msgs) that is roughly ~4–5k chunks.
    # ------------------------------------------------------------------- #
    CHUNK_WINDOW = 25         # messages per chunk window
    CHUNK_MAX_MESSAGES = 2000  # most-recent messages considered per conversation
    CHUNK_MAX_CHUNKS = 60      # max chunks per conversation (even-sampled if over)
    npy_path = os.path.join(OUT, "embeddings.npy")
    keys_path = os.path.join(OUT, "embedding_keys.json")
    chunks_path = os.path.join(OUT, "embedding_chunks.json")
    if os.environ.get("CRM_SKIP_EMBED") == "1":
        print("  embeddings: CRM_SKIP_EMBED=1 — skipping index build.")
    elif convo_msgs:
        import shutil
        import time as _time
        import numpy as np
        import embeddings as _embeddings
        t0 = _time.time()

        # --- Incremental reuse: load the prior index and decide per convo ----
        # Build a per-key map of the OLD index's chunk rows (vectors) + texts so
        # an unchanged conversation can be copied wholesale instead of re-embedded.
        # Any failure to load a consistent old index -> old_by_key stays empty,
        # so EVERY convo re-embeds (the graceful full-build fallback: first run,
        # shape mismatch, or a format change).
        old_by_key: dict = {}   # key -> (list_of_vector_rows, list_of_chunk_texts)
        try:
            if (os.path.exists(npy_path) and os.path.exists(keys_path)
                    and os.path.exists(chunks_path)):
                old_matrix = np.load(npy_path)
                old_keys = json.load(open(keys_path, encoding="utf-8"))
                old_chunks = json.load(open(chunks_path, encoding="utf-8"))
                # All three artifacts MUST be row-parallel, or the old index is
                # untrustworthy -> fall back to a full embed.
                if (old_matrix.ndim == 2
                        and old_matrix.shape[0] == len(old_keys) == len(old_chunks)):
                    for i, k in enumerate(old_keys):
                        slot = old_by_key.setdefault(k, ([], []))
                        slot[0].append(old_matrix[i])
                        slot[1].append(old_chunks[i])
                else:
                    print("  embeddings: prior index shape mismatch — full re-embed.")
        except Exception as exc:
            print(f"  embeddings: could not load prior index ({exc}) — full re-embed.")
            old_by_key = {}

        # Reuse decision is the pure partition over signatures. With no usable old
        # index, prev_sigs may still exist but old_by_key is empty, so a "reuse"
        # key with no cached rows is re-embedded anyway (handled in the loop).
        reuse_keys, reembed_keys = _embeddings.partition_reuse(new_sigs, prev_sigs)
        reuse_set = set(reuse_keys)

        # --- Plan the assembly in ONE pass over convo_msgs --------------------
        # `plan` is a row-ordered list of ("reuse", vector) or ("embed", text)
        # entries; chunk_keys / chunk_texts are built parallel to it. We embed the
        # collected "embed" texts in a single batched call, then materialize the
        # matrix by walking the plan once — no second chunking pass, and the row
        # order provably matches chunk_keys/chunk_texts.
        chunk_keys: list = []
        chunk_texts: list = []
        plan: list = []             # per row: ("reuse", vec) | ("embed", text)
        to_embed_texts: list = []   # texts needing a fresh embedding (batched)
        n_reused_convos = n_reembed_convos = n_reused_chunks = 0
        for key, msgs in convo_msgs:
            cached = old_by_key.get(key)
            if key in reuse_set and cached and cached[1]:
                # Reuse this conversation's cached chunk vectors + texts verbatim.
                for vec, ctext in zip(cached[0], cached[1]):
                    chunk_keys.append(key)
                    chunk_texts.append(ctext)
                    plan.append(("reuse", vec))
                    n_reused_chunks += 1
                n_reused_convos += 1
            else:
                # New / changed convo (or no cached rows) -> re-chunk + re-embed.
                for ctext in _embeddings.chunk_messages(
                        msgs, window=CHUNK_WINDOW,
                        max_messages=CHUNK_MAX_MESSAGES, max_chunks=CHUNK_MAX_CHUNKS):
                    chunk_keys.append(key)
                    chunk_texts.append(ctext)
                    plan.append(("embed", ctext))
                    to_embed_texts.append(ctext)
                n_reembed_convos += 1

        print(f"  embeddings: {len(convo_msgs)} convos -> reuse {n_reused_convos}, "
              f"re-embed {n_reembed_convos} ({len(to_embed_texts)} new/changed "
              f"chunks, {n_reused_chunks} reused); embedding...")
        fresh = _embeddings.embed_texts(to_embed_texts)  # (M, d) or (0, 0)

        # Materialize the matrix by walking the plan once, drawing fresh rows in
        # order. Row i corresponds exactly to chunk_keys[i] / chunk_texts[i].
        if not plan:
            matrix = np.zeros((0, 0), dtype=np.float32)
        else:
            if n_reused_chunks:
                dim = int(np.asarray(plan[next(
                    i for i, e in enumerate(plan) if e[0] == "reuse")][1]).shape[0])
            elif fresh.ndim == 2 and fresh.shape[0] > 0:
                dim = int(fresh.shape[1])
            else:
                dim = 0
            matrix = np.empty((len(plan), dim), dtype=np.float32)
            fi = 0  # cursor into freshly-embedded rows
            for row, (kind, payload) in enumerate(plan):
                if kind == "reuse":
                    matrix[row] = np.asarray(payload, dtype=np.float32)
                else:
                    matrix[row] = fresh[fi]
                    fi += 1

        # Consistency assertion: matrix rows ↔ keys ↔ chunk texts, all parallel.
        assert matrix.shape[0] == len(chunk_keys) == len(chunk_texts), (
            f"index inconsistency: matrix {matrix.shape[0]} rows, "
            f"{len(chunk_keys)} keys, {len(chunk_texts)} chunks")

        # Snapshot any prior index before overwriting (out/ is gitignored, but a
        # rebuild shouldn't silently clobber a working index).
        for p in (npy_path, keys_path, chunks_path, EMBED_SIG_PATH):
            if os.path.exists(p):
                import shutil
                shutil.copy2(p, p + ".bak")
        np.save(npy_path, matrix)
        with open(keys_path, "w", encoding="utf-8") as f:
            json.dump(chunk_keys, f, ensure_ascii=False)
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump(chunk_texts, f, ensure_ascii=False)
        # Persist the signature map for ALL indexed convos so the next build can
        # reuse against it. (Only convos actually in the index are written.)
        indexed_sigs = {k: new_sigs[k] for k in set(chunk_keys) if k in new_sigs}
        with open(EMBED_SIG_PATH, "w", encoding="utf-8") as f:
            json.dump(indexed_sigs, f, ensure_ascii=False)

        # Keyword (FTS5) index over the SAME chunk texts, so literal/phrase
        # search stays congruent with what embeddings see. Best-effort: a build
        # must still succeed if FTS5 is somehow unavailable.
        try:
            import keyword_search as _ks
            fts_path = os.path.join(OUT, "fts.db")
            if os.path.exists(fts_path):
                shutil.copy2(fts_path, fts_path + ".bak")
            _ks.build_fts(chunk_keys, chunk_texts, fts_path)
            print(f"  keyword index: {len(chunk_keys)} chunks -> {fts_path}")
        except Exception as exc:  # noqa: BLE001 - never fail a build over FTS
            print(f"  ! keyword index skipped: {exc}")
        print(f"  embeddings: indexed {matrix.shape[0]} chunks "
              f"(dim {matrix.shape[1] if matrix.ndim == 2 else 0}) in "
              f"{_time.time() - t0:.1f}s -> {npy_path}")

    n_people = sum(1 for p in people if p["kind"] == "person")
    n_groups = sum(1 for p in people if p["kind"] == "group")
    print(f"\nDone. Kept {kept} ({n_people} people, {n_groups} groups).")
    print(f"Removed: {dropped} junk, {excluded} feedback, {empty} empty, "
          f"{spam} opt-out spam, {alert} alerts.")
    print(f"Auto spam/alert removals listed in {OUT}/filtered_out.json "
          f"(add a key to \"keep\" in data/exclude.json to rescue any).")
    print(f"Wrote {OUT}/people.json + {kept} conversation files.")

    # Small stats file the UI surfaces so it's clear what's shown vs filtered.
    # last_synced records when this build ran (local ISO-8601), so the UI can show
    # a "Last synced" label and the Refresh flow can report freshness.
    import datetime as _dt_stats
    stats = {"people": n_people, "groups": n_groups, "kept": kept,
             "last_synced": _dt_stats.datetime.now().isoformat(timespec="seconds"),
             "filtered": {"total": dropped + excluded + empty + spam + alert,
                          "junk": dropped, "feedback": excluded, "empty": empty,
                          "opt_out_spam": spam, "alerts": alert}}
    json.dump(stats, open(os.path.join(OUT, "stats.json"), "w"), indent=2)


if __name__ == "__main__":
    main()
