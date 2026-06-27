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


def build_fts(keys, texts, db_path):
    """(Re)build an FTS5 index mapping conversation key -> chunk text.
    keys[i] and texts[i] are parallel (same arrays the embedding index uses)."""
    import os
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE VIRTUAL TABLE chunks USING fts5("
            "key UNINDEXED, text, tokenize='porter unicode61')")
        con.executemany("INSERT INTO chunks(key, text) VALUES (?, ?)",
                        zip(keys, texts))
        con.commit()
    finally:
        con.close()


def fts_query(db_path, match, k=25):
    """Return up to k [(key, bm25_score, snippet)] best matches, best first.
    `match` is an FTS5 MATCH expression from to_fts_match(); "" -> []."""
    if not match:
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT key, bm25(chunks) AS s, "
            "snippet(chunks, 1, '【', '】', '…', 12) AS snip "
            "FROM chunks WHERE chunks MATCH ? ORDER BY s LIMIT ?",
            (match, k)).fetchall()
    except sqlite3.OperationalError:
        return []  # malformed match or FTS5 unavailable -> no keyword hits
    finally:
        con.close()
    # bm25 returns more-negative = better; expose as positive "higher is better".
    return [(key, -s, snip) for (key, s, snip) in rows]
