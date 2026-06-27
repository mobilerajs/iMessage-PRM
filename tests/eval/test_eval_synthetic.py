# tests/eval/test_eval_synthetic.py
"""Regression test proving the metric functions + the real FTS retrieval wire
together end-to-end on committed, PII-free synthetic data.

It loads the committed ``queries.example.jsonl`` (fake keys pA/pB/pC, synthetic
content), builds a tiny FTS5 index over a handful of fake conversations whose
text matches those queries, runs the same ``keyword_search`` retrieval the server
uses, and asserts ``recall_at_k == 1.0`` for every example query.
"""
import json
import os

import keyword_search

from tests.eval import metrics

HERE = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(HERE, "queries.example.jsonl")

# Fake, PII-free conversations whose content matches the example queries.
FAKE_KEYS = ["pA", "pB", "pC"]
FAKE_TEXTS = [
    "lets grab pizza",
    "happy birthday to you",
    "the mortgage rate is great",
]


def _load_example_queries():
    with open(EXAMPLE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_example_queries_recall_is_perfect(tmp_path):
    db = str(tmp_path / "fts.db")
    keyword_search.build_fts(FAKE_KEYS, FAKE_TEXTS, db)

    queries = _load_example_queries()
    assert queries, "queries.example.jsonl must contain at least one query"

    for entry in queries:
        q = entry["q"]
        relevant = set(entry["relevant"])
        match = keyword_search.to_fts_match(q)
        hits = keyword_search.fts_query(db, match, k=5)
        ranked = [h[0] for h in hits]
        assert metrics.recall_at_k(ranked, relevant, k=5) == 1.0, (
            f"query {q!r} (type={entry.get('type')}) missed {relevant}; "
            f"got ranked={ranked}"
        )
