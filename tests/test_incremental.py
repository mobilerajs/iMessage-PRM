"""Tests for the incremental embedding cache (pure logic, NO model).

A full rebuild re-embeds ALL chunks every run even when almost nothing changed.
The fix is a per-conversation SIGNATURE + a persisted {key: sig} map: on the next
build, a conversation whose signature is unchanged REUSES its cached chunk
vectors + texts; only new or changed conversations get re-embedded.

These tests pin the two pure pieces:

  embeddings.convo_signature(count, last_date)
      -> a stable, comparable signature for one conversation. New message ->
         count and/or last_date change -> signature changes.

  embeddings.partition_reuse(new_sigs, old_sigs)
      -> (reuse_keys, reembed_keys) given this run's {key: sig} and the prior
         run's {key: sig}. Unchanged sig -> reuse; changed sig or brand-new key
         -> reembed; a key gone this run is simply dropped (in neither set).

No embedding model is loaded.
"""
import embeddings


# ---- convo_signature -------------------------------------------------------

def test_signature_stable_for_same_inputs():
    assert embeddings.convo_signature(5, "2026-06-01T10:00:00") == \
        embeddings.convo_signature(5, "2026-06-01T10:00:00")


def test_signature_changes_on_new_count():
    a = embeddings.convo_signature(5, "2026-06-01T10:00:00")
    b = embeddings.convo_signature(6, "2026-06-01T10:00:00")
    assert a != b


def test_signature_changes_on_new_last_date():
    a = embeddings.convo_signature(5, "2026-06-01T10:00:00")
    b = embeddings.convo_signature(5, "2026-06-02T09:00:00")
    assert a != b


def test_signature_is_json_safe():
    # Persisted to JSON, so it must be a primitive (str/int), never a tuple/obj.
    import json
    sig = embeddings.convo_signature(5, "2026-06-01T10:00:00")
    assert isinstance(sig, (str, int))
    json.dumps({"k": sig})  # must not raise


# ---- partition_reuse -------------------------------------------------------

def test_unchanged_keys_are_reused():
    old = {"a": "s1", "b": "s2"}
    new = {"a": "s1", "b": "s2"}
    reuse, reembed = embeddings.partition_reuse(new, old)
    assert set(reuse) == {"a", "b"}
    assert reembed == []


def test_changed_sig_keys_are_reembedded():
    old = {"a": "s1", "b": "s2"}
    new = {"a": "s1", "b": "CHANGED"}
    reuse, reembed = embeddings.partition_reuse(new, old)
    assert set(reuse) == {"a"}
    assert set(reembed) == {"b"}


def test_brand_new_keys_are_reembedded():
    old = {"a": "s1"}
    new = {"a": "s1", "c": "s3"}
    reuse, reembed = embeddings.partition_reuse(new, old)
    assert set(reuse) == {"a"}
    assert set(reembed) == {"c"}


def test_removed_keys_are_dropped():
    # A key present last run but gone this run appears in NEITHER set.
    old = {"a": "s1", "gone": "s9"}
    new = {"a": "s1"}
    reuse, reembed = embeddings.partition_reuse(new, old)
    assert "gone" not in reuse
    assert "gone" not in reembed
    assert set(reuse) == {"a"}
    assert reembed == []


def test_empty_old_index_reembeds_everything():
    # First run (no prior sig map): everything is new -> reembed all.
    new = {"a": "s1", "b": "s2"}
    reuse, reembed = embeddings.partition_reuse(new, {})
    assert reuse == []
    assert set(reembed) == {"a", "b"}


def test_partition_is_complete_and_disjoint():
    old = {"a": "s1", "b": "s2", "c": "s3"}
    new = {"a": "s1", "b": "CHANGED", "d": "s4"}  # a reuse, b changed, c gone, d new
    reuse, reembed = embeddings.partition_reuse(new, old)
    # Every current key is in exactly one of the two sets.
    assert set(reuse) | set(reembed) == set(new)
    assert set(reuse) & set(reembed) == set()
    assert set(reuse) == {"a"}
    assert set(reembed) == {"b", "d"}
