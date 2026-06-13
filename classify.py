#!/usr/bin/env python3
"""Local conversation classifier using a small MLX model (Qwen3-4B).

Assigns each conversation one of three categories the user actually cares about:
Family / Service / Personal. Runs fully on-device, free, in a few minutes.

  .venv/bin/python classify.py --eval        # accuracy vs the 130 hand-labels
  .venv/bin/python classify.py               # classify all, write enrichment
  .venv/bin/python classify.py --filter "plumbers"   # custom yes/no filter

Custom filters (--filter) ask the model a yes/no membership question per
conversation and store matches as a tag, so the UI can offer a chip for it.
"""
import json, os, sys, time, glob, re
from mlx_lm import load, generate

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.environ.get("CRM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
ENRICH = os.path.join(HERE, "data/enrich_parts/local.json")
DIGESTS = os.path.join(HERE, "out/digests.json")
VCF = os.path.join(HERE, "data/contacts.vcf")

# Family is computed structurally in build.py (from family group chats), which is
# far more precise than letting the model guess from surnames. The model's job
# here is the genuinely semantic split it's good at: Service vs Personal.
CATS = ["Service", "Personal"]


def user_identity():
    """Owner of the phone. Priority: env CRM_USER > config.json > vCard first card."""
    name = os.environ.get("CRM_USER", "")
    cfg = os.path.join(HERE, "config.json")
    if not name and os.path.exists(cfg):
        try:
            name = (json.load(open(cfg)).get("user_name") or "").strip()
        except Exception:
            pass
    if not name and os.path.exists(VCF):
        with open(VCF, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("FN:"):
                    name = line[3:].strip()
                    break
    parts = name.split()
    return name or "the user", (parts[-1] if parts else "")


USER_NAME, SURNAME = user_identity()

SYSTEM = (
    "You label a contact from a text-message summary as exactly ONE of: "
    "Service, Personal.\n"
    "- Service: a vendor or transactional contact — contractor, tradesperson "
    "(plumber, roofer, electrician), coach/instructor, delivery, salesperson, "
    "marketplace buyer/seller, appointment/booking desk. The thread is about a "
    "job, quote, scheduling, or a one-off transaction, not personal life.\n"
    "Label Service when the relationship exists BECAUSE of a job, service, or "
    "transaction (you hired them, they sell to you, they coach/instruct for pay, "
    "they deliver/install), even if there are many messages or it's friendly.\n"
    "- Personal: a friend, family member, colleague, or acquaintance you relate "
    "to as a person, not for a paid service.\n"
    f"The user is {USER_NAME}. Reply with ONLY the one category word."
)


def digest_text(d):
    lines = []
    for m in d["sample"]:
        who = "ME" if m["me"] else (m["from"][:12] if d["kind"] == "group" else "THEM")
        lines.append(f"  {who}: {(m['text'] or '')[:90]}")
    mem = (" members: " + ", ".join(d["members"][:6])) if d["kind"] == "group" else ""
    return (f"name: {d['name']} (in_contacts: {'yes' if d['in_contacts'] else 'no'}, "
            f"{'group' if d['kind']=='group' else '1:1'}){mem}\n"
            f"sent_by_me: {d['sent']}, received: {d['recv']}\n"
            f"messages:\n" + "\n".join(lines))


def parse_one(out, options):
    o = out.strip().lower()
    for c in options:
        if c.lower() in o:
            return c
    return options[-1]


def classify(model, tok, digests, prompt_fn, options, label="classify"):
    out = {}
    t0 = time.time()
    for i, d in enumerate(digests):
        prompt = tok.apply_chat_template(
            prompt_fn(d), add_generation_prompt=True, tokenize=False)
        res = generate(model, tok, prompt=prompt, max_tokens=6, verbose=False)
        out[d["key"]] = parse_one(res, options)
        if (i + 1) % 100 == 0:
            print(f"  {label} {i+1}/{len(digests)} ({time.time()-t0:.0f}s)")
    print(f"  {label} done in {time.time()-t0:.0f}s ({(time.time()-t0)/len(digests)*1000:.0f} ms/convo)")
    return out


def cat_prompt(d):
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": digest_text(d) + "\n\nCategory:"}]


def strict_filter_prompt(label):
    """Shared strict yes/no system prompt — biased toward 'no' for precision."""
    return (
        f"You filter contacts. Target label: \"{label}\".\n"
        "Look at the messages. Answer \"yes\" ONLY if the messages clearly show "
        f"this contact genuinely matches \"{label}\" (e.g. they provide/do it, or "
        "the thread is plainly about that). Answer \"no\" for friends, family, "
        "colleagues, and anyone whose messages don't clearly show a match. When "
        "in doubt, answer \"no\".")


def search_prompt(query):
    """Relevance prompt for free-text SEARCH (not contact-type filtering).

    strict_filter_prompt is built for "is this contact a contractor" and is biased
    to "no" — wrong for a topical lookup like "restaurants in the bay area", where
    it would ask "is this contact restaurants" and reject everyone. This judges
    TOPICAL relevance by INTENT and favors recall: when you're looking something
    up, surfacing a plausibly-relevant person beats missing them.
    """
    return (
        f'A user is SEARCHING their text-message history for: "{query}".\n'
        "From the conversation summary, answer \"yes\" if this person is RELEVANT "
        "to that search — the conversation touches the topic, the person could give "
        "recommendations or help with it, or it is about them. Read the query by "
        "INTENT, not literal wording: e.g. \"restaurants in the bay area\" means "
        "people you've discussed dining or places to eat with, or who would have "
        "recommendations. Answer \"no\" only if the conversation is clearly "
        "unrelated; when it is a plausible match, answer \"yes\".")


def load_model():
    return load(MODEL)


def batch_yesno(model, tok, digests, label, progress=None, batch=64, system=None):
    """Batched yes/no membership over all digests. Returns matching keys.

    `progress(done, total)` is called after each batch for live UIs. `system`
    overrides the default strict contact-filter prompt — search passes
    search_prompt(query) for recall-favoring topical relevance instead.
    """
    from mlx_lm import batch_generate
    sysmsg = system or strict_filter_prompt(label)
    keys = []
    for i in range(0, len(digests), batch):
        chunk = digests[i:i + batch]
        prompts = [tok.apply_chat_template(
            [{"role": "system", "content": sysmsg},
             {"role": "user", "content": digest_text(d) + "\n\nAnswer:"}],
            add_generation_prompt=True) for d in chunk]
        res = batch_generate(model, tok, prompts, max_tokens=4, verbose=False)
        texts = res.texts if hasattr(res, "texts") else res
        for d, t in zip(chunk, texts):
            if str(t).strip().lower().startswith("y"):
                keys.append(d["key"])
        if progress:
            progress(min(i + batch, len(digests)), len(digests))
    return keys


# --------------------------------------------------------------------------- #
# Work-vs-Personal judgment (grows Work out of the Personal catch-all)
#
# Applied ONLY to people the deterministic partition left as Personal. The model
# reads the conversation tone and answers work / personal / unsure. This is
# DELIBERATELY biased toward `personal`: the prior failure mode was "everything
# looks like work", so the prompt demands CLEAR professional signal for `work`
# and treats any real social content as `personal`. Genuine ambiguity -> `unsure`
# (the build then leans on the in_contacts prior + sets the unsure flag).
WORK_PERSONAL_SYSTEM = (
    "You decide whether a text-message contact is primarily a WORK/professional "
    "relationship or a PERSONAL/social one, from a short conversation summary. "
    "Answer with EXACTLY one lowercase word: work, personal, or unsure.\n"
    "\n"
    "Judge by what the conversation is mostly ABOUT, not by how friendly the tone "
    "is. Professional people are often warm and casual — a friendly tone does NOT "
    "make a contact personal if the substance is business.\n"
    "\n"
    "- work: the relationship runs on business/professional substance. Signals: "
    "fundraising, investors, board meetings/BOD, runway, deals, cap table, "
    "startups, products, launches, PR/press, hiring/recruiting, candidates, "
    "clients, customers, meetings/calls about work, projects, design/eng/marketing "
    "feedback, contracts/invoices, 'pick your brain', 'talk shop', 'jump on a "
    "call', intros for business, a company or role as the reason you know them. "
    "Colleagues, coworkers, clients, founders, investors, recruiters, advisors, "
    "and business contacts are WORK even when the messages are friendly, include "
    "'happy birthday', or mention family in passing.\n"
    "- personal: the relationship runs on social/personal substance — a friend, "
    "family member, partner, or neighbor. Signals: making social plans (dinner, "
    "drinks, hanging out, parties), affection, family/relationship/kids/pets talk, "
    "jokes and pure chit-chat, sports or hobbies you do together, with NO business "
    "thread underneath. If the messages are purely social with no professional "
    "substance, answer personal.\n"
    "- unsure: genuinely too little or too mixed to tell — terse logistics "
    "('ok', 'thanks', 'see you', an address) with NO clear social OR professional "
    "marker either way.\n"
    "\n"
    "Scheduling alone is NOT a work signal: 'what time works?', 'free now?', 'on "
    "my way', a zoom/call link, or meeting logistics could be either — decide from "
    "the SURROUNDING substance (is the meeting about business or about catching "
    "up?). If it is only coordination with no business topic and no clear social "
    "topic, answer unsure.\n"
    "\n"
    f"The user is {USER_NAME}, a startup founder/investor. Weigh the SUBSTANCE: "
    "business substance -> work; purely social substance -> personal; neither -> "
    "unsure. Do NOT default to personal just because the contact is saved or the "
    "tone is friendly. Do NOT label something work just because it is brief or "
    "from an unknown number. Reply with ONLY one word: work, personal, or unsure.")


def _parse_work_personal(text):
    """Map a raw model reply to work / personal / unsure (defaulting to unsure)."""
    t = str(text).strip().lower()
    # Take the first alpha token so trailing punctuation/words don't confuse us.
    m = re.search(r"[a-z]+", t)
    head = m.group(0) if m else ""
    if head.startswith("work"):
        return "work"
    if head.startswith("person"):  # personal
        return "personal"
    return "unsure"


def judge_work_personal(model, tok, digests, progress=None, batch=64):
    """Batched work/personal/unsure verdict over `digests`.

    Returns {key: 'work'|'personal'|'unsure'}. Reuses the shared model load and
    batch_generate plumbing for speed over a few hundred people.
    """
    from mlx_lm import batch_generate
    verdicts = {}
    for i in range(0, len(digests), batch):
        chunk = digests[i:i + batch]
        prompts = [tok.apply_chat_template(
            [{"role": "system", "content": WORK_PERSONAL_SYSTEM},
             {"role": "user", "content": digest_text(d) + "\n\nAnswer:"}],
            add_generation_prompt=True) for d in chunk]
        res = batch_generate(model, tok, prompts, max_tokens=4, verbose=False)
        texts = res.texts if hasattr(res, "texts") else res
        for d, t in zip(chunk, texts):
            verdicts[d["key"]] = _parse_work_personal(t)
        if progress:
            progress(min(i + batch, len(digests)), len(digests))
    return verdicts


# --------------------------------------------------------------------------- #
# Family judgment (grows Family beyond the kinship-group heuristic)
#
# compute_family() in build.py only catches relatives who appear in a Mom/Dad-
# anchored GROUP chat — it misses 1:1 family (spouse, parents, siblings, kids,
# in-laws). This model pass reads a 1:1 conversation and answers family /
# not_family. It is DELIBERATELY conservative: the failure mode to avoid is
# labeling close (even affectionate) FRIENDS as family. Only clear relative
# signals -> family; everything else -> not_family.
FAMILY_SYSTEM = (
    f"You decide whether a text-message contact is {USER_NAME}'s OWN FAMILY — an "
    f"actual relative of {USER_NAME} by blood or marriage — from a short "
    "conversation summary. Answer EXACTLY one lowercase word: family or "
    "not_family.\n"
    "\n"
    "Default to not_family. Be VERY STRICT: the large majority of contacts are "
    "NOT family. Only answer `family` with STRONG, EXPLICIT evidence that THIS "
    f"contact is {USER_NAME}'s relative.\n"
    "\n"
    "- family: this contact is the user's spouse/husband/wife, parent (their own "
    "mom/dad), child (their son/daughter), sibling (their brother/sister), "
    "grandparent, aunt/uncle, niece/nephew, cousin, or in-law. STRONG evidence: "
    "the contact signs off AS the user's relative ('- Mom', '- Dad', 'love, your "
    "brother'); the user calls THEM mom/dad/sis directly; talk of OUR shared "
    "parents/home/household, 'our mom/dad', co-parenting the user's own kids, a "
    "shared last name PLUS family talk, eldercare/inheritance for a shared "
    "relative.\n"
    "- not_family: EVERYONE else — friends, close friends, neighbors, colleagues, "
    "clients, vendors, acquaintances, and the parents of the user's kids' friends "
    "or classmates. Answer not_family in ALL of these cases, which LOOK like "
    "family but are NOT:\n"
    "  * SCHOOL/ACTIVITY PARENTS: anyone described as someone-else's parent — "
    "\"Riley's Dad\", \"Jamie's Mom\", \"Casey's Dad\", \"Sam's Dad\", or a "
    "carpool/playdate/classmate coordinator. These are OTHER children's parents, "
    "NOT the user's family. not_family.\n"
    "  * CASUAL kinship words between friends: 'brother', 'bro', 'bud', 'buddy', "
    "'sis', 'fam', 'cousin' used as friendly slang, NOT as a literal relative. "
    "not_family.\n"
    "  * A relative of SOMEONE ELSE ('a friend of Jay's', 'your mom found "
    "someone', a third person's mom/dad/uncle). not_family.\n"
    "  * Strangers, marketplace/Sulekha/classified contacts, and one-off "
    "logistics, even if a family word appears. not_family.\n"
    "Affection, birthday wishes, warmth, 'love you', and dinner plans are NOT "
    "family by themselves — friends say these too.\n"
    "\n"
    f"The user is {USER_NAME}. A family word in the messages counts ONLY if it "
    f"refers to {USER_NAME}'s OWN relative — not someone else's parent, not casual "
    "slang. When in doubt, answer not_family — it is far better to miss a relative "
    "than to mislabel a friend or a classmate's parent as family. Reply with ONLY "
    "one word: family or not_family.")


def _parse_family(text):
    """Map a raw model reply to family / not_family (defaulting to not_family)."""
    t = str(text).strip().lower()
    # 'not_family' / 'not family' / 'no' all mean not family. A bare 'family'
    # (not preceded by 'not') is the only positive. Check the negative first.
    if t.startswith("not") or t.startswith("no"):
        return "not_family"
    m = re.search(r"[a-z]+", t)
    head = m.group(0) if m else ""
    if head == "family":
        return "family"
    return "not_family"


def judge_family(model, tok, digests, progress=None, batch=64):
    """Batched family / not_family verdict over `digests` (1:1 people).

    Returns {key: 'family'|'not_family'}. Conservative by design — see
    FAMILY_SYSTEM. Reuses the shared model load + batch_generate plumbing.
    """
    from mlx_lm import batch_generate
    verdicts = {}
    for i in range(0, len(digests), batch):
        chunk = digests[i:i + batch]
        prompts = [tok.apply_chat_template(
            [{"role": "system", "content": FAMILY_SYSTEM},
             {"role": "user", "content": digest_text(d) + "\n\nAnswer:"}],
            add_generation_prompt=True) for d in chunk]
        res = batch_generate(model, tok, prompts, max_tokens=4, verbose=False)
        texts = res.texts if hasattr(res, "texts") else res
        for d, t in zip(chunk, texts):
            verdicts[d["key"]] = _parse_family(t)
        if progress:
            progress(min(i + batch, len(digests)), len(digests))
    return verdicts


def gen_keywords(model, tok, label):
    """Ask the model for strong keywords that would appear in messages with such
    a contact — used to pre-select candidates so we don't run the full store."""
    msg = (
        f"A user wants to find contacts that are: {label}.\n"
        "List 12 short, STRONG lowercase keywords or phrases that would actually "
        "appear in text messages with such a contact (job titles, services, tools, "
        "typical phrases). Avoid generic words like 'hi' or 'thanks'. "
        "Reply as a comma-separated list only.")
    prompt = tok.apply_chat_template(
        [{"role": "user", "content": msg}], add_generation_prompt=True, tokenize=False)
    out = generate(model, tok, prompt=prompt, max_tokens=120, verbose=False)
    kws = []
    for k in re.split(r"[,\n]", out):
        k = k.strip().lower().strip(".\"'`-•* ")  # drop punctuation/bullets
        if 2 <= len(k) <= 30:
            kws.append(k)
    return kws[:20]


def _blob(d):
    parts = [d["name"]] + d.get("members", []) + [m.get("text") or "" for m in d["sample"]]
    return " ".join(parts).lower()


def _kw_regex(keywords):
    # Match each keyword on word boundaries so short ones (dr, rx) don't hit
    # inside other words (drive, address). Phrases match as-is.
    if not keywords:
        return None
    return re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b")


def smart_filter(model, tok, digests, label, progress=None, candidate_fn=None):
    """Candidate generation, then strict model confirm on candidates only.

    Returns (keys, keywords, n_candidates). Falls back to a full scan if the
    candidate generator matches too few conversations (avoids missing a fuzzy
    filter).

    `candidate_fn(label, digests) -> list[digest] | None` is an optional
    retrieval hook. When supplied and it returns a non-trivial candidate set,
    that set is used INSTEAD of the keyword pre-filter (this is how server.py
    plugs in the embedding-index top-k so category creation is also fast). A
    `None`/too-small return falls through to the existing keyword path, so the
    CLI and any index-less caller behave exactly as before.
    """
    # Nuanced / negation descriptions need to consider everyone — keyword
    # pre-filtering would miss people whose messages don't contain the keyword.
    complex_desc = len(label.split()) > 12 or re.search(
        r"\b(not|no|exclude|remove|except|without|avoid)\b", label.lower())

    keywords = []
    cands = None
    if candidate_fn is not None and not complex_desc:
        try:
            cands = candidate_fn(label, digests)
        except Exception:
            cands = None
        if cands is not None and len(cands) < 5:
            cands = None  # retrieval too thin — fall back to keywords

    if cands is None:
        if complex_desc:
            cands = digests
        else:
            keywords = gen_keywords(model, tok, label)
            rx = _kw_regex(keywords)
            cands = [d for d in digests if rx.search(_blob(d))] if rx else digests
            if len(cands) < 5:
                cands = digests  # keywords too narrow — scan everything
    keys = batch_yesno(model, tok, cands, label, progress=progress)
    return keys, keywords, len(cands)


ROUTER_SYS = (
    "You configure a contact filter from a description. Choose the CHEAPEST type "
    "that works:\n"
    "- computed: answerable from STRUCTURAL FIELDS ALONE, with NO judgment about "
    "who the person is or what the conversation is about — kind('person'/'group'), "
    "in_contacts, msg_count, days_since_last, sent, recv, has_birthday. Use ONLY "
    "for activity/volume/recency/structure that the description EXPLICITLY asks for "
    "(e.g. \"haven't talked in a while\", \"most texted\", \"group chats\", "
    "\"people with a birthday\").\n"
    "- keyword: answerable by specific words/phrases that appear in messages — a "
    "clear vocabulary (e.g. marketplace: \"still available\", \"obo\", \"zelle\").\n"
    "- semantic: needs judgment about WHO a person IS, WHAT a conversation is "
    "ABOUT, a person's IDENTITY, or their RELATIONSHIP TO SOMEONE — e.g. "
    "'work/tech professionals', 'not school-related', 'not contractors', "
    "'friends of my son', \"my daughter's school parents\", 'people I play tennis "
    "with', 'sounds like haggling'. ANY description that describes a kind of "
    "person, a social/family relationship, or a topic is semantic.\n"
    "IMPORTANT: if the description needs ANY such judgment, choose semantic — even "
    "if it ALSO mentions activity (recency/volume). Use computed ONLY when the "
    "WHOLE description is pure activity/structure (counts, dates, contact-status, "
    "person/group, category Family/Service/Personal). A description of an identity "
    "or relationship (\"friends of X\", \"my kid's friends\", \"people from "
    "church\") is NEVER computed — it is semantic, because no field encodes it. "
    "in_contacts/two_way/counts/recency are SHAPE flags, not a definition of who "
    "someone is — never use them to stand in for an identity/relationship "
    "description.\n\n"
    "Reply with ONLY JSON, one of:\n"
    '{"type":"computed","rule":{...},"sort":"recent|catchup|most|least"}\n'
    '{"type":"keyword","keywords":["...","..."]}\n'
    '{"type":"semantic"}\n\n'
    "Rule keys (all optional, ANDed): kind, in_contacts(true), has_birthday(true), "
    "min_count(int), max_count(int), two_way(true), min_days_since(int), "
    "max_days_since(int).\n"
    "To EXCLUDE: not_kind('person'/'group'), not_in_contacts(true), and "
    "not_category('Family'/'Service'/'Personal') — use not_category for "
    "\"exclude/ignore family\" or \"not vendors\".\n"
    "Include ONLY conditions the description clearly implies — never add unrelated "
    "ones (no has_birthday unless birthdays are mentioned).\n"
    "Examples:\n"
    '"people I was close to but haven\'t talked to in months" -> '
    '{"type":"computed","rule":{"two_way":true,"min_count":10,"min_days_since":90},"sort":"catchup"}\n'
    '"catch up, but exclude family" -> '
    '{"type":"computed","rule":{"two_way":true,"min_days_since":90,"not_category":"Family"},"sort":"catchup"}')


_RULE_KEYS = {"kind", "in_contacts", "category", "has_birthday", "has_suggestion",
              "min_count", "max_count", "two_way", "min_days_since", "max_days_since",
              "not_kind", "not_in_contacts", "not_category"}
_SORTS = {"recent", "catchup", "most", "least", "birthday", "confidence"}


def _has_discriminator(r):
    """A computed rule actually DEFINES a category/filter only if it carries at
    least one real DISCRIMINATOR — a key that picks people out by WHAT they are,
    not merely the SHAPE of the relationship.

    Discriminators: category / not_category, a group-chat structural filter
    (kind/not_kind == 'group'), has_birthday, has_suggestion, or keyword-type.

    Generic relationship-SHAPE flags alone — in_contacts, not_in_contacts,
    two_way, min/max_count, min/max_days_since — do NOT constitute a category
    definition (they'd match hundreds of people indiscriminately). A rule with
    only shape-flags is a routing miss and must fall through to semantic, where
    the model reads each conversation.
    """
    if r.get("category") or r.get("not_category"):
        return True
    if r.get("kind") == "group" or r.get("not_kind") == "group":
        return True
    if r.get("has_birthday") or r.get("has_suggestion"):
        return True
    return False


def sanitize_route(d, description):
    """The model proposes; we validate. Drops hallucinated/contradictory
    conditions so the user never has to see or fix a rule. Invisible hardening."""
    desc = (description or "").lower()
    if d.get("type") == "computed":
        r = {k: v for k, v in (d.get("rule") or {}).items() if k in _RULE_KEYS}
        # Gate the flags the model most often invents to actual mentions.
        if r.get("has_birthday") and not re.search(r"birthday|bday|b-day", desc):
            r.pop("has_birthday", None)
        if r.get("has_suggestion") and "name" not in desc:
            r.pop("has_suggestion", None)
        # Meaningless / contradictory values.
        if r.get("two_way") is False:
            r.pop("two_way", None)
        if r.get("in_contacts") is False:
            r.pop("in_contacts", None)
        if r.get("in_contacts") and re.search(
                r"stranger|unknown|don.?t know|never (talk|repl|met)|spam|cold", desc):
            r.pop("in_contacts", None)
        if r.get("kind") not in ("person", "group"):
            r.pop("kind", None)
        if r.get("not_kind") not in ("person", "group"):
            r.pop("not_kind", None)
        if r.get("category") not in ("Family", "Service", "Personal"):
            r.pop("category", None)
        if r.get("not_category") not in ("Family", "Service", "Personal"):
            r.pop("not_category", None)
        if r.get("not_in_contacts") is not True:
            r.pop("not_in_contacts", None)
        for k in ("min_count", "max_count", "min_days_since", "max_days_since"):
            if k in r:
                try:
                    r[k] = max(0, int(r[k]))
                except Exception:
                    r.pop(k, None)
        d["rule"] = r
        if d.get("sort") not in _SORTS:
            d.pop("sort", None)
        # An empty computed rule matches everyone — treat as a routing miss.
        if not r:
            return {"type": "semantic"}
        # A computed rule with ONLY relationship-shape flags (in_contacts,
        # two_way, counts, recency) and no real discriminator isn't a category
        # definition — it would match hundreds of people. This is the classic
        # mis-route of an identity/relationship description ("friends of X")
        # into {kind:person, in_contacts:true, two_way:true}. Discard the rule
        # and let the model read each conversation.
        if not _has_discriminator(r):
            return {"type": "semantic"}
    elif d.get("type") == "keyword":
        kws = [k.strip().lower() for k in (d.get("keywords") or [])
               if isinstance(k, str) and 2 <= len(k.strip()) <= 30]
        if not kws:
            return {"type": "semantic"}
        d["keywords"] = kws[:20]
    return d


def route_filter(model, tok, description):
    """Decide how a described filter should be evaluated. Returns a dict with
    'type' (computed|keyword|semantic) and the relevant params, sanitized."""
    prompt = tok.apply_chat_template(
        [{"role": "system", "content": ROUTER_SYS},
         {"role": "user", "content": f'Description: "{description}"\nJSON:'}],
        add_generation_prompt=True, tokenize=False)
    out = generate(model, tok, prompt=prompt, max_tokens=160, verbose=False)
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("type") in ("computed", "keyword", "semantic"):
                return sanitize_route(d, description)
        except Exception:
            pass
    return {"type": "semantic"}


def save_filter(name, description, keys, fid=None):
    slug = fid or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    path = os.path.join(HERE, f"data/enrich_parts/filter_{slug}.json")
    json.dump({"_filter": name, "description": description, "keys": keys},
              open(path, "w"), indent=0)
    return path


def main():
    digests = json.load(open(DIGESTS))
    args = sys.argv[1:]

    if "--filter" in args:
        label = args[args.index("--filter") + 1]
        # Optional short display name for the chip; defaults to the label.
        name = args[args.index("--name") + 1] if "--name" in args else label
        print(f"Custom filter \"{name}\" over {len(digests)} convos\nModel: {MODEL}")
        model, tok = load(MODEL)
        keys, keywords, ncand = smart_filter(model, tok, digests, label)
        print(f"keywords: {keywords}\ncandidates after pre-filter: {ncand}")
        path = save_filter(name, label, keys)
        print(f"\n{len(keys)} match \"{name}\" -> {path}")
        return

    if "--workpersonal" in args:
        # Judge work/personal/unsure for the people the partition left Personal.
        # build.py reads out/people.json for the current Personal set; here we
        # judge ALL 1:1 people and cache, so build can look up any key.
        targets = [d for d in digests if d["kind"] == "person"]
        print(f"Work/personal judgment over {len(targets)} people\nModel: {MODEL}")
        model, tok = load(MODEL)
        verdicts = judge_work_personal(
            model, tok, targets,
            progress=lambda done, total: print(f"  {done}/{total}"))
        from collections import Counter
        print("\n" + ", ".join(f"{v}:{n}" for v, n in Counter(verdicts.values()).most_common()))
        path = os.path.join(HERE, "data/enrich_parts/work_personal.json")
        json.dump(verdicts, open(path, "w"), ensure_ascii=False, indent=0)
        print(f"Wrote {path}")
        return

    if "--names" in args:
        # Infer full names for non-contacts (shown as numbers) AND contacts we
        # only have a first name for (suggest the surname).
        from mlx_lm import batch_generate

        def needs_name(d):
            if d["kind"] != "person":
                return False
            if not d["in_contacts"]:
                return True
            toks = d["name"].split()
            return len(toks) == 1 and toks[0][:1].isalpha()

        targets = [d for d in digests if needs_name(d)]
        print(f"Inferring names for {len(targets)} contacts\nModel: {MODEL}")
        model, tok = load(MODEL)
        ufirst = USER_NAME.split()[0] if USER_NAME else ""
        sysmsg = (
            "From the messages, what is THIS CONTACT's real name (the other party, "
            f"not the user)? The user is {USER_NAME} — NEVER answer with the user's "
            f"own name ({ufirst}); we want the other person's name. Only use a name "
            "that clearly appears — they signed it, were addressed by it, or it's in "
            "an email/signature. Prefer FIRST and LAST name; give just the part you "
            "can support. Do not return a company, website, or phone number. "
            "If no personal name is evident, reply UNKNOWN. "
            "Reply with ONLY the name or UNKNOWN.")
        names = {}
        for i in range(0, len(targets), 64):
            chunk = targets[i:i + 64]
            prompts = [tok.apply_chat_template(
                [{"role": "system", "content": sysmsg},
                 {"role": "user", "content": digest_text(d) + "\n\nName:"}],
                add_generation_prompt=True) for d in chunk]
            res = batch_generate(model, tok, prompts, max_tokens=10, verbose=False)
            texts = res.texts if hasattr(res, "texts") else res
            for d, t in zip(chunk, texts):
                nm = str(t).strip().strip(".\"'").split("\n")[0].strip()
                if not nm or nm.upper().startswith("UNKNOWN") or len(nm) > 40:
                    continue
                low = nm.lower()
                # Reject the user's own name, websites, transcription noise, digits.
                if low in (USER_NAME.lower(), ufirst.lower()):
                    continue
                if any(x in low for x in (".com", ".net", "http", "www", "(?)", "@")):
                    continue
                if any(c.isdigit() for c in nm):
                    continue
                cur = d["name"].strip()
                # Skip if it adds nothing (same as the existing first name).
                if low == cur.lower():
                    continue
                ntoks = nm.split()
                if d["in_contacts"] and len(ntoks) < 2:
                    continue  # for named contacts only suggest a fuller name
                conf = "high" if len(ntoks) >= 2 else "low"
                names[d["key"]] = {"suggested_name": nm, "name_confidence": conf,
                                   "name_reason": "inferred from conversation"}
            print(f"  names {min(i+64,len(targets))}/{len(targets)}")
        path = os.path.join(HERE, "data/enrich_parts/names.json")
        json.dump(names, open(path, "w"), ensure_ascii=False, indent=0)
        print(f"\nSuggested {len(names)} names -> {path}")
        return

    if "--eval" in args:
        labels = {}
        for p in glob.glob(os.path.join(HERE, "data/enrich_parts/part1.json")):
            labels.update(json.load(open(p)))
        dmap = {d["key"]: d for d in digests}
        # The model only handles Service vs Personal; Family is structural, so
        # collapse my Family labels into Personal for this eval.
        items = [(k, v["category"]) for k, v in labels.items()
                 if k in dmap and v.get("category")]
        coll = lambda c: "Service" if c == "Service" else "Personal"
        print(f"Eval Service-vs-Personal on {len(items)} labeled convos\nModel: {MODEL}")
        model, tok = load(MODEL)
        sub = [dmap[k] for k, _ in items]
        res = classify(model, tok, sub, cat_prompt, CATS, label="eval")
        ok = sum(1 for k, tr in items if res[k] == coll(tr))
        from collections import Counter, defaultdict
        conf = defaultdict(Counter)
        for k, tr in items:
            conf[coll(tr)][res[k]] += 1
        print(f"\nService-vs-Personal accuracy: {ok}/{len(items)} = {ok/len(items)*100:.1f}%")
        for t in CATS:
            if conf[t]:
                print(f"  truth {t:9}: " + ", ".join(f"{p}:{c}" for p, c in conf[t].most_common()))
        return

    # Full run: classify everything, write enrichment.
    print(f"Classifying {len(digests)} convos\nModel: {MODEL}  (user: {USER_NAME})")
    model, tok = load(MODEL)
    res = classify(model, tok, digests, cat_prompt, CATS, label="all")
    enrichment = {k: {"category": v} for k, v in res.items()}
    json.dump(enrichment, open(ENRICH, "w"), ensure_ascii=False, indent=0)
    from collections import Counter
    print("\n" + ", ".join(f"{c}:{n}" for c, n in Counter(res.values()).most_common()))
    print(f"Wrote {ENRICH}")


if __name__ == "__main__":
    main()
