# tests/eval/metrics.py
def recall_at_k(ranked, relevant, k):
    relevant = set(relevant)
    if not relevant:
        return 0.0
    top = ranked[:k]
    return len(set(top) & relevant) / len(relevant)

def precision_at_k(ranked, relevant, k):
    relevant = set(relevant)
    top = ranked[:k]
    return (len(set(top) & relevant) / k) if k else 0.0

def mrr(ranked, relevant):
    relevant = set(relevant)
    for i, key in enumerate(ranked, 1):
        if key in relevant:
            return 1.0 / i
    return 0.0
