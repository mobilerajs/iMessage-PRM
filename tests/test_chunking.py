"""Tests for the chunked full-content embedding index (pure logic, NO model).

The root cause of weak semantic search was embedding only a 6-message SAMPLE per
conversation, so topics mentioned in passing were invisible. The fix embeds the
FULL conversation, split into chunks. These tests pin the two pure pieces:

  embeddings.chunk_messages   -> split a message list into windows, honoring the
                                 per-conversation caps (most-recent cap + max
                                 chunks, sampling evenly when over).
  embeddings.aggregate_chunks_to_persons
                              -> collapse chunk-level (key, score) hits to one
                                 best score + best chunk text per conversation key.

No embedding model is loaded: chunk_messages takes plain message dicts and the
aggregator takes plain (key, score, text) tuples.
"""
import embeddings


def _msgs(n):
    """n synthetic messages with identifiable text."""
    return [{"me": i % 2 == 0, "from": "X", "text": f"msg{i}"} for i in range(n)]


def test_chunk_splits_into_windows_of_size():
    # 60 messages at window=25 -> 3 windows: 25, 25, 10.
    chunks = embeddings.chunk_messages(_msgs(60), window=25, max_messages=2000,
                                       max_chunks=60)
    assert len(chunks) == 3
    # Each chunk is a single string; the first contains msg0, last contains msg59.
    assert "msg0" in chunks[0]
    assert "msg24" in chunks[0]
    assert "msg25" in chunks[1]
    assert "msg59" in chunks[2]


def test_chunk_exact_multiple():
    chunks = embeddings.chunk_messages(_msgs(50), window=25, max_messages=2000,
                                       max_chunks=60)
    assert len(chunks) == 2


def test_chunk_short_conversation_single_chunk():
    chunks = embeddings.chunk_messages(_msgs(5), window=25, max_messages=2000,
                                       max_chunks=60)
    assert len(chunks) == 1
    assert "msg0" in chunks[0] and "msg4" in chunks[0]


def test_chunk_empty_messages():
    assert embeddings.chunk_messages([], window=25, max_messages=2000,
                                     max_chunks=60) == []


def test_chunk_caps_most_recent_messages():
    # max_messages keeps only the most-recent N before chunking, so a giant
    # thread can't dominate. 100 msgs, cap 50 -> only msg50..msg99 considered.
    chunks = embeddings.chunk_messages(_msgs(100), window=25, max_messages=50,
                                       max_chunks=60)
    joined = "\n".join(chunks)
    assert "msg99" in joined
    assert "msg50" in joined
    assert "msg49" not in joined  # dropped by the most-recent cap
    assert "msg0" not in joined


def test_chunk_respects_max_chunks_cap():
    # 100 windows worth of messages but max_chunks=10 -> never more than 10 chunks
    # (sampled evenly across the conversation, not just the first/last 10).
    msgs = _msgs(2000)  # 80 windows at window=25
    chunks = embeddings.chunk_messages(msgs, window=25, max_messages=2000,
                                       max_chunks=10)
    assert len(chunks) == 10
    # Even sampling: the selection should span the whole conversation, so the
    # last window (containing msg1999) is represented.
    assert "msg1999" in chunks[-1]
    # ...and an early window too (not all clustered at one end).
    assert "msg0" in chunks[0]


def test_aggregate_keeps_best_score_and_chunk_per_key():
    # Chunk-level hits for two persons; aggregation keeps the single best score
    # per key AND the text of that best-scoring chunk.
    hits = [
        ("alice", 0.40, "alice chunk low"),
        ("bob", 0.90, "bob chunk high"),
        ("alice", 0.80, "alice chunk high"),   # alice's best
        ("bob", 0.10, "bob chunk low"),
    ]
    agg = embeddings.aggregate_chunks_to_persons(hits)
    # Returned best-first by score.
    assert [key for key, _s, _t in agg] == ["bob", "alice"]
    by_key = {key: (score, text) for key, score, text in agg}
    assert abs(by_key["alice"][0] - 0.80) < 1e-9
    assert by_key["alice"][1] == "alice chunk high"
    assert abs(by_key["bob"][0] - 0.90) < 1e-9
    assert by_key["bob"][1] == "bob chunk high"


def test_aggregate_empty():
    assert embeddings.aggregate_chunks_to_persons([]) == []


def test_aggregate_single_chunk_per_key():
    hits = [("a", 0.5, "ta"), ("b", 0.7, "tb")]
    agg = embeddings.aggregate_chunks_to_persons(hits)
    assert [k for k, _s, _t in agg] == ["b", "a"]
