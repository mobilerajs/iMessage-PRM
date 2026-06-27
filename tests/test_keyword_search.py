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
