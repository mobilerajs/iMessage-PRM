# keyword_search.py
"""Literal keyword search over the same chunk texts the embeddings index uses,
via SQLite FTS5, plus Reciprocal Rank Fusion to combine it with semantic search.

Pure/stdlib only (sqlite3 ships FTS5 on macOS) so it's unit-testable and adds no
dependency. The index lives at out/fts.db, built alongside out/embeddings.npy."""
import re
import sqlite3

# A double-quoted run is treated as one FTS5 phrase; everything else is split on
# non-word chars and OR-joined, each term quoted so FTS5 operators in user input
# (AND/OR/NEAR/*/(): ) are data, never syntax. Recall-favoring (OR, not AND).
_PHRASE_RE = re.compile(r'"([^"]+)"')
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def to_fts_match(query: str) -> str:
    """Convert a raw user query into a safe FTS5 MATCH expression."""
    query = (query or "").strip()
    if not query:
        return ""
    m = _PHRASE_RE.search(query)
    if m:
        phrase = m.group(1).strip()
        return f'"{phrase}"' if phrase else ""
    terms = _WORD_RE.findall(query)
    return " OR ".join(f'"{t}"' for t in terms)


def is_phrase_query(query: str) -> bool:
    """True if the user wrapped the query in double quotes (force literal mode)."""
    m = _PHRASE_RE.search(query or "")
    return bool(m and m.group(1).strip())
