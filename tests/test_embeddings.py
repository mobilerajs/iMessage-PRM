"""Tests for embeddings.cosine_topk — the pure-numpy retrieval core.

These run with NO model: cosine_topk takes an already-normalized matrix and a
query vector and must return the top-k (index, score) pairs in descending score
order. Hand-built vectors with known dot products pin the expected ordering.
"""
import numpy as np

import embeddings


def _norm(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def test_cosine_topk_orders_by_score():
    # Four 2-D unit vectors at known angles from the query (1, 0):
    #   row0 = (1,0)        -> dot 1.0   (identical)
    #   row1 = (0,1)        -> dot 0.0   (orthogonal)
    #   row2 = (.7071,.7071)-> dot ~.707 (45 deg)
    #   row3 = (-1,0)       -> dot -1.0  (opposite)
    matrix = np.stack([
        _norm([1, 0]),
        _norm([0, 1]),
        _norm([1, 1]),
        _norm([-1, 0]),
    ])
    qvec = _norm([1, 0])

    top = embeddings.cosine_topk(qvec, matrix, k=3)

    # Expected order by descending cosine: row0 (1.0), row2 (~.707), row1 (0.0).
    assert [i for i, _ in top] == [0, 2, 1]
    scores = [s for _, s in top]
    assert scores == sorted(scores, reverse=True)
    assert abs(scores[0] - 1.0) < 1e-5
    assert abs(scores[1] - 0.7071) < 1e-3
    assert abs(scores[2] - 0.0) < 1e-5


def test_cosine_topk_k_larger_than_n():
    matrix = np.stack([_norm([1, 0]), _norm([0, 1])])
    qvec = _norm([1, 0])
    top = embeddings.cosine_topk(qvec, matrix, k=10)
    # Never returns more rows than exist, still fully ordered.
    assert [i for i, _ in top] == [0, 1]


def test_cosine_topk_single_best():
    matrix = np.stack([_norm([0, 1]), _norm([1, 0.05]), _norm([0.2, 1])])
    qvec = _norm([1, 0])
    top = embeddings.cosine_topk(qvec, matrix, k=1)
    assert len(top) == 1
    assert top[0][0] == 1  # row1 is closest to (1,0)
