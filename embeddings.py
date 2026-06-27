#!/usr/bin/env python3
"""Local, offline embedding index for hybrid semantic search.

At ~1000 conversations a vector DB is overkill: we keep a plain numpy matrix of
L2-normalized embeddings and do cosine = dot product. This module owns the small
embedding model and the pure-numpy top-k retrieval used to narrow ~1000 → 25
candidates before the LLM confirm step.

Model: `mlx-community/bge-small-en-v1.5-bf16` via **mlx-embeddings**. We pick the
MLX stack on purpose — the rest of the app already runs MLX (Qwen via mlx-lm),
so this adds NO torch dependency (mlx-embeddings is MLX-native). The model is a
~33M-param BGE small that emits already-L2-normalized 384-d `text_embeds`. It is
downloaded once at install and then runs fully offline — nothing leaves the
machine at query time, satisfying the privacy-first / fully-local mandate.

Public surface:
  load_embedder()                      -> (model, tokenizer), cached
  embed_texts(texts) -> np.ndarray     -> (N, d) float32, L2-normalized
  embed_query(q)     -> np.ndarray     -> (d,)  float32, L2-normalized
  cosine_topk(qvec, matrix, k)         -> [(index, score), ...] desc by score
"""
from __future__ import annotations

import os

import numpy as np

import appconfig

# Default to the MLX-native BGE small model. Overridable for experiments, but the
# index and the query MUST use the same model (same vector space).
EMBED_MODEL = appconfig.resolve("embed_model", "CRM_EMBED_MODEL", "mlx-community/bge-small-en-v1.5-bf16")

# Token budget per text. bge-small is a 512-token model; conversation digests are
# short, so 512 is plenty and keeps batches fast.
MAX_LENGTH = 512

_EMBEDDER = None  # (model, tokenizer) cached after first load


def load_embedder():
    """Load (and cache) the local embedding model + tokenizer.

    First call may download the model to the HF cache; subsequent calls — and
    every call after install — are fully offline. Cached process-wide so the
    server loads it once and keeps it warm.
    """
    global _EMBEDDER
    if _EMBEDDER is None:
        from mlx_embeddings.utils import load
        _EMBEDDER = load(EMBED_MODEL)
    return _EMBEDDER


def embed_texts(texts, batch_size: int = 64) -> np.ndarray:
    """Embed a list of texts -> (N, d) float32, L2-normalized.

    Returns a (0, 0) array for empty input. Batches to keep memory bounded over
    ~1000 rows. `text_embeds` from the BGE model is already L2-normalized; we
    re-normalize defensively so cosine == dot downstream regardless of model.
    """
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    from mlx_embeddings import generate
    model, tok = load_embedder()
    chunks = []
    for i in range(0, len(texts), batch_size):
        batch = [t if isinstance(t, str) else str(t) for t in texts[i:i + batch_size]]
        out = generate(model, tok, batch, max_length=MAX_LENGTH)
        chunks.append(np.asarray(out.text_embeds, dtype=np.float32))
    mat = np.concatenate(chunks, axis=0) if len(chunks) > 1 else chunks[0]
    return _l2_normalize(mat)


def embed_query(q: str) -> np.ndarray:
    """Embed a single query string -> (d,) float32, L2-normalized."""
    mat = embed_texts([q])
    return mat[0]


def _l2_normalize(mat: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalize; guard against zero rows."""
    mat = np.asarray(mat, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (mat / norms).astype(np.float32)


# ---- chunked full-content index --------------------------------------------
# The index embeds the FULL conversation, not a 6-message sample, so topics
# mentioned in passing ("pizza") are retrievable. Each conversation is split into
# fixed-size message WINDOWS; every window is embedded as its own row. Two caps
# keep one giant thread from dominating the index:
#   max_messages  -> consider only the most-recent N messages before chunking;
#   max_chunks    -> if that still yields too many windows, sample windows EVENLY
#                    across the conversation (not just the first/last).
# Both helpers below are pure (no model): chunking is the build-time TDD target,
# aggregation is the query-time chunk->person collapse.


def _chunk_text(msgs):
    """Render a window of message dicts to a single embeddable string.

    Same WHO/text shape as classify.digest_text so the embedded text matches what
    the conversation actually reads like, just over the full content.
    """
    lines = []
    for m in msgs:
        who = "ME" if m.get("me") else (m.get("from") or "THEM")
        text = (m.get("text") or "")
        if text:
            lines.append(f"{who}: {text}")
    return "\n".join(lines)


def chunk_messages(msgs, window=25, max_messages=2000, max_chunks=60):
    """Split a conversation's messages into embeddable chunk strings.

    - Keep only the most-recent `max_messages` (a long thread can't dominate).
    - Split into windows of `window` messages each (the LAST window may be short).
    - If that yields more than `max_chunks` windows, sample `max_chunks` of them
      EVENLY across the conversation (so the topic coverage spans the whole
      thread, not just one end).

    Returns a list of strings (one per chunk). Empty input -> []. Windows that
    render to empty text (e.g. all attachments) are dropped.
    """
    if not msgs:
        return []
    if max_messages and len(msgs) > max_messages:
        msgs = msgs[-max_messages:]
    windows = [msgs[i:i + window] for i in range(0, len(msgs), window)]
    if max_chunks and len(windows) > max_chunks:
        # Even sampling: pick max_chunks indices spread across [0, len-1],
        # always including the first and last window.
        n = len(windows)
        idxs = [round(j * (n - 1) / (max_chunks - 1)) for j in range(max_chunks)]
        windows = [windows[i] for i in idxs]
    chunks = [_chunk_text(w) for w in windows]
    return [c for c in chunks if c]


# ---- incremental embedding cache -------------------------------------------
# Re-embedding ALL chunks every build is wasteful when almost nothing changed.
# We give each conversation a cheap SIGNATURE and persist a {key: sig} map next
# to the index. On the next build, a conversation whose signature is unchanged
# REUSES its cached chunk vectors + texts; only new/changed ones are re-embedded.
# Both helpers are pure (no model, no I/O) so they are the TDD target.


def convo_signature(count, last_date) -> str:
    """A stable, comparable signature for one conversation.

    Derived from message `count` and the `last_date` of the latest message —
    both already known at build time. A new message changes count and/or
    last_date, so the signature changes and the conversation is re-embedded;
    an untouched conversation keeps the same signature and is reused.

    Returns a JSON-safe string (the sig map is persisted to embed_sig.json).
    """
    return f"{int(count)}|{last_date or ''}"


def partition_reuse(new_sigs: dict, old_sigs: dict):
    """Decide, per conversation key, reuse vs re-embed for this build.

    `new_sigs` is {key: sig} for everything being indexed THIS run; `old_sigs`
    is the prior run's persisted {key: sig}. Returns (reuse_keys, reembed_keys):

      - key in old AND new_sig == old_sig  -> REUSE  (cached vectors + texts)
      - key absent from old, OR sig differs -> REEMBED (new or changed convo)
      - key in old but absent from new      -> DROPPED (in neither list)

    The two returned lists are disjoint and together cover exactly new_sigs'
    keys, so the reassembled index stays internally consistent.
    """
    reuse, reembed = [], []
    for key, sig in new_sigs.items():
        if key in old_sigs and old_sigs[key] == sig:
            reuse.append(key)
        else:
            reembed.append(key)
    return reuse, reembed


def aggregate_chunks_to_persons(hits):
    """Collapse chunk-level hits to one best (score, chunk-text) per person key.

    `hits` is an iterable of (key, score, chunk_text). Many chunks map to the same
    conversation key; we keep the SINGLE best-scoring chunk per key (its score and
    its text, so the confirm step can judge the real matched content). Returns a
    list of (key, best_score, best_chunk_text) sorted by score descending.
    """
    best = {}  # key -> (score, text)
    for key, score, text in hits:
        cur = best.get(key)
        if cur is None or score > cur[0]:
            best[key] = (score, text)
    out = [(k, s, t) for k, (s, t) in best.items()]
    out.sort(key=lambda r: r[1], reverse=True)
    return out


# ---- category prior (soft boost) -------------------------------------------
# A SOFT re-rank that nudges retrieval toward the query's likely category
# without ever hiding strong cross-category matches. Three pure-numpy pieces:
#   category_centroids -> per-category mean embedding (L2-renormalized)
#   query_affinity     -> cosine(query, each centroid), roughly [-1, 1]
#   soft_rerank        -> argsort(raw_cosine + ALPHA * affinity[candidate_cat])
# All model-free, so they are unit-testable with hand-built vectors and cheap
# enough to recompute over ~1000 rows when the index loads.


def category_centroids(matrix: np.ndarray, keys, key_to_category):
    """Per-category centroid = L2-normalized mean of its members' embeddings.

    `keys` is parallel to `matrix` rows; `key_to_category` maps a key to its base
    category (or None to skip — e.g. groups or people with no category). Returns
    {category: (d,) unit vector}. A category with no usable rows is omitted.
    Cheap: one boolean mask + mean per category over ~1000 rows.
    """
    matrix = np.asarray(matrix, dtype=np.float32)
    if matrix.shape[0] == 0:
        return {}
    # Bucket row indices by category, skipping unmapped/None keys.
    buckets: dict = {}
    for i, key in enumerate(keys):
        cat = key_to_category.get(key)
        if cat:
            buckets.setdefault(cat, []).append(i)
    cents = {}
    for cat, idxs in buckets.items():
        mean = matrix[idxs].mean(axis=0, keepdims=True)
        cents[cat] = _l2_normalize(mean)[0]
    return cents


def query_affinity(qvec: np.ndarray, centroids):
    """Cosine of the query against each category centroid -> {category: score}.

    Both query and centroids are L2-normalized, so cosine == dot. Scores are in
    roughly [-1, 1]; the argmax is the `category_hint`. Empty centroids -> {}.
    """
    qvec = np.asarray(qvec, dtype=np.float32)
    return {cat: float(np.dot(qvec, vec)) for cat, vec in centroids.items()}


def soft_rerank(raw_cosine, cand_categories, affinity, alpha: float):
    """Re-rank candidates by `raw_cosine + alpha * affinity[candidate_category]`.

    `raw_cosine` is a 1-D array of cosine scores (parallel to `cand_categories`),
    `affinity` is {category: score} from query_affinity, `alpha` is the modest
    boost weight. A candidate whose category is missing from `affinity` gets a 0
    boost (neutral). Returns candidate indices, best first.

    SOFT, NOT A FILTER: because `alpha` is small relative to the cosine spread,
    a candidate with a much higher raw cosine still wins even when its category
    is disfavored — the prior only decides near-ties. (See test_search_prior.)
    """
    raw = np.asarray(raw_cosine, dtype=np.float32)
    boost = np.array(
        [alpha * affinity.get(c, 0.0) for c in cand_categories], dtype=np.float32)
    boosted = raw + boost
    return list(np.argsort(boosted)[::-1].astype(int))


def cosine_topk(qvec: np.ndarray, matrix: np.ndarray, k: int):
    """Top-k rows of `matrix` by cosine similarity to `qvec`.

    Vectors are L2-normalized, so cosine == dot product: scores = matrix @ qvec.
    Uses argpartition to find the top-k cheaply, then sorts just those k by score
    descending. Returns a list of (row_index, score) — at most min(k, N) items.

    Pure numpy, no model — this is the hot path and the TDD target.
    """
    matrix = np.asarray(matrix, dtype=np.float32)
    qvec = np.asarray(qvec, dtype=np.float32)
    n = matrix.shape[0]
    if n == 0 or k <= 0:
        return []
    scores = matrix @ qvec
    k = min(k, n)
    if k < n:
        # argpartition puts the k largest (unordered) in the last k slots.
        part = np.argpartition(scores, n - k)[n - k:]
    else:
        part = np.arange(n)
    # Sort the candidate indices by score, descending.
    order = part[np.argsort(scores[part])[::-1]]
    return [(int(i), float(scores[i])) for i in order]
