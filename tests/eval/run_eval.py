#!/usr/bin/env python3
"""Local real-data eval runner for the hybrid (keyword + semantic) retriever.

WHAT IT MEASURES
----------------
RETRIEVAL quality, *pre-confirm*. It replicates the RETRIEVAL half of the server's
``_hybrid_candidates`` — keyword (FTS5/BM25) fused with semantic (cosine over the
embedding index) via Reciprocal Rank Fusion — WITHOUT the LLM confirm step. So the
numbers tell you how good the candidate set is before the model filters it; they
are an upper bound on what the model can return and the right thing to optimize
the retriever against. It is model-FREE for the keyword path; the semantic path
uses the small bge embedder (NOT the 2.5GB Qwen LLM) and is skipped automatically
if the embedding index is missing.

PRIVACY
-------
This reads ``tests/eval/queries.jsonl`` — the REAL, LOCAL gold set, which
references real conversation keys (phone numbers/emails) and is gitignored. It is
never committed. The committed, PII-free counterpart is ``queries.example.jsonl``.
Nothing about your data is hardcoded here; the gold set is read at runtime only.
See ``tests/eval/README.md`` for the cheap labeling loop.

USAGE
-----
    .venv/bin/python tests/eval/run_eval.py                 # print the metrics table
    .venv/bin/python tests/eval/run_eval.py --update-baseline   # also write baseline.json
    .venv/bin/python tests/eval/run_eval.py --queries path.jsonl
    .venv/bin/python tests/eval/run_eval.py --help

queries.jsonl is JSON-lines, one object per query:
    {"q": "pizza", "type": "topic",    "relevant": ["<key1>", "<key2>"]}
    {"q": "\"happy birthday\"", "type": "literal", "relevant": ["<key>"]}
    {"q": "mom", "type": "identity", "relevant": ["<key>"]}
"""
import argparse
import json
import os
import sys

# Make the project root importable so `import keyword_search` / `embeddings`
# and `from tests.eval import metrics` work no matter the cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tests.eval import metrics  # noqa: E402  (after sys.path tweak)

# Paths relative to the project root (mirrors server.py).
OUT = os.path.join(ROOT, "out")
DIGESTS = os.path.join(OUT, "digests.json")
FTS_DB = os.path.join(OUT, "fts.db")
EMBED_NPY = os.path.join(OUT, "embeddings.npy")
EMBED_KEYS = os.path.join(OUT, "embedding_keys.json")

DEFAULT_QUERIES = os.path.join(HERE, "queries.jsonl")
EXAMPLE_QUERIES = os.path.join(HERE, "queries.example.jsonl")
BASELINE = os.path.join(HERE, "baseline.json")
README = os.path.join(HERE, "README.md")

# k values to report recall@k / precision@k at.
KS = (10, 25)
# How wide a candidate net to retrieve before scoring (>= max(KS)).
WIDE = 200


def _load_queries(path):
    queries = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                raise SystemExit(f"{path}:{n}: invalid JSON line: {exc}")
            if "q" not in obj or "relevant" not in obj:
                raise SystemExit(
                    f"{path}:{n}: each line needs 'q' and 'relevant' fields")
            obj.setdefault("type", "topic")
            queries.append(obj)
    return queries


def _keyword_ranked(q, dmap):
    """Keyword (FTS5/BM25) ranking of conversation keys we have a digest for.
    Deduped to best rank per key. [] if the index is missing."""
    import keyword_search as ks
    if not os.path.exists(FTS_DB):
        return []
    hits = ks.fts_query(FTS_DB, ks.to_fts_match(q), k=WIDE)
    ranked, seen = [], set()
    for key, _score, _snip in hits:
        if key in dmap and key not in seen:
            seen.add(key)
            ranked.append(key)
    return ranked


def _semantic_ranked(q, dmap, embed_index):
    """Semantic (cosine over the embedding index) ranking, aggregated chunk->person.
    [] if the embedding index is unavailable. Uses the small bge embedder, not the
    LLM."""
    if embed_index is None:
        return []
    import embeddings as emb
    matrix, keys = embed_index
    qvec = emb.embed_query(q)
    wide = emb.cosine_topk(qvec, matrix, max(WIDE * 4, 4 * max(KS)))
    hits = [(keys[idx], score, "") for idx, score in wide if keys[idx] in dmap]
    agg = emb.aggregate_chunks_to_persons(hits)
    return [key for key, _s, _t in agg]


def _load_embed_index():
    """(matrix, keys) for the chunk-level embedding index, or None if absent/stale."""
    if not (os.path.exists(EMBED_NPY) and os.path.exists(EMBED_KEYS)):
        return None
    try:
        import numpy as np
        matrix = np.load(EMBED_NPY)
        with open(EMBED_KEYS, encoding="utf-8") as f:
            keys = json.load(f)
        if matrix.shape[0] != len(keys):
            print(f"  ! embedding index/key mismatch "
                  f"({matrix.shape[0]} vs {len(keys)}); skipping semantic")
            return None
        return matrix, keys
    except Exception as exc:  # noqa: BLE001 - semantic is optional
        print(f"  ! embedding index unavailable ({exc}); keyword-only")
        return None


def retrieve(q, is_literal, dmap, embed_index):
    """Replicate _hybrid_candidates' RETRIEVAL (no LLM confirm): RRF-fuse the
    keyword and semantic rankings. Literal (quoted) queries are keyword-only."""
    import keyword_search as ks
    kw = _keyword_ranked(q, dmap)
    if is_literal:
        return kw
    sem = _semantic_ranked(q, dmap, embed_index)
    if not sem:
        return kw  # no embedding index -> keyword only
    if not kw:
        return sem
    return ks.rrf_fuse([sem, kw])


def _is_literal(q):
    import keyword_search as ks
    return ks.is_phrase_query(q)


def _blank_acc():
    acc = {"n": 0, "mrr": 0.0}
    for k in KS:
        acc[f"r@{k}"] = 0.0
        acc[f"p@{k}"] = 0.0
    return acc


def _add(acc, ranked, relevant):
    acc["n"] += 1
    acc["mrr"] += metrics.mrr(ranked, relevant)
    for k in KS:
        acc[f"r@{k}"] += metrics.recall_at_k(ranked, relevant, k)
        acc[f"p@{k}"] += metrics.precision_at_k(ranked, relevant, k)


def _avg(acc):
    n = acc["n"] or 1
    row = {"n": acc["n"], "mrr": acc["mrr"] / n}
    for k in KS:
        row[f"r@{k}"] = acc[f"r@{k}"] / n
        row[f"p@{k}"] = acc[f"p@{k}"] / n
    return row


def evaluate(queries, dmap, embed_index):
    per_type = {}
    overall = _blank_acc()
    for entry in queries:
        q = entry["q"]
        qtype = entry.get("type", "topic")
        relevant = set(entry["relevant"])
        ranked = retrieve(q, _is_literal(q), dmap, embed_index)
        per_type.setdefault(qtype, _blank_acc())
        _add(per_type[qtype], ranked, relevant)
        _add(overall, ranked, relevant)
    results = {t: _avg(a) for t, a in per_type.items()}
    results["overall"] = _avg(overall)
    return results


def _print_table(results):
    cols = ["n", "mrr"] + [f"r@{k}" for k in KS] + [f"p@{k}" for k in KS]
    header = f"{'type':<10} " + " ".join(f"{c:>7}" for c in cols)
    print(header)
    print("-" * len(header))
    order = [t for t in results if t != "overall"] + ["overall"]
    for t in order:
        row = results[t]
        cells = []
        for c in cols:
            v = row[c]
            cells.append(f"{int(v):>7}" if c == "n" else f"{v:>7.3f}")
        print(f"{t:<10} " + " ".join(cells))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Local real-data eval for hybrid retrieval (pre-confirm). "
                    "Reads the gitignored queries.jsonl gold set.")
    parser.add_argument(
        "--queries", default=DEFAULT_QUERIES,
        help="JSONL gold set (default: tests/eval/queries.jsonl, gitignored).")
    parser.add_argument(
        "--update-baseline", action="store_true",
        help="Write the results to tests/eval/baseline.json (gitignored).")
    args = parser.parse_args(argv)

    if not os.path.exists(args.queries):
        print(
            f"No gold set found at {args.queries}.\n\n"
            "This file is the REAL, LOCAL, gitignored gold set and is never "
            "committed. To create it, copy the synthetic example and replace it "
            "with real labels:\n"
            f"    cp {EXAMPLE_QUERIES} {DEFAULT_QUERIES}\n"
            f"then follow the labeling loop in {README}.",
            file=sys.stderr)
        return 2

    if not os.path.exists(DIGESTS):
        print(f"No index found at {DIGESTS}. Build it first: "
              ".venv/bin/python build.py", file=sys.stderr)
        return 2

    queries = _load_queries(args.queries)
    if not queries:
        print(f"{args.queries} has no queries.", file=sys.stderr)
        return 2

    with open(DIGESTS, encoding="utf-8") as f:
        digests = json.load(f)
    dmap = {d["key"]: d for d in digests}

    embed_index = _load_embed_index()
    if embed_index is None:
        print("  (semantic disabled — keyword-only metrics)")

    results = evaluate(queries, dmap, embed_index)
    print()
    _print_table(results)

    if args.update_baseline:
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"\nWrote baseline -> {BASELINE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
