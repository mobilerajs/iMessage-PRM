"""Tests for the CATEGORY PRIOR (soft boost) on semantic search.

Pure numpy — NO model, NO server. We build a tiny fake embedding index where
two clusters of rows map to two categories (Work / Personal), then exercise the
three pure functions the prior is built from:

  embeddings.category_centroids  -> per-category mean vector (L2-renormalized)
  embeddings.query_affinity      -> cosine(query, each centroid)
  embeddings.soft_rerank         -> raw_cosine + ALPHA * affinity[candidate_cat]

The properties under test mirror the design's "soft, not a filter" mandate:
  (a) each centroid points at its own cluster (argmax affinity is correct);
  (b) for a query near cluster A, an A-category candidate is boosted above a
      BORDERLINE B-category candidate (the prior helps when categories agree);
  (c) BUT a B-category candidate with very HIGH raw cosine still ranks in the
      top-K — the modest ALPHA can't overcome a strong cross-category match, so
      the prior re-ranks rather than excludes.
"""
import numpy as np

import embeddings


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _fake_index():
    """A 4-row, 2-D index: rows 0,1 = Work cluster (~+x), rows 2,3 = Personal
    cluster (~+y). Keys are parallel to rows; the category map keys off them."""
    matrix = np.stack([
        _norm([1.0, 0.05]),   # work A
        _norm([0.95, 0.10]),  # work B
        _norm([0.05, 1.0]),   # personal A
        _norm([0.10, 0.95]),  # personal B
    ])
    keys = ["w1", "w2", "p1", "p2"]
    key_to_cat = {"w1": "Work", "w2": "Work", "p1": "Personal", "p2": "Personal"}
    return matrix, keys, key_to_cat


def test_centroids_point_at_their_clusters():
    matrix, keys, key_to_cat = _fake_index()
    cents = embeddings.category_centroids(matrix, keys, key_to_cat)

    assert set(cents) == {"Work", "Personal"}
    # Each centroid is a unit vector.
    for v in cents.values():
        assert abs(np.linalg.norm(v) - 1.0) < 1e-5
    # Work centroid leans +x, Personal centroid leans +y.
    assert cents["Work"][0] > cents["Work"][1]
    assert cents["Personal"][1] > cents["Personal"][0]


def test_query_affinity_argmax_is_correct():
    matrix, keys, key_to_cat = _fake_index()
    cents = embeddings.category_centroids(matrix, keys, key_to_cat)

    qwork = _norm([1.0, 0.0])      # clearly work-ish
    aff = embeddings.query_affinity(qwork, cents)
    assert max(aff, key=aff.get) == "Work"

    qpers = _norm([0.0, 1.0])      # clearly personal-ish
    aff2 = embeddings.query_affinity(qpers, cents)
    assert max(aff2, key=aff2.get) == "Personal"


def test_soft_boost_lifts_correct_category_over_borderline():
    matrix, keys, key_to_cat = _fake_index()
    cents = embeddings.category_centroids(matrix, keys, key_to_cat)
    qwork = _norm([1.0, 0.0])

    # Two candidates with NEARLY EQUAL raw cosine: a Work one and a borderline
    # Personal one. With the prior, Work should rank first.
    cand_cats = ["Work", "Personal"]
    raw = np.array([0.50, 0.52], dtype=np.float32)  # borderline: P slightly higher raw
    aff = embeddings.query_affinity(qwork, cents)

    ranked = embeddings.soft_rerank(raw, cand_cats, aff, alpha=0.2)
    # ranked is a list of indices (into the candidate arrays), best first.
    assert ranked[0] == 0  # the Work candidate now leads despite lower raw cosine.


def test_strong_cross_category_match_still_ranks_top_k():
    """Soft, not filter: a Personal candidate with a VERY HIGH raw cosine stays
    at the top even when the query's affinity favors Work, because ALPHA is
    modest and we re-rank a wide raw-cosine net (never hard-filter by category)."""
    matrix, keys, key_to_cat = _fake_index()
    cents = embeddings.category_centroids(matrix, keys, key_to_cat)
    qwork = _norm([1.0, 0.0])
    aff = embeddings.query_affinity(qwork, cents)

    # One Personal candidate dominates on raw cosine; the rest are weak Work.
    cand_cats = ["Personal", "Work", "Work"]
    raw = np.array([0.95, 0.30, 0.25], dtype=np.float32)
    ranked = embeddings.soft_rerank(raw, cand_cats, aff, alpha=0.2)
    assert ranked[0] == 0  # strong cross-category match is NOT suppressed.


def test_centroids_ignore_unmapped_keys():
    """Keys with no category (e.g. groups) must not crash or pollute centroids."""
    matrix, keys, key_to_cat = _fake_index()
    key_to_cat = dict(key_to_cat)
    key_to_cat["w2"] = None  # simulate a group / missing category
    cents = embeddings.category_centroids(matrix, keys, key_to_cat)
    assert "Work" in cents and "Personal" in cents
    # Work centroid now built from a single row (w1); still a unit vector.
    assert abs(np.linalg.norm(cents["Work"]) - 1.0) < 1e-5
