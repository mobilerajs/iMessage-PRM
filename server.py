#!/usr/bin/env python3
"""Local iMessage CRM server.

Serves the web UI and exposes a small API backed by the on-device model, so you
can create filters live (and, soon, tune precision and edit names) without the
CLI. The model is loaded once and kept warm, so each filter is just inference.

  .venv/bin/python server.py            # http://localhost:8001

This is the seam for packaging the app as something installable: one process
that owns the model, the data, and the UI.
"""
import datetime, json, os, re, subprocess, sys, threading, time, uuid
from flask import Flask, request, jsonify, send_from_directory

import classify

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8001"))
FILTERS_DATA = os.path.join(HERE, "data/filters.json")
FILTERS_OUT = os.path.join(HERE, "out/filters.json")
USERSTATE = os.path.join(HERE, "data/userstate.json")
EMBED_NPY = os.path.join(HERE, "out/embeddings.npy")
EMBED_KEYS = os.path.join(HERE, "out/embedding_keys.json")

# How many candidates the embedding index hands to the LLM confirm step. This is
# the whole point of the hybrid: retrieval narrows ~1000 -> SEARCH_TOPK instantly,
# the model confirms only those (not the full store).
SEARCH_TOPK = 25

# ---- category prior (soft boost) -------------------------------------------
# Strength of the category nudge: boosted = raw_cosine + PRIOR_ALPHA * affinity.
# Kept MODEST on purpose. Cosine spread among real candidates is far wider than
# 0.2 * affinity (affinity itself is ~[-1,1] but realistically small), so the
# prior only re-orders near-ties — a strong cross-category match still wins. This
# is the "soft, not a filter" knob; raise it to bias harder toward the hint.
PRIOR_ALPHA = 0.2
# Wide raw-cosine net we re-rank before taking the top SEARCH_TOPK. Starting from
# a net this much larger than SEARCH_TOPK is what keeps the prior soft: a strong
# cross-category match makes the wide cut on raw cosine, then survives the modest
# boost re-rank. (If we only re-ranked the narrow top-25 we'd risk dropping it.)
SEARCH_WIDE_N = max(60, 2 * SEARCH_TOPK)


def _load_filters():
    try:
        return json.load(open(FILTERS_DATA, encoding="utf-8"))
    except Exception:
        return []


def _save_filters(filters):
    json.dump(filters, open(FILTERS_DATA, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    json.dump(filters, open(FILTERS_OUT, "w", encoding="utf-8"), ensure_ascii=False)


def _days_since(iso):
    try:
        return (datetime.datetime.now() - datetime.datetime.fromisoformat(iso)).days
    except Exception:
        return 0


def _passes(p, f):
    if p.get("key") in (f.get("exclude_keys") or []):
        return False
    if p.get("key") in (f.get("include_keys") or []):
        return True
    if f.get("type") == "computed":
        r = f.get("rule", {})
        if r.get("kind") and p.get("kind") != r["kind"]: return False
        if r.get("not_kind") and p.get("kind") == r["not_kind"]: return False
        if r.get("in_contacts") and not p.get("in_contacts"): return False
        if r.get("not_in_contacts") and p.get("in_contacts"): return False
        if r.get("category") and p.get("category") != r["category"]: return False
        if r.get("not_category") and p.get("category") == r["not_category"]: return False
        if r.get("has_birthday") and not p.get("birthday"): return False
        if r.get("has_suggestion") and not p.get("suggested_name"): return False
        if r.get("min_count") is not None and p.get("count", 0) < r["min_count"]: return False
        if r.get("max_count") is not None and p.get("count", 0) > r["max_count"]: return False
        if r.get("two_way") and not (p.get("sent", 0) > 0 and p.get("recv", 0) > 0): return False
        if r.get("min_days_since") is not None and _days_since(p.get("last_date", "")) < r["min_days_since"]: return False
        if r.get("max_days_since") is not None and _days_since(p.get("last_date", "")) > r["max_days_since"]: return False
        return True
    if f.get("type") == "keyword":
        return any(k.lower() in (p.get("blob") or "") for k in f.get("keywords", []))
    if f.get("type") == "semantic":
        return f.get("name") in (p.get("tags") or [])
    if f.get("type") == "category":
        # User-added category: membership is the model-computed key set carried
        # on the object itself (so the frontend can read it straight from
        # out/filters.json without consulting people tags).
        return p.get("key") in (f.get("keys") or [])
    return False


def add_filter_def(fdef):
    filters = [f for f in _load_filters() if f.get("id") != fdef["id"]]
    filters.append(fdef)
    _save_filters(filters)
    try:
        people = json.load(open(os.path.join(HERE, "out/people.json"), encoding="utf-8"))
        return sum(1 for p in people if _passes(p, fdef))
    except Exception:
        return None


def sanity_warning(n):
    """Quietly flag a filter that matched nothing or almost everything — usually
    a sign the description was too vague — without exposing any internals."""
    try:
        total = len(json.load(open(os.path.join(HERE, "out/people.json"), encoding="utf-8")))
    except Exception:
        return None
    if n == 0:
        return "That filter matched nobody — try describing it differently."
    if total and n >= 0.9 * total:
        return "That filter matched almost everyone — try making it more specific."
    return None

app = Flask(__name__, static_folder=None)

STATE = {"model": None, "tok": None, "loading": True, "error": None}
JOBS = {}  # job_id -> {state, done, total, message, result}
FILTER_JOB = {}  # filter id -> latest job_id; lets a new run supersede an in-flight one


def _superseded(fid, job_id):
    """True if a newer model job has started for this filter — if so the caller
    should drop its (now stale) result instead of writing it."""
    return FILTER_JOB.get(fid) != job_id


def _load_model():
    try:
        STATE["model"], STATE["tok"] = classify.load_model()
    except Exception as exc:  # surfaced via /api/status
        STATE["error"] = str(exc)
    finally:
        STATE["loading"] = False


threading.Thread(target=_load_model, daemon=True).start()


# ---- embedding index (hybrid retrieval) ------------------------------------
# Loaded lazily on first search and cached: the (N, d) matrix, its parallel key
# list, and the warm embedder. All local/offline. Rebuilt by build.py.
EMBED = {"matrix": None, "keys": None, "loaded": False, "error": None,
         # Category prior: cached alongside the index. `centroids` is
         # {category: unit vector}, `key_to_cat` maps embedding key -> base
         # category (groups omitted). Both rebuilt whenever the index loads.
         "centroids": None, "key_to_cat": None}


def _build_centroids(matrix, keys):
    """Build per-category centroids from people.json base categories.

    Membership = each person's base `category` in out/people.json, matched to the
    embedding key. Groups (kind=="group") and people with no category are skipped
    (they get no centroid and contribute no boost). Best-effort: any failure
    returns empty maps so search simply runs with no prior. Cheap: a dict lookup
    per key plus a mean per category over ~1000 rows.
    """
    try:
        import embeddings as _emb
        people = json.load(open(os.path.join(HERE, "out/people.json"), encoding="utf-8"))
        key_to_cat = {}
        for p in people:
            if p.get("kind") == "group":
                continue  # skip groups
            cat = p.get("category")
            if cat:
                key_to_cat[p.get("key")] = cat
        centroids = _emb.category_centroids(matrix, keys, key_to_cat)
        return centroids, key_to_cat
    except Exception:
        return {}, {}


def _load_embed_index():
    """Load out/embeddings.npy + out/embedding_keys.json + the embedder once.

    Cached process-wide. On any failure, EMBED['error'] is set and search falls
    back to the legacy full keyword+model path so the endpoint never 500s just
    because the index is missing/stale. Also builds the category-prior centroids
    alongside the index (best-effort; their absence just means no boost).
    """
    if EMBED["loaded"]:
        return EMBED["matrix"] is not None
    EMBED["loaded"] = True
    try:
        import numpy as np
        import embeddings as _emb
        matrix = np.load(EMBED_NPY)
        keys = json.load(open(EMBED_KEYS, encoding="utf-8"))
        if matrix.shape[0] != len(keys):
            raise ValueError(
                f"index/key length mismatch: {matrix.shape[0]} vs {len(keys)}")
        _emb.load_embedder()  # warm the model so first query is fast
        EMBED["matrix"], EMBED["keys"] = matrix, keys
        EMBED["centroids"], EMBED["key_to_cat"] = _build_centroids(matrix, keys)
        return True
    except Exception as exc:
        EMBED["error"] = str(exc)
        return False


def _embed_candidates(q, digests, k=SEARCH_TOPK):
    """Embed the query and return the top-k candidate digests via cosine top-k.

    Returns None if the index is unavailable (caller falls back to full scan).
    The digests are returned in the order classify.smart_filter expects (a list
    of digest dicts), filtered/ordered by retrieval score.
    """
    if not _load_embed_index():
        return None
    import embeddings as _emb
    qvec = _emb.embed_query(q)
    top = _emb.cosine_topk(qvec, EMBED["matrix"], k)
    keys = EMBED["keys"]
    dmap = {d["key"]: d for d in digests}
    cands = []
    for idx, _score in top:
        d = dmap.get(keys[idx])
        if d is not None:
            cands.append(d)
    return cands


def _search_candidates_with_prior(q, digests, k=SEARCH_TOPK):
    """Search candidate selection with the SOFT category prior.

    Returns (cands, category_hint, affinity):
      - retrieve a WIDER raw-cosine top-N (SEARCH_WIDE_N) first;
      - compute the query's affinity to each category centroid -> category_hint;
      - re-rank the wide net by `raw_cosine + PRIOR_ALPHA * affinity[base_cat]`
        and take the top `k` for the (unchanged) LLM confirm step.

    Because we start from the wide raw-cosine net and PRIOR_ALPHA is modest, a
    very strong cross-category match still survives the re-rank — the prior is a
    soft nudge, never a hard category filter. Falls back gracefully: returns
    (None, None, {}) if the index is unavailable; if centroids are missing it
    behaves exactly like the old plain top-k (no boost), so search never 500s.
    """
    if not _load_embed_index():
        return None, None, {}
    import embeddings as _emb
    qvec = _emb.embed_query(q)
    keys = EMBED["keys"]
    dmap = {d["key"]: d for d in digests}

    centroids = EMBED.get("centroids") or {}
    key_to_cat = EMBED.get("key_to_cat") or {}
    affinity = _emb.query_affinity(qvec, centroids) if centroids else {}
    category_hint = max(affinity, key=affinity.get) if affinity else None

    # Wide raw-cosine net, but never below k (so we always have enough to return).
    wide = _emb.cosine_topk(qvec, EMBED["matrix"], max(SEARCH_WIDE_N, k))
    # Restrict to candidates we actually have digests for, preserving raw scores.
    rows = [(keys[idx], score) for idx, score in wide if keys[idx] in dmap]
    if not rows:
        return [], category_hint, affinity

    if affinity:
        raw = [score for _key, score in rows]
        cand_cats = [key_to_cat.get(key) for key, _score in rows]
        order = _emb.soft_rerank(raw, cand_cats, affinity, alpha=PRIOR_ALPHA)
        rows = [rows[i] for i in order]
    # else: rows already in raw-cosine order (plain top-k, no boost).

    cands = [dmap[key] for key, _score in rows[:k]]
    return cands, category_hint, affinity


# Candidate-generator hook for classify.smart_filter: embedding top-K instead of
# the keyword pre-filter, so category creation / refine is also hybrid-fast.
# K~40 (a touch wider than search's 25 — a saved category should err toward
# recall). Returns None when the index is unavailable so smart_filter cleanly
# falls back to its keyword path.
FILTER_TOPK = 40


def _filter_candidate_fn(label, digests):
    return _embed_candidates(label, digests, k=FILTER_TOPK)


# ---- static UI -------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.route("/<path:path>")
def static_files(path):
    # Serve app.js / styles.css from the root and generated data from out/.
    root = os.path.join(HERE, "out") if path.startswith("out/") else HERE
    rel = path[4:] if path.startswith("out/") else path
    return send_from_directory(root, rel)


# ---- api -------------------------------------------------------------------
@app.route("/api/status")
def status():
    return jsonify(model_loading=STATE["loading"], error=STATE["error"])


@app.route("/api/userstate", methods=["GET"])
def get_userstate():
    """Durable user state (hidden / approved / saved-to-contacts) — on disk so
    it's not tied to one browser and survives everything."""
    try:
        return jsonify(json.load(open(USERSTATE, encoding="utf-8")))
    except Exception:
        # Fresh install: return the full shape. The POST persists whatever the
        # client sends verbatim, so new keys (e.g. catOverride) round-trip without
        # server changes; this default just keeps the empty shape complete.
        return jsonify({"excluded": {}, "approved": {}, "saved": {},
                        "dismissed": {}, "catOverride": {}})


@app.route("/api/userstate", methods=["POST"])
def set_userstate():
    body = request.get_json(force=True)
    json.dump(body, open(USERSTATE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return jsonify(ok=True)


@app.route("/api/search", methods=["GET"])
def semantic_search():
    """Ephemeral HYBRID semantic search over conversation CONTENT.

    Fast retrieval narrows the store, the LLM confirms for precision:
      1. embed the query and take a WIDE raw-cosine net against the precomputed
         index (out/embeddings.npy), then SOFT-rerank by a category prior
         (raw_cosine + PRIOR_ALPHA * affinity[base_category]) down to the top 25
         — narrows ~1000 -> 25 conversations INSTANTLY, nudged toward the query's
         likely category WITHOUT hiding strong cross-category matches;
      2. run the EXISTING strict LLM confirm (classify.batch_yesno) over ONLY
         those 25, not the full store.
    Nothing is saved. If the index is missing/stale, falls back to the legacy
    keyword-prefilter + model path (classify.smart_filter) so search still works.

      GET /api/search?q=<phrase>
        ->  {"keys": [...], "n": <int>, "ms": <float>,
             "category_hint": "<Work|Personal|...>" | null,
             "affinity": {category: score}}
    The keys / n / ms fields are unchanged, so the frontend keeps working as-is;
    category_hint / affinity are additive.
    """
    if STATE["loading"]:
        return jsonify(error="model still loading"), 503
    if STATE["error"]:
        return jsonify(error=STATE["error"]), 500
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify(error="q required"), 400
    try:
        t0 = time.time()
        digests = json.load(open(os.path.join(HERE, "out/digests.json")))
        cands, category_hint, affinity = _search_candidates_with_prior(
            q, digests, k=SEARCH_TOPK)
        if cands is None:
            # Index unavailable — legacy full path (keyword prefilter + confirm).
            keys, _keywords, _ncand = classify.smart_filter(
                STATE["model"], STATE["tok"], digests, q)
        else:
            # Hybrid: confirm ONLY the (prior-reranked) candidates — but with the
            # SEARCH relevance prompt (topical/intent, recall-favoring), not the
            # strict contact-filter prompt that would reject a topical query like
            # "restaurants in the bay area".
            keys = classify.batch_yesno(STATE["model"], STATE["tok"], cands, q,
                                        system=classify.search_prompt(q))
        # Round affinity for a compact, stable JSON payload.
        affinity = {c: round(s, 4) for c, s in (affinity or {}).items()}
        return jsonify(keys=keys, n=len(keys),
                       ms=round((time.time() - t0) * 1000, 1),
                       category_hint=category_hint, affinity=affinity)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/filter", methods=["POST"])
def create_filter():
    if STATE["loading"]:
        return jsonify(error="model still loading"), 503
    if STATE["error"]:
        return jsonify(error=STATE["error"]), 500
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    desc = (body.get("description") or name).strip()
    edit_id = (body.get("id") or "").strip()  # present when editing -> update in place
    if not name:
        return jsonify(error="name required"), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "message": "starting", "result": None}

    def run():
        try:
            # Stable id: keep the original on edit so we update in place (no dup).
            slug = edit_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            FILTER_JOB[slug] = job_id  # claim this filter; supersede any in-flight run
            existing = next((f for f in _load_filters() if f.get("id") == slug), None)
            keys_path = os.path.join(HERE, f"data/enrich_parts/filter_{slug}.json")

            # If only the name changed (same description) on a non-semantic filter,
            # keep its rule/keywords exactly — don't re-route and risk losing it.
            if existing and existing.get("type") in ("computed", "keyword") and \
                    (existing.get("description") or "") == desc:
                fdef = dict(existing); fdef["name"] = name
                n = add_filter_def(fdef)
                JOBS[job_id].update(state="done", message="done",
                                    result={"name": name, "id": slug, "type": fdef["type"],
                                            "matches": n, "warning": sanity_warning(n)})
                return

            JOBS[job_id]["message"] = "routing"
            route = classify.route_filter(STATE["model"], STATE["tok"], desc)
            ftype = route.get("type", "semantic")

            if ftype in ("computed", "keyword"):
                # Instant: store the rule/keywords; no per-conversation model run.
                fdef = {"id": slug, "name": name, "type": ftype,
                        "description": desc, "color": (existing or {}).get("color", "#5856d6")}
                if ftype == "computed":
                    fdef["rule"] = route.get("rule", {})
                    if route.get("sort"):
                        fdef["sort"] = route["sort"]
                else:
                    fdef["keywords"] = route.get("keywords", [])
                n = add_filter_def(fdef)
                # Was semantic, now isn't: drop stale keys file + clear people tags.
                if os.path.exists(keys_path):
                    os.remove(keys_path)
                    subprocess.run([sys.executable, os.path.join(HERE, "build.py")], cwd=HERE,
                                   check=True, capture_output=True)
                JOBS[job_id].update(state="done", message="done",
                                    result={"name": name, "id": slug, "type": ftype,
                                            "matches": n, "warning": sanity_warning(n)})
                return

            # semantic: keyword pre-filter -> strict model confirm on candidates.
            digests = json.load(open(os.path.join(HERE, "out/digests.json")))
            JOBS[job_id]["total"] = len(digests)
            JOBS[job_id]["message"] = "finding candidates"

            def prog(done, total):
                JOBS[job_id]["done"] = done
                JOBS[job_id]["total"] = total
                JOBS[job_id]["message"] = "classifying candidates"

            keys, keywords, ncand = classify.smart_filter(
                STATE["model"], STATE["tok"], digests, desc, progress=prog,
                candidate_fn=_filter_candidate_fn)
            if _superseded(slug, job_id):
                JOBS[job_id].update(state="superseded", message="superseded by a newer change")
                return
            # Keep the keys file too (build.py still tags people from it), but the
            # user-added category ALSO carries its model-matched member keys on the
            # object itself so the frontend reads membership straight from
            # out/filters.json. type:"category" + keys[] + category==name.
            classify.save_filter(name, desc, keys, fid=slug)
            add_filter_def({"id": slug, "name": name, "type": "category",
                            "description": desc, "color": (existing or {}).get("color", "#5856d6"),
                            "keys": keys, "category": name})
            # No full rebuild needed: the category carries its member `keys` on the
            # filter object and the frontend resolves membership from them directly
            # (effectiveCategory). build.py only re-tags p.tags, which the exclusive
            # category model no longer uses — so adding a category is now instant.
            JOBS[job_id].update(state="done", message="done",
                                result={"name": name, "id": slug, "type": "category",
                                        "matches": len(keys), "warning": sanity_warning(len(keys))})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


def _osa_str(s):
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


@app.route("/api/contacts/search", methods=["POST"])
def contacts_search():
    """Find existing contacts whose name resembles the suggested one, so the user
    can merge the number into them instead of creating a duplicate. Matches on the
    first name token (broad) and returns each candidate's first phone for context."""
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(candidates=[])
    q = name.split()[0]
    script = f'''
tell application "Contacts"
  set out to ""
  repeat with pp in (people whose name contains {_osa_str(q)})
    set ph to ""
    try
      if (count of phones of pp) > 0 then set ph to value of phone 1 of pp
    end try
    set out to out & (id of pp) & "||" & (name of pp) & "||" & ph & "%%"
  end repeat
  return out
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return jsonify(error=str(exc)), 500
    cands = []
    for rec in (r.stdout or "").split("%%"):
        parts = rec.split("||")
        if len(parts) >= 2 and parts[1].strip():
            cands.append({"id": parts[0].strip(), "name": parts[1].strip(),
                          "phone": parts[2].strip() if len(parts) > 2 else ""})
    return jsonify(candidates=cands[:8])


def _norm_key(raw):
    raw = (raw or "").strip()
    if "@" in raw:
        return raw.lower()
    d = re.sub(r"\D", "", raw)
    return d[-10:] if len(d) >= 10 else d


@app.route("/api/contacts/sync", methods=["POST"])
def contacts_sync():
    """Pull current names from Contacts.app so renames there show up here. Reads
    every contact's name + phones + emails, rebuilds the phone/email -> name map,
    and re-runs build. Slow for large address books, so it's a background job."""
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "message": "reading Contacts (this can take a minute)", "result": None}

    script = '''
on joinList(lst)
  set AppleScript's text item delimiters to ","
  set s to (lst as text)
  set AppleScript's text item delimiters to ""
  return s
end joinList
tell application "Contacts"
  set out to ""
  repeat with p in people
    set phs to {}
    try
      set phs to (value of phones of p)
    end try
    set ems to {}
    try
      set ems to (value of emails of p)
    end try
    set out to out & (name of p) & tab & my joinList(phs) & tab & my joinList(ems) & linefeed
  end repeat
  return out
end tell'''

    def run():
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                JOBS[job_id].update(state="error", message=(r.stderr or "osascript failed").strip())
                return
            name_map = {}
            for line in (r.stdout or "").splitlines():
                parts = line.split("\t")
                if len(parts) < 3 or not parts[0].strip():
                    continue
                nm = parts[0].strip()
                handles = parts[1].split(",") + parts[2].split(",")
                for h in handles:
                    k = _norm_key(h)
                    if k:
                        name_map.setdefault(k, nm)
            json.dump(name_map, open(os.path.join(HERE, "data/contacts_live.json"), "w", encoding="utf-8"),
                      ensure_ascii=False)
            JOBS[job_id]["message"] = "rebuilding"
            subprocess.run([sys.executable, os.path.join(HERE, "build.py")], cwd=HERE, check=True, capture_output=True)
            JOBS[job_id].update(state="done", message="done", result={"contacts": len(name_map)})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/contacts/open", methods=["POST"])
def contacts_open():
    """Open a person's card in Contacts.app (matched by name)."""
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify(error="name required"), 400
    script = f'''
tell application "Contacts"
  set ms to (people whose name is {_osa_str(name)})
  if ms is {{}} then return "notfound"
  set selection to ms
  activate
end tell
return "opened"'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return jsonify(error=(r.stderr or "osascript failed").strip()), 500
        out = r.stdout.strip()
        if out == "notfound":
            return jsonify(error=f'No contact named "{name}" found.'), 404
        return jsonify(ok=True)
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/contacts/update", methods=["POST"])
def contacts_update():
    """Write an approved name to Contacts.app. If `contact_id` is given, merge the
    number into that exact contact; otherwise match by name (update) or create."""
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    handle = (body.get("raw_id") or "").strip()
    bday = body.get("birthday") or None
    contact_id = (body.get("contact_id") or "").strip()
    if not name or not handle:
        return jsonify(error="name and raw_id required"), 400
    parts = name.split()
    first, last = parts[0], " ".join(parts[1:])
    is_email = "@" in handle
    if is_email:
        add = f'make new email at end of emails of p with properties {{label:"home", value:{_osa_str(handle)}}}'
    else:
        add = f'make new phone at end of phones of p with properties {{label:"mobile", value:{_osa_str(handle)}}}'
    # Optional birthday (year-less). AppleScript needs a real date object.
    bday_line = ""
    if bday and bday.get("month") and bday.get("day"):
        bday_line = (
            f'set bd to current date\n'
            f'  set day of bd to 1\n'
            f'  set month of bd to {int(bday["month"])}\n'
            f'  set day of bd to {int(bday["day"])}\n'
            f'  set birth date of p to bd')
    if contact_id:
        pick = f'set p to (first person whose id is {_osa_str(contact_id)})\n  set action to "merged"'
    else:
        pick = (f'set matches to (every person whose name is {_osa_str(name)})\n'
                f'  if matches is not {{}} then\n'
                f'    set p to item 1 of matches\n'
                f'    set action to "updated"\n'
                f'  else\n'
                f'    set p to make new person with properties {{first name:{_osa_str(first)}, last name:{_osa_str(last)}}}\n'
                f'    set action to "created"\n'
                f'  end if')
    script = f'''
tell application "Contacts"
  {pick}
  {add}
  {bday_line}
  save
  return action
end tell'''
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return jsonify(error=(r.stderr or "osascript failed").strip()), 500
        return jsonify(ok=True, action=r.stdout.strip())
    except Exception as exc:
        return jsonify(error=str(exc)), 500


@app.route("/api/names", methods=["POST"])
def infer_names():
    """Kick off name inference (local model) as a background job."""
    if STATE["loading"]:
        return jsonify(error="model still loading"), 503
    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "message": "inferring names", "result": None}

    def run():
        try:
            subprocess.run([sys.executable, "classify.py", "--names"], cwd=HERE, check=True, capture_output=True)
            subprocess.run([sys.executable, "build.py"], cwd=HERE, check=True, capture_output=True)
            JOBS[job_id].update(state="done", message="done", result={"ok": True})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/job/<job_id>")
def job_status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify(error="unknown job"), 404
    return jsonify(j)


def _excerpt(dmap, key):
    d = dmap.get(key)
    if not d:
        return ""
    txt = " ".join((m.get("text") or "")[:40] for m in d.get("sample", [])[:2])
    return f'{d["name"]} ("{txt[:70]}")'


@app.route("/api/filter/<fid>/refine", methods=["POST"])
def filter_refine(fid):
    """Re-derive a semantic filter, feeding the user's remove/move corrections to
    the model as negative/positive examples so it GENERALIZES — not just the exact
    rows, but similar ones."""
    if STATE["loading"]:
        return jsonify(error="model still loading"), 503
    f = next((x for x in _load_filters() if x.get("id") == fid), None)
    if not f:
        return jsonify(error="not found"), 404
    if f.get("type") not in ("semantic", "category"):
        return jsonify(error="Refine only applies to AI filters (computed/keyword filters are exact rules)."), 400
    neg, pos = f.get("exclude_keys") or [], f.get("include_keys") or []
    if not neg and not pos:
        return jsonify(error="No corrections yet — remove or move a few wrong matches first."), 400

    job_id = uuid.uuid4().hex[:8]
    JOBS[job_id] = {"state": "running", "done": 0, "total": 0, "message": "refining", "result": None}
    FILTER_JOB[fid] = job_id  # claim this filter; supersede any in-flight run

    def run():
        try:
            digests = json.load(open(os.path.join(HERE, "out/digests.json")))
            dmap = {d["key"]: d for d in digests}
            desc = f.get("description") or f["name"]
            aug = desc
            if neg:
                aug += "\nContacts like these should NOT match: " + "; ".join(_excerpt(dmap, k) for k in neg[:6])
            if pos:
                aug += "\nContacts like these SHOULD match: " + "; ".join(_excerpt(dmap, k) for k in pos[:6])

            def prog(done, total):
                JOBS[job_id]["done"] = done; JOBS[job_id]["total"] = total
                JOBS[job_id]["message"] = "re-classifying"

            keys, kw, ncand = classify.smart_filter(STATE["model"], STATE["tok"], digests, aug, progress=prog,
                                                    candidate_fn=_filter_candidate_fn)
            if _superseded(fid, job_id):
                JOBS[job_id].update(state="superseded", message="superseded by a newer change")
                return
            classify.save_filter(f["name"], desc, keys, fid=fid)  # keep clean description
            # For a user-added category, refresh the keyset carried on the object
            # so out/filters.json stays the source of truth the frontend reads.
            if f.get("type") == "category":
                f["keys"] = keys
                f.setdefault("category", f["name"])
                add_filter_def(f)
            subprocess.run([sys.executable, os.path.join(HERE, "build.py")], cwd=HERE, check=True, capture_output=True)
            JOBS[job_id].update(state="done", message="done", result={"name": f["name"], "matches": len(keys)})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


@app.route("/api/filter/<fid>/exclude", methods=["POST"])
def filter_exclude(fid):
    """Per-filter exception: pull one conversation out of a filter (or put it
    back). The rule/AI still runs; this key is just force-excluded."""
    body = request.get_json(force=True)
    key = body.get("key")
    op = body.get("op", "add")
    which = body.get("list", "exclude_keys")  # exclude_keys | include_keys
    if which not in ("exclude_keys", "include_keys"):
        which = "exclude_keys"
    filters = _load_filters()
    f = next((x for x in filters if x.get("id") == fid), None)
    if not f or not key:
        return jsonify(error="filter or key missing"), 404
    s = set(f.get(which) or [])
    s.discard(key) if op == "remove" else s.add(key)
    f[which] = sorted(s)
    # Moving in clears the opposite list so they don't fight.
    other = "include_keys" if which == "exclude_keys" else "exclude_keys"
    if op == "add" and key in (f.get(other) or []):
        f[other] = sorted(set(f[other]) - {key})
    _save_filters(filters)
    return jsonify(ok=True, count=len(s))


@app.route("/api/filter/<fid>", methods=["DELETE"])
def delete_filter(fid):
    safe = re.sub(r"[^a-z0-9-]", "", fid)
    filters = _load_filters()
    target = next((f for f in filters if f.get("id") == safe), None)
    filters = [f for f in filters if f.get("id") != safe]
    _save_filters(filters)
    # Semantic filters also have a keys file + people tags to clear.
    keys_path = os.path.join(HERE, f"data/enrich_parts/filter_{safe}.json")
    if os.path.exists(keys_path):
        os.remove(keys_path)
        subprocess.run([sys.executable, os.path.join(HERE, "build.py")], cwd=HERE,
                       check=True, capture_output=True)
    return jsonify(ok=True, deleted=bool(target))


if __name__ == "__main__":
    print(f"iMessage CRM server -> http://localhost:{PORT}  (model loading in background)")
    app.run(port=PORT, threaded=True)
