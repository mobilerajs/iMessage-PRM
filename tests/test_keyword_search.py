# tests/test_keyword_search.py
import keyword_search as ks

def test_phrase_query_is_quoted_intact():
    # A user-quoted phrase becomes a single FTS5 phrase token.
    assert ks.to_fts_match('"happy birthday"') == '"happy birthday"'

def test_bare_terms_are_or_joined_and_quoted():
    # Bare multi-word -> OR of individually-quoted terms (recall-favoring).
    assert ks.to_fts_match("pizza friday") == '"pizza" OR "friday"'

def test_special_chars_are_stripped_not_injected():
    # FTS5 operators in raw input must not become syntax/injection.
    assert ks.to_fts_match('pizza* AND (drop)') == '"pizza" OR "AND" OR "drop"'

def test_empty_returns_empty():
    assert ks.to_fts_match("   ") == ""
    assert ks.to_fts_match('""') == ""

def test_build_and_query_roundtrip(tmp_path):
    db = str(tmp_path / "fts.db")
    keys = ["pA", "pB", "pC"]
    texts = [
        "lets grab pizza on friday night",
        "the wifi password is hunter2",
        "meeting about the mortgage rate",
    ]
    ks.build_fts(keys, texts, db)
    hits = ks.fts_query(db, ks.to_fts_match("pizza"), k=5)
    assert [h[0] for h in hits] == ["pA"]          # key
    assert "pizza" in hits[0][2].lower()           # snippet contains the term

def test_phrase_query_matches_contiguous(tmp_path):
    db = str(tmp_path / "fts.db")
    ks.build_fts(["p1", "p2"], ["happy birthday to you", "happy to help, birthday soon"], db)
    hits = ks.fts_query(db, ks.to_fts_match('"happy birthday"'), k=5)
    assert [h[0] for h in hits] == ["p1"]          # only the contiguous phrase

def test_query_empty_match_returns_empty(tmp_path):
    db = str(tmp_path / "fts.db")
    ks.build_fts(["p1"], ["hello"], db)
    assert ks.fts_query(db, "", k=5) == []

def test_rrf_rewards_agreement():
    # Key present high in BOTH lists beats a key high in only one.
    semantic = ["a", "b", "c"]
    keyword = ["b", "a", "d"]
    fused = ks.rrf_fuse([semantic, keyword])
    assert fused[0] in ("a", "b")        # a and b appear in both, top of result
    assert set(fused[:2]) == {"a", "b"}
    assert fused.index("c") > 1 and fused.index("d") > 1

def test_rrf_handles_empty_and_dupes():
    assert ks.rrf_fuse([[], []]) == []
    # duplicate key within one list counts only its best (first) rank
    fused = ks.rrf_fuse([["a", "a", "b"], ["b"]])
    assert fused == ["b", "a"]           # b in both -> higher
