# tests/eval/test_metrics.py
from tests.eval import metrics as m

def test_recall_at_k():
    assert m.recall_at_k(["a", "b", "c"], {"b", "z"}, k=3) == 0.5   # found 1 of 2
    assert m.recall_at_k([], {"a"}, k=3) == 0.0

def test_precision_at_k():
    assert m.precision_at_k(["a", "b", "c", "d"], {"a", "c"}, k=4) == 0.5

def test_mrr():
    assert m.mrr(["x", "a", "y"], {"a"}) == 0.5                     # first hit at rank 2
    assert m.mrr(["x", "y"], {"a"}) == 0.0
