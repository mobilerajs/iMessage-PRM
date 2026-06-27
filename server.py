#!/usr/bin/env python3
"""Local iMessage CRM server.

Serves the web UI and exposes a small API backed by the on-device model, so you
can create filters live (and, soon, tune precision and edit names) without the
CLI. The model is loaded once and kept warm, so each filter is just inference.

  .venv/bin/python server.py            # http://localhost:8001

This is the seam for packaging the app as something installable: one process
that owns the model, the data, and the UI.
"""
import datetime, json, os, re, sqlite3, subprocess, sys, threading, time, uuid
from flask import Flask, request, jsonify, send_from_directory

import classify

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", "8001"))
# The working DB path the build reads (mirrors build.py's CHAT_DB default of
# data/chat.db). Refresh snapshots the live Messages DB into THIS file, then
# rebuilds from it. Kept in sync with build.py so a custom CHAT_DB is honored.
CHAT_DB = os.path.expanduser(
    os.environ.get("CHAT_DB", os.path.join(HERE, "data", "chat.db")))
# The live, on-device Messages database. Reading it requires Full Disk Access.
LIVE_CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")
# The exported vCard the build reads (mirrors build.py's CONTACTS_VCF default of
# data/contacts.vcf). Used by /api/refresh/estimate to count contacts when the
# live Contacts sync (data/contacts_live.json) isn't present.
CONTACTS_VCF = os.path.expanduser(
    os.environ.get("CONTACTS_VCF", os.path.join(HERE, "data", "contacts.vcf")))
STATS_OUT = os.path.join(HERE, "out", "stats.json")
FILTERS_DATA = os.path.join(HERE, "data/filters.json")
FILTERS_OUT = os.path.join(HERE, "out/filters.json")
USERSTATE = os.path.join(HERE, "data/userstate.json")
EMBED_NPY = os.path.join(HERE, "out/embeddings.npy")
EMBED_KEYS = os.path.join(HERE, "out/embedding_keys.json")
# Chunk TEXT parallel to each row of the index, so the LLM confirm step can judge
# the real matched content (the chunk containing the topic) rather than the old
# 6-message digest sample. Optional: if missing we fall back to digest-based
# confirm (old-format index) so search never breaks on a stale build.
EMBED_CHUNKS = os.path.join(HERE, "out/embedding_chunks.json")

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
# Wide raw-cosine net we re-rank before taking the top SEARCH_TOPK. The index is
# now CHUNK-level (many chunks per person), so we pull a wider net of CHUNKS and
# then aggregate to persons — a person can surface via any one matching chunk.
# Starting from a net this much larger than SEARCH_TOPK is what keeps the prior
# soft: a strong cross-category match makes the wide cut on raw cosine, then
# survives the modest boost re-rank.
SEARCH_WIDE_N = max(120, 4 * SEARCH_TOPK)


def _atomic_dump(obj, path, **kw):
    """Write JSON atomically: dump to a temp file in the same dir, then
    os.replace() it into place so a crash mid-write can't leave a truncated /
    corrupt file (os.replace is atomic on the same filesystem)."""
    d = os.path.dirname(path) or "."
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, **kw)
    os.replace(tmp, path)


def _load_filters():
    try:
        return json.load(open(FILTERS_DATA, encoding="utf-8"))
    except Exception:
        return []


def _save_filters(filters):
    _atomic_dump(filters, FILTERS_DATA, ensure_ascii=False, indent=2)
    _atomic_dump(filters, FILTERS_OUT, ensure_ascii=False)


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
JOBS_MAX = 50
_JOBS_LOCK = threading.Lock()


def job_set(job_id, value):
    with _JOBS_LOCK:
        JOBS[job_id] = value
        if len(JOBS) > JOBS_MAX:
            # Evict oldest insertion-order keys (dicts preserve order).
            for k in list(JOBS)[: len(JOBS) - JOBS_MAX]:
                JOBS.pop(k, None)


def job_get(job_id):
    with _JOBS_LOCK:
        return JOBS.get(job_id)


def _run_build():
    """Run a full build.py rebuild, surfacing stderr instead of hiding it.

    Returns (ok, error). On failure `error` is the tail of build.py's stderr
    (truncated to the last 2000 chars) so a background job can report WHY the
    rebuild failed — `check=True` + `capture_output=True` would otherwise raise
    and bury the traceback. Never raises for a non-zero exit."""
    r = subprocess.run([sys.executable, os.path.join(HERE, "build.py")],
                       cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[-2000:]
        return False, err or f"build.py exited with code {r.returncode}"
    return True, ""


# Duration (seconds) of the most recent successful /api/refresh, so the pre-
# refresh modal can show a human estimate of how long the next one will take.
REFRESH_STATE = {"last_seconds": None}


def _superseded(fid, job_id):
    """True if a newer model job has started for this filter — if so the caller
    should drop its (now stale) result instead of writing it."""
    with _JOBS_LOCK:
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
EMBED = {"matrix": None, "keys": None, "chunks": None, "loaded": False,
         "error": None,
         # Category prior: cached alongside the index. `centroids` is
         # {category: unit vector}, `key_to_cat` maps conversation key -> base
         # category (groups omitted). With the chunk-level index, `keys` repeats
         # a conversation key per chunk, so each category centroid is the mean of
         # all CHUNK vectors whose conversation has that category. Both rebuilt
         # whenever the index loads.
         "centroids": None, "key_to_cat": None}


def _build_centroids(matrix, keys):
    """Build per-category centroids from people.json base categories.

    Membership = each person's base `category` in out/people.json, matched to the
    conversation key. With the CHUNK-level index, `keys` repeats a conversation
    key once per chunk, so embeddings.category_centroids averages all CHUNK rows
    whose conversation falls in that category — the centroid is built from the
    chunk matrix grouped by each chunk's conversation category. Groups
    (kind=="group") and people with no category are skipped (no centroid, no
    boost). Best-effort: any failure returns empty maps so search runs with no
    prior. Cheap: a dict lookup per chunk plus a mean per category.
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
    """Load the chunk index (npy + keys + chunk texts) + the embedder once.

    The index is CHUNK-level: out/embeddings.npy has one row per chunk,
    out/embedding_keys.json the conversation key for each row, and
    out/embedding_chunks.json the chunk TEXT for each row. Cached process-wide.
    On any failure, EMBED['error'] is set and search falls back to the legacy
    full keyword+model path so the endpoint never 500s because the index is
    missing/stale. The chunk-text file is OPTIONAL: an old-format index without
    it still loads (EMBED['chunks'] stays None) and confirm falls back to the
    digest. Also builds the category-prior centroids (best-effort; absence just
    means no boost).
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
        # Chunk texts are optional (old-format index has none).
        chunks = None
        if os.path.exists(EMBED_CHUNKS):
            chunks = json.load(open(EMBED_CHUNKS, encoding="utf-8"))
            if len(chunks) != len(keys):
                chunks = None  # mismatched/stale -> ignore, fall back to digest
        _emb.load_embedder()  # warm the model so first query is fast
        EMBED["matrix"], EMBED["keys"], EMBED["chunks"] = matrix, keys, chunks
        EMBED["centroids"], EMBED["key_to_cat"] = _build_centroids(matrix, keys)
        return True
    except Exception as exc:
        EMBED["error"] = str(exc)
        return False


def _embed_candidates(q, digests, k=SEARCH_TOPK):
    """Embed the query and return the top-k candidate PERSON digests.

    The index is chunk-level, so we take a wide net of CHUNKS, aggregate to one
    best score per conversation key, and return the top-k person digests in that
    order. Returns None if the index is unavailable (caller falls back to full
    scan). Returns a list of digest dicts as classify.smart_filter expects.
    """
    if not _load_embed_index():
        return None
    import embeddings as _emb
    qvec = _emb.embed_query(q)
    keys = EMBED["keys"]
    dmap = {d["key"]: d for d in digests}
    # Wide chunk net (enough to cover k distinct persons after dedupe).
    wide = _emb.cosine_topk(qvec, EMBED["matrix"], max(SEARCH_WIDE_N, 4 * k))
    hits = [(keys[idx], score, "") for idx, score in wide if keys[idx] in dmap]
    agg = _emb.aggregate_chunks_to_persons(hits)
    return [dmap[key] for key, _s, _t in agg[:k]]


def _search_candidates_with_prior(q, digests, k=SEARCH_TOPK):
    """Chunk-aware search candidate selection with the SOFT category prior.

    Returns (cands, category_hint, affinity):
      - retrieve a WIDE raw-cosine net over CHUNKS (SEARCH_WIDE_N);
      - AGGREGATE chunks -> persons: keep the best cosine score per conversation
        key AND the single best-matching chunk TEXT for that person;
      - compute the query's affinity to each category centroid -> category_hint;
      - re-rank the aggregated persons by
        `best_cosine + PRIOR_ALPHA * affinity[base_cat]` and take the top `k`.

    Each returned candidate is a SHALLOW COPY of its digest with `__text` set to
    the matched chunk, so the LLM confirm step judges the real matched content
    (the chunk that contains the topic) instead of the 6-message sample.

    Because we start from the wide raw-cosine net and PRIOR_ALPHA is modest, a
    very strong cross-category match still survives the re-rank — the prior is a
    soft nudge, never a hard category filter. Falls back gracefully: returns
    (None, None, {}) if the index is unavailable; if centroids are missing it
    behaves like a plain top-k (no boost); if chunk texts are missing (old
    index), `__text` is omitted and confirm uses the digest. Search never 500s.
    """
    if not _load_embed_index():
        return None, None, {}
    import embeddings as _emb
    qvec = _emb.embed_query(q)
    keys = EMBED["keys"]
    chunks = EMBED.get("chunks")
    dmap = {d["key"]: d for d in digests}

    centroids = EMBED.get("centroids") or {}
    key_to_cat = EMBED.get("key_to_cat") or {}
    affinity = _emb.query_affinity(qvec, centroids) if centroids else {}
    category_hint = max(affinity, key=affinity.get) if affinity else None

    # Wide CHUNK net (multiple chunks per person), restricted to convos we have a
    # digest for. Carry the chunk text so aggregation can keep the best snippet.
    wide = _emb.cosine_topk(qvec, EMBED["matrix"], max(SEARCH_WIDE_N, 4 * k))
    hits = []
    for idx, score in wide:
        key = keys[idx]
        if key in dmap:
            text = chunks[idx] if chunks else ""
            hits.append((key, score, text))
    if not hits:
        return [], category_hint, affinity

    # Aggregate chunks -> persons: best score + best chunk text per key.
    agg = _emb.aggregate_chunks_to_persons(hits)  # [(key, best_score, best_text)]

    # Soft category prior at the PERSON level (over the per-person best scores).
    if affinity:
        raw = [score for _key, score, _text in agg]
        cand_cats = [key_to_cat.get(key) for key, _s, _t in agg]
        order = _emb.soft_rerank(raw, cand_cats, affinity, alpha=PRIOR_ALPHA)
        agg = [agg[i] for i in order]
    # else: agg already in best-cosine order (plain top-k, no boost).

    cands = []
    for key, _score, text in agg[:k]:
        d = dict(dmap[key])  # shallow copy so we don't mutate the cached digest
        if text:
            d["__text"] = text
        cands.append(d)
    return cands, category_hint, affinity


FTS_DB = os.path.join(HERE, "out", "fts.db")


def _keyword_candidates(q, dmap, k=SEARCH_TOPK):
    """[(key, snippet)] from the FTS5 index, deduped to best rank per key.
    Restricted to keys we have a digest for. [] if index missing."""
    import keyword_search as _ks
    if not os.path.exists(FTS_DB):
        return []
    hits = _ks.fts_query(FTS_DB, _ks.to_fts_match(q), k=max(k, 40))
    out, seen = [], set()
    for key, _score, snip in hits:
        if key in dmap and key not in seen:
            seen.add(key)
            out.append((key, snip))
    return out


def _hybrid_candidates(q, digests, k=SEARCH_TOPK):
    """Fuse semantic (prior-reranked) and keyword (BM25) candidate lists via RRF.
    Returns (cands, category_hint, affinity, is_literal). Each cand is a digest
    copy with __text set to its matched snippet (keyword snippet preferred when
    that's why it surfaced). Falls back to semantic-only if the FTS index is
    absent, and to keyword-only/[] if the embedding index is absent."""
    import keyword_search as _ks
    dmap = {d["key"]: d for d in digests}
    sem_cands, category_hint, affinity = _search_candidates_with_prior(q, digests, k=max(k, 40))
    kw = _keyword_candidates(q, dmap, k=max(k, 40))
    is_literal = _ks.is_phrase_query(q)

    sem_keys = [c["key"] for c in (sem_cands or [])]
    kw_keys = [key for key, _snip in kw]
    kw_snip = {key: snip for key, snip in kw}
    sem_text = {c["key"]: c.get("__text", "") for c in (sem_cands or [])}

    if is_literal:
        fused_keys = kw_keys[:k]                       # literal: keyword only
    elif sem_cands is None:
        fused_keys = kw_keys[:k]                       # no embedding index
    else:
        fused_keys = _ks.rrf_fuse([sem_keys, kw_keys])[:k]

    cands = []
    for key in fused_keys:
        if key not in dmap:
            continue
        d = dict(dmap[key])
        text = kw_snip.get(key) or sem_text.get(key) or ""
        if text:
            d["__text"] = text
        cands.append(d)
    return cands, category_hint, affinity, is_literal


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


# Static files the frontend may fetch from the project ROOT. Everything else at
# root (config.json, data/, *.py source, etc.) is NOT served — this catch-all is
# an explicit ALLOWLIST, not a "serve the whole tree" handler. send_from_directory
# already blocks "..", but allowlisting is what keeps source/data unreachable
# even without traversal.
_ROOT_ALLOW = {"index.html", "app.js", "styles.css", "favicon.ico"}
# Generated artifacts under out/ the frontend legitimately fetches. Beyond these
# fixed names, only per-id subpaths are allowed: messages/<id>.json and
# photos/<id>.<ext> (the lazy-loaded conversation + contact photo).
_OUT_ALLOW = {"people.json", "stats.json", "filters.json", "digests.json"}
_OUT_MESSAGE_RE = re.compile(r"^messages/[A-Za-z0-9_-]+\.json$")
_OUT_PHOTO_RE = re.compile(r"^photos/[A-Za-z0-9_-]+\.[A-Za-z0-9]+$")


def _out_allowed(rel):
    """True if `rel` (a path under out/) is a known frontend artifact."""
    return (rel in _OUT_ALLOW
            or bool(_OUT_MESSAGE_RE.match(rel))
            or bool(_OUT_PHOTO_RE.match(rel)))


@app.route("/<path:path>")
def static_files(path):
    # Serve app.js / styles.css from the root and KNOWN generated data from out/.
    # Anything not on the allowlist (config.json, data/, .py source, unknown
    # out/ files) -> 404, so the server never exposes data or source.
    if path.startswith("out/"):
        rel = path[4:]
        if not _out_allowed(rel):
            return ("Not found", 404)
        return send_from_directory(os.path.join(HERE, "out"), rel)
    if path not in _ROOT_ALLOW:
        return ("Not found", 404)
    return send_from_directory(HERE, path)


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


def validate_userstate(body):
    """Userstate must be a JSON object. Returns (ok, error_message)."""
    if not isinstance(body, dict):
        return False, "userstate must be a JSON object"
    return True, ""


@app.route("/api/userstate", methods=["POST"])
def set_userstate():
    body = request.get_json(force=True, silent=True)
    ok, err = validate_userstate(body)
    if not ok:
        return jsonify(error=err), 400
    if os.path.exists(USERSTATE):                      # keep one backup
        try:
            import shutil; shutil.copy2(USERSTATE, USERSTATE + ".bak")
        except OSError:
            pass
    _atomic_dump(body, USERSTATE, ensure_ascii=False, indent=2)
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
        cands, category_hint, affinity, is_literal = _hybrid_candidates(
            q, digests, k=SEARCH_TOPK)
        if not cands and not os.path.exists(FTS_DB) and not _load_embed_index():
            # Neither index available — legacy full path (keyword prefilter + confirm).
            keys = classify.smart_filter(STATE["model"], STATE["tok"], digests, q)[0]
        elif is_literal:
            # Quoted phrase: trust BM25 exact matches, skip the LLM confirm.
            keys = [c["key"] for c in cands]
        else:
            # Hybrid: confirm ONLY the (prior-reranked + keyword-fused) candidates —
            # but with the SEARCH relevance prompt (topical/intent, recall-favoring),
            # not the strict contact-filter prompt that would reject a topical query
            # like "restaurants in the bay area". Each candidate carries `__text`
            # (the matched chunk/snippet), which batch_yesno's default reader uses so
            # the model judges the real matched content, not the 6-message sample.
            keys = classify.batch_yesno(STATE["model"], STATE["tok"], cands, q,
                                        system=classify.search_prompt(q))
        # Round affinity for a compact, stable JSON payload.
        affinity = {c: round(s, 4) for c, s in (affinity or {}).items()}
        # Per-result matched snippet: the chunk that actually matched the query
        # (the same text the LLM confirmed on), so the UI can show WHY each person
        # surfaced and let the user expand it. Keyed by conversation key.
        snippets = {}
        if cands:
            by_key = {d["key"]: (d.get("__text") or "") for d in cands}
            snippets = {k: by_key.get(k, "") for k in keys}
        return jsonify(keys=keys, n=len(keys),
                       ms=round((time.time() - t0) * 1000, 1),
                       category_hint=category_hint, affinity=affinity,
                       snippets=snippets)
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
    job_set(job_id, {"state": "running", "done": 0, "total": 0, "message": "starting", "result": None})

    def run():
        try:
            # Stable id: keep the original on edit so we update in place (no dup).
            slug = edit_id or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            # Security: the slug becomes a file path (filter_<slug>.json) and a
            # dict key, so sanitize it the SAME way delete_filter does — strip
            # anything outside [a-z0-9-] so a value like "../../x" can never
            # escape data/enrich_parts/. Reject if nothing survives.
            slug = re.sub(r"[^a-z0-9-]", "", slug)
            if not slug:
                JOBS[job_id].update(state="error", message="invalid filter id")
                return
            with _JOBS_LOCK:
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
                # The rebuild surfaces build.py's stderr (no check=True) so a
                # failed convert reports WHY instead of a bare CalledProcessError.
                if os.path.exists(keys_path):
                    os.remove(keys_path)
                    ok, err = _run_build()
                    if not ok:
                        JOBS[job_id].update(state="error",
                                            message=err or "build failed", error=err)
                        return
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


_CONTACTS_SCRIPT = '''
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


def sync_contacts_live():
    """Read current names from Contacts.app (AppleScript) into
    data/contacts_live.json so renames/merges there show up here (build.py merges
    it, live names winning). Best-effort + fully local: returns
    {"synced": True, "contacts": N} or {"synced": False, "reason": ...}. Never
    raises. Needs macOS Automation permission for Contacts; if denied/absent it
    just returns synced=False and the caller keeps the prior vCard names."""
    try:
        r = subprocess.run(["osascript", "-e", _CONTACTS_SCRIPT],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            return {"synced": False, "reason": (r.stderr or "osascript failed").strip()[:200]}
        name_map = {}
        for line in (r.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or not parts[0].strip():
                continue
            nm = parts[0].strip()
            for h in parts[1].split(",") + parts[2].split(","):
                k = _norm_key(h)
                if k:
                    name_map.setdefault(k, nm)
        json.dump(name_map, open(os.path.join(HERE, "data/contacts_live.json"), "w",
                                 encoding="utf-8"), ensure_ascii=False)
        return {"synced": True, "contacts": len(name_map)}
    except Exception as exc:
        return {"synced": False, "reason": str(exc)[:200]}


@app.route("/api/contacts/sync", methods=["POST"])
def contacts_sync():
    """Pull current names from Contacts.app so renames there show up here. Reads
    every contact's name + phones + emails, rebuilds the phone/email -> name map,
    and re-runs build. Slow for large address books, so it's a background job."""
    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "done": 0, "total": 0, "message": "reading Contacts (this can take a minute)", "result": None})

    def run():
        try:
            res = sync_contacts_live()
            if not res.get("synced"):
                JOBS[job_id].update(state="error", message=res.get("reason") or "Contacts sync failed")
                return
            JOBS[job_id]["message"] = "rebuilding"
            subprocess.run([sys.executable, os.path.join(HERE, "build.py")], cwd=HERE, check=True, capture_output=True)
            JOBS[job_id].update(state="done", message="done", result={"contacts": res.get("contacts")})
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
    job_set(job_id, {"state": "running", "done": 0, "total": 0, "message": "inferring names", "result": None})

    def run():
        try:
            subprocess.run([sys.executable, "classify.py", "--names"], cwd=HERE, check=True, capture_output=True)
            subprocess.run([sys.executable, "build.py"], cwd=HERE, check=True, capture_output=True)
            JOBS[job_id].update(state="done", message="done", result={"ok": True})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


def snapshot_live_db():
    """Local, offline snapshot of the live Messages DB into the working copy.

    Source = ~/Library/Messages/chat.db. If it exists AND is readable (Full Disk
    Access granted), copy it to the working DB path the build reads (CHAT_DB,
    default data/chat.db) using SQLite's online backup API — a transaction-
    consistent, file-to-file LOCAL copy (no network, ever). The prior working
    copy is backed up first (best effort).

    If the live DB is absent or unreadable (no FDA), do NOTHING and return a flag
    so the caller just rebuilds the existing working copy. NEVER raises: a failed
    snapshot must never block the refresh.

    Returns a dict: {"snapshotted": bool, "reason": str}.
    """
    src = LIVE_CHAT_DB
    if not os.path.exists(src):
        return {"snapshotted": False, "reason": "live Messages DB not found"}
    if not os.access(src, os.R_OK):
        return {"snapshotted": False,
                "reason": "live Messages DB not readable (grant Full Disk Access)"}

    src_conn = dest_conn = None
    try:
        # Back up the prior working copy first (best effort, local file copy).
        if os.path.exists(CHAT_DB):
            try:
                import shutil
                shutil.copy2(CHAT_DB, CHAT_DB + ".bak")
            except Exception:
                pass  # a missing backup must not stop the snapshot

        os.makedirs(os.path.dirname(CHAT_DB), exist_ok=True)
        # Read-only + immutable URI so we physically cannot touch the live DB;
        # .backup() is a local, transaction-consistent file-to-file copy.
        src_conn = sqlite3.connect(
            f"file:{src}?mode=ro&immutable=1", uri=True)
        dest_conn = sqlite3.connect(CHAT_DB)
        src_conn.backup(dest_conn)
        return {"snapshotted": True, "reason": "copied live DB via backup API"}
    except Exception as exc:
        # No FDA often surfaces here as an OperationalError; treat as graceful.
        return {"snapshotted": False, "reason": f"snapshot skipped: {exc}"}
    finally:
        for c in (dest_conn, src_conn):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """On-demand refresh: snapshot the live Messages DB (best effort), then run
    the incremental build. Reuses the JOBS dict + /api/job/<id> status route.

    The job: snapshot -> rebuild -> read out/stats.json for last_synced. Uses
    sys.executable (never bare python3). Fully local/offline."""
    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "done": 0, "total": 0,
                     "message": "snapshotting", "result": None})

    def run():
        try:
            t_start = time.time()  # time the whole snapshot+contacts+rebuild run
            JOBS[job_id]["message"] = "snapshotting messages"
            snap = snapshot_live_db()  # best effort; never raises
            JOBS[job_id]["message"] = "syncing contacts"
            csync = sync_contacts_live()  # best effort; never raises
            JOBS[job_id]["message"] = "rebuilding"
            subprocess.run([sys.executable, "build.py"], cwd=HERE,
                           check=True, capture_output=True)
            # Record the duration so /api/refresh/estimate can show how long the
            # next refresh is likely to take.
            REFRESH_STATE["last_seconds"] = round(time.time() - t_start, 1)
            last_synced = None
            try:
                stats = json.load(open(STATS_OUT, encoding="utf-8"))
                last_synced = stats.get("last_synced")
            except Exception:
                pass
            JOBS[job_id].update(
                state="done", message="done",
                result={"last_synced": last_synced,
                        "snapshotted": snap.get("snapshotted"),
                        "snapshot_reason": snap.get("reason"),
                        "contacts_synced": csync.get("synced"),
                        "contacts": csync.get("contacts")})
        except subprocess.CalledProcessError as exc:
            err = (exc.stderr or b"").decode("utf-8", "replace").strip() \
                if isinstance(exc.stderr, (bytes, bytearray)) else str(exc)
            JOBS[job_id].update(state="error", message=err or "build failed")
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(job_id=job_id)


def _count_vcard_contacts(path):
    """Count FN: lines in a vCard (one per contact). Returns an int, or None if
    the file is missing/unreadable. Never raises."""
    try:
        n = 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("FN:") or line.startswith("FN;"):
                    n += 1
        return n
    except Exception:
        return None


@app.route("/api/refresh/estimate")
def refresh_estimate():
    """Numbers the pre-refresh modal combines into a human time estimate.

    Returns the working DB size, a contacts count (live Contacts sync if present,
    else the vCard's FN: count), and the duration of the most recent refresh.
    Never raises: any missing file or error just yields a null for that field."""
    db_mb = None
    try:
        if os.path.exists(CHAT_DB):
            db_mb = round(os.path.getsize(CHAT_DB) / 1e6, 1)
    except Exception:
        db_mb = None

    contacts = None
    try:
        live_path = os.path.join(HERE, "data", "contacts_live.json")
        if os.path.exists(live_path):
            contacts = len(json.load(open(live_path, encoding="utf-8")))
        else:
            contacts = _count_vcard_contacts(CONTACTS_VCF)
    except Exception:
        contacts = None

    return jsonify(db_mb=db_mb, contacts=contacts,
                   last_seconds=REFRESH_STATE.get("last_seconds"))


@app.route("/api/job/<job_id>")
def job_status(job_id):
    j = job_get(job_id)
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
    # Security: fid (URL path segment) becomes a file path (filter_<fid>.json)
    # via save_filter and a FILTER_JOB key — sanitize the SAME way delete_filter
    # does so a value like "../../x" can never escape data/enrich_parts/.
    fid = re.sub(r"[^a-z0-9-]", "", fid)
    if not fid:
        return jsonify(error="invalid filter id"), 400
    f = next((x for x in _load_filters() if x.get("id") == fid), None)
    if not f:
        return jsonify(error="not found"), 404
    if f.get("type") not in ("semantic", "category"):
        return jsonify(error="Refine only applies to AI filters (computed/keyword filters are exact rules)."), 400
    neg, pos = f.get("exclude_keys") or [], f.get("include_keys") or []
    if not neg and not pos:
        return jsonify(error="No corrections yet — remove or move a few wrong matches first."), 400

    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "done": 0, "total": 0, "message": "refining", "result": None})
    with _JOBS_LOCK:
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
    # Sanitize: fid is a URL path segment that becomes a file path
    # (filter_<safe>.json), so strip anything outside [a-z0-9-] first.
    safe = re.sub(r"[^a-z0-9-]", "", fid)
    filters = _load_filters()
    target = next((f for f in filters if f.get("id") == safe), None)
    filters = [f for f in filters if f.get("id") != safe]
    # Persist the delete on the request thread so the change is durable before we
    # return — only the (slow) rebuild moves to the background.
    _save_filters(filters)
    keys_path = os.path.join(HERE, f"data/enrich_parts/filter_{safe}.json")
    needs_rebuild = os.path.exists(keys_path)
    if needs_rebuild:
        os.remove(keys_path)  # semantic filters carry a keys file + people tags

    # Nothing to rebuild (e.g. a category filter): the delete is already done.
    if not needs_rebuild:
        return jsonify(ok=True, deleted=bool(target))

    # A semantic filter's people tags need a rebuild to clear. That can take many
    # seconds, so run it as a background job (mirrors /api/refresh) and let the
    # frontend poll /api/job/<id> instead of blocking the request thread.
    job_id = uuid.uuid4().hex[:8]
    job_set(job_id, {"state": "running", "done": 0, "total": 0,
                     "message": "rebuilding", "result": None})

    def run():
        try:
            ok, err = _run_build()
            if not ok:
                JOBS[job_id].update(state="error", message=err or "build failed",
                                    error=err)
                return
            JOBS[job_id].update(state="done", message="done",
                                result={"deleted": bool(target), "id": safe})
        except Exception as exc:
            JOBS[job_id].update(state="error", message=str(exc), error=str(exc))

    threading.Thread(target=run, daemon=True).start()
    return jsonify(ok=True, deleted=bool(target), job_id=job_id)


if __name__ == "__main__":
    print(f"iMessage CRM server -> http://localhost:{PORT}  (model loading in background)")
    app.run(port=PORT, threaded=True)
