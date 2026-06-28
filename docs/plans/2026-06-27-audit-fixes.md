# Audit Fixes Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix all findings from the 2026-06-27 codebase audit — add hybrid keyword+semantic search, make onboarding honest, fix correctness bugs, add an eval harness, and polish the frontend — in small, test-driven, individually-committable steps.

**Architecture:** The app is a local Flask server (`server.py`) over a build pipeline (`build.py`) that turns a read-only copy of `chat.db` + a vCard into JSON artifacts in `out/`, classified/searched by local MLX models (`classify.py`, `embeddings.py`). The frontend is a single vanilla-JS page (`index.html`/`app.js`/`styles.css`). We add a new pure module `keyword_search.py` (SQLite FTS5 + RRF fusion) and a shared `appconfig.py`, keeping each change isolated and reversible. Pure logic is unit-tested with pytest; frontend changes are verified with `node --check` + a manual checklist.

**Tech Stack:** Python 3.14 (arm64 venv), Flask, MLX (Qwen3-4B + bge-small), SQLite/FTS5 (stdlib), NumPy, vanilla JS, pytest.

**Conventions for every task below:**
- Run from the repo root (the `iMessage CRM/` checkout), NOT a worktree.
- Python is the venv: `.venv/bin/python`, tests `.venv/bin/python -m pytest -q`.
- JS sanity: `node --check app.js`.
- **Public repo — privacy gate before every commit:** `git ls-files | xargs grep -niE '<real phone/name patterns>'` returns nothing new; `data/`, `out/`, `config.json`, and `tests/eval/queries.jsonl` stay gitignored.
- Commit message footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Baseline at start: **82 tests passing**.

---

## Workstream 1 — Hybrid keyword + semantic search

**Why:** Today there is no literal/keyword search over message content (search agent confirmed: no FTS5/BM25). Name search is prefix-on-name-only (`app.js:361-367`); content search is semantic-only and a single-word query that prefix-matches any name *never* runs semantic search (the gate at `app.js:604`). We add an FTS5 index over the same chunk texts the embeddings use, fuse keyword + semantic with Reciprocal Rank Fusion, and let a quoted query force literal phrase search.

### Task 1.1: Create `keyword_search.py` — FTS5 MATCH-string sanitizer

**Files:**
- Create: `keyword_search.py`
- Test: `tests/test_keyword_search.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -v`
Expected: FAIL (module `keyword_search` not found).

**Step 3: Implement**

```python
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
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -v`
Expected: PASS (4 tests).

**Step 5: Commit**

```bash
git add keyword_search.py tests/test_keyword_search.py
git commit -m "feat(search): FTS5 query sanitizer (to_fts_match) for keyword search"
```

### Task 1.2: `build_fts` + `fts_query` over chunk texts

**Files:**
- Modify: `keyword_search.py`
- Test: `tests/test_keyword_search.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -k "roundtrip or phrase_query_matches or empty_match" -v`
Expected: FAIL (`build_fts`/`fts_query` not defined).

**Step 3: Implement (append to `keyword_search.py`)**

```python
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
```

> Note: a conversation has multiple chunks → multiple rows can share a `key`. The
> fusion step (Task 1.4) dedupes by key keeping best rank, so leave rows as-is here.

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -v`
Expected: PASS (all keyword_search tests).

**Step 5: Commit**

```bash
git add keyword_search.py tests/test_keyword_search.py
git commit -m "feat(search): build_fts + fts_query (BM25 + snippet over chunks)"
```

### Task 1.3: Reciprocal Rank Fusion

**Files:**
- Modify: `keyword_search.py`
- Test: `tests/test_keyword_search.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -k rrf -v`
Expected: FAIL (`rrf_fuse` not defined).

**Step 3: Implement (append)**

```python
def rrf_fuse(ranked_lists, k=60):
    """Reciprocal Rank Fusion of several ranked key-lists into one ordering.
    score(key) = sum over lists of 1/(k + rank), rank 0-based, first occurrence
    only. No score normalization needed across cosine/BM25 — that's the point."""
    scores = {}
    for lst in ranked_lists:
        seen = set()
        for rank, key in enumerate(lst):
            if key in seen:
                continue
            seen.add(key)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    # Sort by fused score desc; ties keep first-seen order for determinism.
    order = {}
    for lst in ranked_lists:
        for key in lst:
            order.setdefault(key, len(order))
    return sorted(scores, key=lambda key: (-scores[key], order[key]))
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_keyword_search.py -v`
Expected: PASS.

**Step 5: Commit**

```bash
git add keyword_search.py tests/test_keyword_search.py
git commit -m "feat(search): reciprocal rank fusion (rrf_fuse)"
```

### Task 1.4: Build `out/fts.db` during the pipeline

**Files:**
- Modify: `build.py` (after the chunk index write, around `build.py:1487`)
- Test: `tests/test_keyword_search.py` (a build-integration-lite check is hard; covered by the unit tests above + manual rebuild)

**Step 1: Implement** — right after the `chunks_path` write block (`build.py:1486-1487`), add:

```python
        # Keyword (FTS5) index over the SAME chunk texts, so literal/phrase
        # search stays congruent with what embeddings see. Best-effort: a build
        # must still succeed if FTS5 is somehow unavailable.
        try:
            import keyword_search as _ks
            fts_path = os.path.join(OUT, "fts.db")
            if os.path.exists(fts_path):
                shutil.copy2(fts_path, fts_path + ".bak")
            _ks.build_fts(chunk_keys, chunk_texts, fts_path)
            print(f"  keyword index: {len(chunk_keys)} chunks -> {fts_path}")
        except Exception as exc:  # noqa: BLE001 - never fail a build over FTS
            print(f"  ! keyword index skipped: {exc}")
```

(`shutil` is already imported locally at `build.py:1481`; if the linter complains, hoist the existing `import shutil` above this block.)

**Step 2: Verify with a real rebuild**

Run: `.venv/bin/python build.py 2>&1 | grep -E "keyword index|chunks ->"`
Expected: a line `keyword index: <N> chunks -> .../out/fts.db`; `ls -la out/fts.db` exists.

**Step 3: Spot-check the index**

Run:
```bash
.venv/bin/python -c "import keyword_search as k; print(k.fts_query('out/fts.db', k.to_fts_match('pizza'), 5)[:2])"
```
Expected: a couple of (key, score, snippet) tuples whose snippet contains "pizza".

**Step 4: Confirm gitignore covers it**

Run: `git check-ignore out/fts.db` → expected: prints the path (ignored).

**Step 5: Commit**

```bash
git add build.py
git commit -m "feat(search): build out/fts.db keyword index alongside embeddings"
```

### Task 1.5: Fuse keyword + semantic in `/api/search`

**Files:**
- Modify: `server.py` (`_search_candidates_with_prior` region ~282-346, `semantic_search` ~430-491)
- Test: manual (route-level; the pure pieces are already covered)

**Step 1: Add a hybrid candidate builder** in `server.py` after `_search_candidates_with_prior` (~line 346):

```python
FTS_DB = os.path.join(HERE, "out", "fts.db")


def _keyword_candidates(q, dmap, k=SEARCH_TOPK):
    """[(key, snippet)] from the FTS5 index, deduped to best rank per key.
    Restricted to keys we have a digest for. [] if index missing."""
    import keyword_search as _ks
    if not os.path.exists(FTS_DB):
        return []
    hits = _ks.fts_query(FTS_DB, _ks.to_fts_match(q), k=max(k, 40))
    out, seen = [], set()
    for key, _score, snip in hits:
        if key in dmap and key not in seen:
            seen.add(key)
            out.append((key, snip))
    return out


def _hybrid_candidates(q, digests, k=SEARCH_TOPK):
    """Fuse semantic (prior-reranked) and keyword (BM25) candidate lists via RRF.
    Returns (cands, category_hint, affinity, is_literal). Each cand is a digest
    copy with __text set to its matched snippet (keyword snippet preferred when
    that's why it surfaced). Falls back to semantic-only if the FTS index is
    absent, and to keyword-only/[] if the embedding index is absent."""
    import keyword_search as _ks
    dmap = {d["key"]: d for d in digests}
    sem_cands, category_hint, affinity = _search_candidates_with_prior(q, digests, k=max(k, 40))
    kw = _keyword_candidates(q, dmap, k=max(k, 40))
    is_literal = _ks.is_phrase_query(q)

    sem_keys = [c["key"] for c in (sem_cands or [])]
    kw_keys = [key for key, _snip in kw]
    kw_snip = {key: snip for key, snip in kw}
    sem_text = {c["key"]: c.get("__text", "") for c in (sem_cands or [])}

    if is_literal:
        fused_keys = kw_keys[:k]                       # literal: keyword only
    elif sem_cands is None:
        fused_keys = kw_keys[:k]                       # no embedding index
    else:
        fused_keys = _ks.rrf_fuse([sem_keys, kw_keys])[:k]

    cands = []
    for key in fused_keys:
        if key not in dmap:
            continue
        d = dict(dmap[key])
        text = kw_snip.get(key) or sem_text.get(key) or ""
        if text:
            d["__text"] = text
        cands.append(d)
    return cands, category_hint, affinity, is_literal
```

**Step 2: Rewire `semantic_search`** — replace the candidate/confirm block (`server.py:462-476`) with:

```python
        cands, category_hint, affinity, is_literal = _hybrid_candidates(
            q, digests, k=SEARCH_TOPK)
        if not cands and not os.path.exists(FTS_DB) and not _load_embed_index():
            # Neither index available — legacy full path (keyword prefilter + confirm).
            keys = classify.smart_filter(STATE["model"], STATE["tok"], digests, q)[0]
        elif is_literal:
            # Quoted phrase: trust BM25 exact matches, skip the LLM confirm.
            keys = [c["key"] for c in cands]
        else:
            keys = classify.batch_yesno(STATE["model"], STATE["tok"], cands, q,
                                        system=classify.search_prompt(q))
```

> Keep the `snippets` block below it as-is — it already reads `__text` from `cands`.

**Step 3: Sanity-check the server boots**

Run: `.venv/bin/python -c "import ast; ast.parse(open('server.py').read()); print('ok')"`
Then restart the server and:
```bash
curl -s "http://localhost:8001/api/search?q=pizza" | .venv/bin/python -m json.tool | head
curl -s "http://localhost:8001/api/search?q=%22happy%20birthday%22" | .venv/bin/python -m json.tool | head
```
Expected: `keys`/`snippets` populated; the quoted query returns literal matches fast (no multi-second LLM wait).

**Step 4: Commit**

```bash
git add server.py
git commit -m "feat(search): hybrid keyword+semantic via RRF; quoted query = literal phrase"
```

### Task 1.6: Drop the single-word gate; teach the UI the two modes

**Files:**
- Modify: `app.js` (gate at `:604`; failure handling at `:627` handled in Workstream 5)
- Modify: `index.html` (search hint text `:22`)

**Step 1: Remove the gate.** In `onEnter` (`app.js:596-604`), delete:

```javascript
  if (!multiWord && instantHits > 0) return; // fast instant view is enough
```

so pressing Enter ALWAYS runs a content search (single-word topics like `pizza` now work). Keep `multiWord`/`instantHits` only if still referenced; otherwise remove the now-dead locals.

**Step 2: Update the placeholder** (`index.html:22`) to teach quoting:

```html
           placeholder="Type a name to filter — or press Enter to search messages (use &quot;quotes&quot; for exact)…" />
```

**Step 3: Verify**

Run: `node --check app.js` → no errors.
Manual: type `pizza`, press Enter → semantic/keyword results appear (previously did nothing). Type `"happy birthday"`, Enter → exact-phrase matches.

**Step 4: Commit**

```bash
git add app.js index.html
git commit -m "feat(search): Enter always searches content; quotes = exact; clearer hint"
```

---

## Workstream 2 — Onboarding honesty

**Why:** A stranger cloning the public repo hits cold-start blockers: Apple-Silicon-only never stated; `config.json` advertises `chat_db`/`contacts_vcf`/`model` keys that the code never reads (only `user_name`); the quick-start builds before the name is set; the ~2.5 GB model download is undocumented; the README tells users to copy the WAL but the DB is opened `immutable=1` (which ignores it).

### Task 2.1: `appconfig.py` — make `config.json` real (env > config > default)

**Files:**
- Create: `appconfig.py`
- Test: `tests/test_appconfig.py`

**Step 1: Write the failing test**

```python
# tests/test_appconfig.py
import json, importlib

def test_env_beats_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"chat_db": "from_config.db"}))
    monkeypatch.setenv("CHAT_DB", "from_env.db")
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(cfg)) == "from_env.db"

def test_config_beats_default(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"chat_db": "from_config.db"}))
    monkeypatch.delenv("CHAT_DB", raising=False)
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(cfg)) == "from_config.db"

def test_default_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAT_DB", raising=False)
    import appconfig; importlib.reload(appconfig)
    assert appconfig.resolve("chat_db", "CHAT_DB", "default.db", config_path=str(tmp_path/"missing.json")) == "default.db"
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_appconfig.py -v` → FAIL (no module).

**Step 3: Implement**

```python
# appconfig.py
"""Single source of truth for settings, with precedence env > config.json >
default. config.json is OPTIONAL (copy from config.example.json). This makes the
config.json keys (user_name, model, chat_db, contacts_vcf) actually do something
— previously only user_name was read and the path/model keys were dead."""
import json, os

HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_CONFIG = os.path.join(HERE, "config.json")


def _load(config_path=None):
    path = config_path or _DEFAULT_CONFIG
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def resolve(config_key, env_var, default, config_path=None):
    """env_var, then config.json[config_key], then default. Paths are NOT
    expanduser'd here — callers that take paths should wrap in os.path.expanduser."""
    env = os.environ.get(env_var)
    if env:
        return env
    val = _load(config_path).get(config_key)
    return val if val not in (None, "") else default
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_appconfig.py -v` → PASS (3 tests).

**Step 5: Commit**

```bash
git add appconfig.py tests/test_appconfig.py
git commit -m "feat(config): appconfig.resolve (env > config.json > default)"
```

### Task 2.2: Wire `appconfig` into build.py / classify.py / embeddings.py

**Files:**
- Modify: `build.py:48-49` (DB_PATH/VCF_PATH), `classify.py:18,30-35`, `embeddings.py:30`

**Step 1: build.py** — replace the env-only reads (`build.py:48-49`):

```python
import appconfig
DB_PATH = os.path.expanduser(appconfig.resolve("chat_db", "CHAT_DB", os.path.join(DATA, "chat.db")))
VCF_PATH = os.path.expanduser(appconfig.resolve("contacts_vcf", "CONTACTS_VCF", os.path.join(DATA, "contacts.vcf")))
```

**Step 2: classify.py** — model (`classify.py:18`) and keep `user_name` working through the same path:

```python
MODEL = appconfig.resolve("model", "CRM_MODEL", "mlx-community/Qwen3-4B-Instruct-2507-4bit")
```

(`user_identity()` at `classify.py:30-35` already reads `config.json` user_name with env `CRM_USER` priority — leave it, or refactor to `appconfig.resolve("user_name", "CRM_USER", "")` for consistency. Prefer the refactor; keep the vCard fallback that follows it.)

**Step 3: embeddings.py** — embed model (`embeddings.py:30`):

```python
import appconfig
EMBED_MODEL = appconfig.resolve("embed_model", "CRM_EMBED_MODEL", "mlx-community/bge-small-en-v1.5-bf16")
```

**Step 4: Verify nothing broke**

Run: `.venv/bin/python -m pytest -q` → all prior tests still pass.
Run: `.venv/bin/python -c "import build, classify, embeddings; print(build.DB_PATH, classify.MODEL, embeddings.EMBED_MODEL)"` → sane values.

**Step 5: Commit**

```bash
git add build.py classify.py embeddings.py
git commit -m "feat(config): honor config.json chat_db/contacts_vcf/model/user_name"
```

### Task 2.3: Add `embed_model` to the example; document config precedence

**Files:**
- Modify: `config.example.json`, `README.md`

**Step 1:** Add the now-real `embed_model` key to `config.example.json`:

```json
{
  "user_name": "Your Name",
  "model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
  "embed_model": "mlx-community/bge-small-en-v1.5-bf16",
  "chat_db": "data/chat.db",
  "contacts_vcf": "data/contacts.vcf"
}
```

**Step 2:** In README's config section, state precedence: "Any setting can be overridden by an env var (e.g. `CHAT_DB`), which beats `config.json`, which beats the built-in default." Update the env-var table (`README.md:215-219`) to note this.

**Step 3: Commit**

```bash
git add config.example.json README.md
git commit -m "docs(config): document env > config.json > default; add embed_model key"
```

### Task 2.4: Apple-Silicon + Python preflight

**Files:**
- Modify: `start.sh` (top, after `set -euo pipefail`), `build.py` (inside `main()`, not at import)

**Step 1: start.sh** — add after line 8 (`cd ...`):

```bash
if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "This app requires an Apple-Silicon Mac (MLX is arm64-only). Detected: $(uname -s)/$(uname -m)." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Xcode Command Line Tools: xcode-select --install" >&2
  exit 1
fi
```

**Step 2: build.py** — at the very start of `main()` (so importing build for tests does NOT exit), add a guard that can be skipped for CI:

```python
    import platform
    if not os.environ.get("CRM_SKIP_ARCH_CHECK") and (
            platform.system() != "Darwin" or platform.machine() != "arm64"):
        sys.exit("build.py requires an Apple-Silicon Mac (MLX is arm64-only). "
                 "Set CRM_SKIP_ARCH_CHECK=1 to override.")
```

**Step 3: Verify**

Run: `.venv/bin/python -m pytest -q` (tests import build but don't call main → still pass).
Run: `bash -n start.sh` → no syntax error.

**Step 4: Commit**

```bash
git add start.sh build.py
git commit -m "feat(onboarding): Apple-Silicon + python3 preflight checks"
```

### Task 2.5: README — quick-start reorder, model download, WAL fix, requirements

**Files:**
- Modify: `README.md`

**Step 1:** Reorder the quick-start (`README.md:24-38`) so **config comes before the first build**:
1. `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. `cp config.example.json config.json` and set `user_name`
3. put `data/chat.db` + `data/contacts.vcf` in place
4. `./start.sh` (first run downloads models — see below)

**Step 2:** Add a **Requirements** subsection near the top: Apple-Silicon Mac, macOS, Python 3.x + Xcode CLT, ~3 GB free for models, network for first run.

**Step 3:** Add a **First-run model download** note: the first build/serve downloads the Qwen3-4B model (~2.5 GB) and bge-small embeddings from Hugging Face; it happens once, needs network, and takes a few minutes. Link `docs/SETUP-QUESTIONS.md`.

**Step 4:** Fix the WAL/immutable contradiction (`README.md:42-43`): replace "copy `chat.db-wal`/`chat.db-shm` too" with: "Fully quit Messages (⌘Q) before copying so it flushes pending writes — the DB is opened `immutable=1`, which ignores any separate `-wal` file, so the copied `chat.db` must already contain your latest messages."

**Step 5:** Fix the inaccurate `requirements.txt:2` comment claim ("models download once at install") to "models download lazily on first build/serve, then are cached."

**Step 6: Verify + commit**

Run: `grep -nE "wal|immutable|2.5 GB|Apple Silicon|Requirements" README.md` → confirms edits landed.

```bash
git add README.md requirements.txt
git commit -m "docs(onboarding): reorder quick-start, document model download, fix WAL/immutable"
```

### Task 2.6: Loud warning when the vCard is missing

**Files:**
- Modify: `build.py:746-748`

**Step 1:** Promote the quiet log to an unmissable banner:

```python
    if not os.path.exists(VCF_PATH):
        print("\n" + "!" * 60)
        print(f"! NO vCARD at {VCF_PATH}")
        print("! Names will fall back to raw phone numbers. Export Contacts:")
        print("!   Contacts.app -> Cmd-A -> File -> Export -> Export vCard...")
        print("!   then save as data/contacts.vcf (or set CONTACTS_VCF).")
        print("!" * 60 + "\n")
```

**Step 2: Verify + commit**

Run: `.venv/bin/python -c "import ast; ast.parse(open('build.py').read()); print('ok')"`

```bash
git add build.py
git commit -m "feat(onboarding): prominent warning when contacts.vcf is missing"
```

---

## Workstream 3 — Correctness bugs

### Task 3.1: H1 — `apple_ns_to_iso` UTC + crash-proof

**Why:** `build.py:808-815` uses naive local-time `fromtimestamp` (shifts every message by the UTC offset; corrupts day-of for birthday inference and the recency sort) and raises on out-of-range values — and it's in the unguarded main loop (`build.py:1084`), so one bad row aborts the whole build (and rebuild backs refresh/filter/sync).

**Files:**
- Modify: `build.py:808-815`
- Test: `tests/test_timestamps.py`

**Step 1: Write the failing test**

```python
# tests/test_timestamps.py
import build

def test_known_apple_ns_to_utc():
    # Apple epoch is 2001-01-01 UTC. 0 ns -> that instant, in UTC.
    assert build.apple_ns_to_iso(0).startswith("2001-01-01T00:00:00")

def test_seconds_and_nanoseconds_agree():
    secs = 700_000_000           # seconds form
    ns = secs * 1_000_000_000    # nanoseconds form
    assert build.apple_ns_to_iso(secs) == build.apple_ns_to_iso(ns)

def test_garbage_returns_empty_not_raise():
    assert build.apple_ns_to_iso(None) == ""
    assert build.apple_ns_to_iso(10**30) == ""   # absurd -> "" not OverflowError
    assert build.apple_ns_to_iso("nope") == ""
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_timestamps.py -v` → FAIL (local-time offset and/or raises).

**Step 3: Implement** — replace `build.py:808-815`:

```python
def apple_ns_to_iso(date_val) -> str:
    """Apple timestamp (ns or s since 2001-01-01 UTC) -> ISO-8601 UTC string.
    Returns "" for missing/garbage values so one bad row never aborts a build."""
    import datetime as _dt
    try:
        if date_val is None:
            return ""
        date_val = int(date_val)
        secs = date_val / 1e9 if date_val > 1_000_000_000_000 else float(date_val)
        return _dt.datetime.fromtimestamp(
            secs + APPLE_EPOCH, tz=_dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError, TypeError):
        return ""
```

> Note: output now ends in `+00:00`. `new Date(iso)` in `app.js` parses this
> correctly, and ISO-8601 UTC strings still compare lexicographically for the
> recency sort. Existing `last_date` values regenerate on the next build.

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_timestamps.py -v` → PASS.
Run: `.venv/bin/python -m pytest -q` → all pass (check `test_recency`/`test_refresh` still green).

**Step 5: Commit**

```bash
git add build.py tests/test_timestamps.py
git commit -m "fix(build): apple_ns_to_iso uses UTC and never raises on bad rows"
```

### Task 3.2: Harden the message loop against one bad row

**Files:**
- Modify: `build.py` (the main row loop, around `build.py:1084` where `apple_ns_to_iso` is called and msg dict built)

**Step 1:** Wrap the per-row body (decode + append) so a single exception `continue`s instead of aborting. Minimal: ensure `iso = apple_ns_to_iso(date_val)` (now safe) and guard the decode/append in a `try/except Exception: continue` with a counted warning. Add a `bad_rows` counter printed in the summary.

**Step 2: Verify**

Run: `.venv/bin/python build.py 2>&1 | tail -5` → completes; if any bad rows, prints a count.
Run: `.venv/bin/python -m pytest -q` → green.

**Step 3: Commit**

```bash
git add build.py
git commit -m "fix(build): skip-and-count malformed message rows instead of aborting"
```

### Task 3.3: `norm_key` — reduce international collisions (NANP-aware)

**Why:** last-10-digits (`build.py:59-72`) collapses distinct international numbers sharing the last 10 digits into one person/Contacts-write target.

**Caveat to call out in the commit:** this changes keys for NON-US numbers, so existing `userstate` overrides keyed by the old key for those contacts won't carry over. US/NANP keys are unchanged (the common case), so most overrides survive. Acceptable for a baseline; note it.

**Files:**
- Modify: `build.py:59-72`
- Test: `tests/test_norm_key.py`

**Step 1: Write the failing test**

```python
# tests/test_norm_key.py
import build

def test_us_formats_still_collapse_to_last10():
    assert build.norm_key("+1 (555) 123-4567") == build.norm_key("5551234567")
    assert build.norm_key("+15551234567") == "5551234567"

def test_distinct_intl_numbers_do_not_collide():
    # Same last 10 digits, different country codes -> different keys now.
    uk = build.norm_key("+44 20 5551234567"[:])
    other = build.norm_key("+33 1 5551234567")
    assert uk != other

def test_email_unchanged():
    assert build.norm_key("A@B.com ") == "a@b.com"
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_norm_key.py -v` → FAIL (intl numbers collide).

**Step 3: Implement** — replace `norm_key`:

```python
def norm_key(raw: str) -> str:
    """Canonical key for matching a handle id to a contact.
    Emails -> lowercased. NANP numbers (US/Canada: 10 digits, or 11 starting
    with 1) -> last 10 digits (so "+1 (555) 123-4567" == "5551234567"). Other
    international numbers keep their FULL normalized digits to avoid last-10
    collisions across country codes. Short codes stay as-is."""
    if not raw:
        return ""
    if "@" in raw:
        return raw.strip().lower()
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return digits
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits  # intl (keep country code) / short codes
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_norm_key.py -v` → PASS.
Run: `.venv/bin/python -m pytest -q` → all green.

**Step 5: Commit**

```bash
git add build.py tests/test_norm_key.py
git commit -m "fix(build): NANP-aware norm_key to avoid international last-10 collisions

Non-US contact keys change; their userstate overrides won't carry over (US keys unchanged)."
```

### Task 3.4: Guard `JOBS`/`FILTER_JOB` with a lock + evict completed

**Files:**
- Modify: `server.py:155-156` and the job read/write sites (`/api/job/<id>` ~968, writers ~508/594/706/812/886/1007)
- Test: `tests/test_jobs.py`

**Step 1: Write the failing test**

```python
# tests/test_jobs.py
import importlib

def test_job_helpers_set_get_and_evict(monkeypatch):
    import server; importlib.reload(server)
    server.JOBS.clear()
    for i in range(server.JOBS_MAX + 5):
        server.job_set(f"j{i}", {"state": "done"})
    # Never grows unbounded.
    assert len(server.JOBS) <= server.JOBS_MAX
    # Most-recent survive.
    last = f"j{server.JOBS_MAX + 4}"
    assert server.job_get(last) == {"state": "done"}
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -v` → FAIL (`job_set`/`job_get`/`JOBS_MAX` absent).

**Step 3: Implement** — near `server.py:155`:

```python
JOBS = {}
FILTER_JOB = {}
JOBS_MAX = 50
_JOBS_LOCK = threading.Lock()


def job_set(job_id, value):
    with _JOBS_LOCK:
        JOBS[job_id] = value
        if len(JOBS) > JOBS_MAX:
            # Evict oldest insertion-order keys (dicts preserve order).
            for k in list(JOBS)[: len(JOBS) - JOBS_MAX]:
                JOBS.pop(k, None)


def job_get(job_id):
    with _JOBS_LOCK:
        return JOBS.get(job_id)
```

Then replace direct `JOBS[...] = ...` writes with `job_set(...)` and the `/api/job/<id>` read (`server.py:968-973`) with `job_get(...)`. (Leave `FILTER_JOB` supersede logic but take `_JOBS_LOCK` around its read-modify-write.)

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_jobs.py -v` → PASS.
Run: `.venv/bin/python -m pytest -q` → green; restart server, run a Refresh → job polling still works.

**Step 5: Commit**

```bash
git add server.py tests/test_jobs.py
git commit -m "fix(server): lock + cap the JOBS dict (thread-safe, bounded)"
```

### Task 3.5: `/api/userstate` — validate + back up before overwrite

**Files:**
- Modify: `server.py:423-427` (`set_userstate`)
- Test: `tests/test_userstate.py`

**Step 1: Write the failing test**

```python
# tests/test_userstate.py
import json, importlib

def test_rejects_non_dict_and_keeps_backup(tmp_path, monkeypatch):
    import server; importlib.reload(server)
    us = tmp_path / "userstate.json"
    us.write_text(json.dumps({"hidden": ["pX"]}))
    monkeypatch.setattr(server, "USERSTATE", str(us))
    # A bad payload must be rejected and the good prior state preserved.
    ok, err = server.validate_userstate([1, 2, 3])
    assert ok is False and err
    ok, err = server.validate_userstate({"hidden": ["pY"]})
    assert ok is True
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_userstate.py -v` → FAIL (`validate_userstate` absent).

**Step 3: Implement** — add a validator and use it in `set_userstate`:

```python
def validate_userstate(body):
    """Userstate must be a JSON object. Returns (ok, error_message)."""
    if not isinstance(body, dict):
        return False, "userstate must be a JSON object"
    return True, ""
```

Rewrite `set_userstate` (`server.py:423-427`):

```python
@app.route("/api/userstate", methods=["POST"])
def set_userstate():
    body = request.get_json(force=True, silent=True)
    ok, err = validate_userstate(body)
    if not ok:
        return jsonify(error=err), 400
    if os.path.exists(USERSTATE):                      # keep one backup
        try:
            import shutil; shutil.copy2(USERSTATE, USERSTATE + ".bak")
        except OSError:
            pass
    _atomic_dump(body, USERSTATE, ensure_ascii=False, indent=2)
    return jsonify(ok=True)
```

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_userstate.py -v` → PASS.
Run: `.venv/bin/python -m pytest -q` → green.

**Step 5: Commit**

```bash
git add server.py tests/test_userstate.py
git commit -m "fix(server): validate userstate payload + back up before overwrite"
```

### Task 3.6: M3 — move delete-filter / keyword-convert rebuilds off the request thread

**Why:** `delete_filter` (`server.py:1082`) and the filter type-change (`server.py:555`) run `build.py` synchronously with `check=True` and (for delete) no error handling — the endpoint hangs for the whole rebuild and hides the traceback.

**Files:**
- Modify: `server.py` (delete_filter ~1071-1083; filter type-change ~545-560)
- Modify: `app.js` (delete-filter flow — poll the job like Refresh does)

**Step 1:** Refactor `delete_filter` to spawn a background job (mirror the Refresh pattern: create a `job_id`, `job_set` progress, run `build.py` in a thread with `capture_output=True`, on `CalledProcessError` store `exc.stderr` in the job error) and return `{job_id}` immediately. Do the same for the type-change rebuild.

**Step 2:** In `app.js`, after deleting a filter, poll `/api/job/<id>` (reuse the existing Refresh polling helper) and only reload when done; show the job error inline on failure.

**Step 3: Verify**

Run: `.venv/bin/python -c "import ast; ast.parse(open('server.py').read()); print('ok')"`; `node --check app.js`.
Manual: create a throwaway filter, delete it → UI shows progress, doesn't hang, reloads on completion; kill it mid-build to confirm the error surfaces (optional).

**Step 4: Commit**

```bash
git add server.py app.js
git commit -m "fix(server): run delete/keyword-convert rebuilds as background jobs, surface stderr"
```

---

## Workstream 4 — Eval harness

**Why:** No formal evals exist. **Privacy constraint:** a real gold set references real conversation keys (phone numbers) → cannot be committed to a public repo. So: commit the runner + metric functions + a **synthetic** fixture/queries; keep the real `queries.jsonl` gitignored for local use.

### Task 4.1: Metric functions (pure, unit-tested)

**Files:**
- Create: `tests/eval/__init__.py`, `tests/eval/metrics.py`
- Test: `tests/eval/test_metrics.py`

**Step 1: Write the failing test**

```python
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
```

**Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/eval/test_metrics.py -v` → FAIL.

**Step 3: Implement**

```python
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
```

(Add `tests/eval/__init__.py` empty so `from tests.eval import ...` works.)

**Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/eval/test_metrics.py -v` → PASS.

**Step 5: Commit**

```bash
git add tests/eval/__init__.py tests/eval/metrics.py tests/eval/test_metrics.py
git commit -m "feat(eval): recall@k / precision@k / MRR metric functions"
```

### Task 4.2: Synthetic eval fixture + regression test

**Files:**
- Create: `tests/eval/queries.example.jsonl` (synthetic, committed)
- Create: `tests/eval/test_eval_synthetic.py`
- Modify: `.gitignore` (add `tests/eval/queries.jsonl`, `tests/eval/baseline.json`)

**Step 1:** Write `queries.example.jsonl` with fake keys/content, e.g.:

```jsonl
{"q": "pizza", "type": "topic", "relevant": ["pA"]}
{"q": "\"happy birthday\"", "type": "literal", "relevant": ["pB"]}
{"q": "mortgage rate", "type": "topic", "relevant": ["pC"]}
```

**Step 2:** Write `test_eval_synthetic.py` that builds a tiny FTS index (via `keyword_search.build_fts`) from a handful of fake conversations matching the example queries, runs `fts_query`, and asserts `recall_at_k == 1.0` for each — proving the metric + retrieval wire together end-to-end on committed, PII-free data.

**Step 3:** Add to `.gitignore`:

```
tests/eval/queries.jsonl
tests/eval/baseline.json
```

**Step 4: Run + verify gitignore**

Run: `.venv/bin/python -m pytest tests/eval -v` → PASS.
Run: `git check-ignore tests/eval/queries.jsonl` → prints the path.

**Step 5: Commit**

```bash
git add tests/eval/queries.example.jsonl tests/eval/test_eval_synthetic.py .gitignore
git commit -m "feat(eval): synthetic FTS eval fixture + regression test (PII-free)"
```

### Task 4.3: `run_eval.py` — local real-data runner (gitignored output)

**Files:**
- Create: `tests/eval/run_eval.py`
- Modify: `tests/eval/README.md` (how to build the real gold set locally)

**Step 1:** `run_eval.py` loads `tests/eval/queries.jsonl` (real, local), calls the SAME server functions (`_hybrid_candidates` / FTS / semantic) against the live `out/` index, computes per-type recall@k/precision@k/MRR via `metrics.py`, prints a table, and writes `baseline.json` with `--update-baseline`. It must `import server` lazily and not require the model for retrieval-only metrics.

**Step 2:** `tests/eval/README.md`: explain the cheap labeling loop — run a query in the UI, copy the truly-relevant `key` values from `out/digests.json` into `queries.jsonl`, tag `topic`/`identity`/`literal`; never commit `queries.jsonl`.

**Step 3: Verify (local only)**

Run (after building a real gold set): `.venv/bin/python tests/eval/run_eval.py` → prints metrics table.

**Step 4: Commit** (runner + README only; the real gold set stays out)

```bash
git add tests/eval/run_eval.py tests/eval/README.md
git commit -m "feat(eval): local real-data eval runner + labeling guide"
```

---

## Workstream 5 — Frontend quick wins

These are DOM/UX changes; verify each with `node --check app.js` + the manual check noted. No pytest.

### Task 5.1: `aria-live` on status + loading; screen-reader announcements

**Files:** `index.html:39,43`

**Step 1:** `#status` → add `aria-live="polite"`; `#loading` → add `role="status" aria-live="polite"`.
**Step 2:** `node --check` n/a (HTML). Open page, run a search → VoiceOver announces the result count / "Search failed".
**Step 3:** `git add index.html && git commit -m "a11y: announce search status + loading via aria-live"`

### Task 5.2: Inline Retry on search/refresh failure

**Files:** `app.js:626-630` (search), `app.js:845-885` (refresh)

**Step 1:** On failure, keep the query, and render the status as a message + a `Retry` link (mirror the existing `#clear` link pattern at `app.js:440`) that re-invokes `onEnter()` / the refresh starter.
**Step 2:** `node --check app.js`. Manual: stop the server, search → "Search failed. Retry" link works after restart.
**Step 3:** `git add app.js && git commit -m "ux: inline Retry on search/refresh failure (no dead-end)"`

### Task 5.3: Persistent model-loading banner

**Files:** `app.js:306-308` (status poll)

**Step 1:** When `/api/status` reports `model_loading`, show a persistent `#status` banner ("Model loading — first search may be slow") immediately, not only during a search; clear it when status flips ready (poll until ready).
**Step 2:** `node --check app.js`. Manual: restart server, load page during model load → banner shows, then clears.
**Step 3:** `git add app.js && git commit -m "ux: persistent model-loading banner until ready"`

### Task 5.4: Actionable empty-state

**Files:** `index.html:55` (`#empty`), `app.js` (render path)

**Step 1:** Make `#empty` distinguish "no matches" from "search ran, nothing found", and offer an action (Clear filters / press Enter to search messages). Show a Clear control when a filter/search is active.
**Step 2:** `node --check app.js`. Manual: filter to nothing → actionable empty-state with a working Clear.
**Step 3:** `git add index.html app.js && git commit -m "ux: actionable empty-state with Clear"`

### Task 5.5: Always-visible bulk-select checkboxes

**Files:** `styles.css:230-232`

**Step 1:** Replace the `opacity:0` until-hover rule with always-visible-but-dim (e.g. `opacity:.45`) that goes full opacity on hover/checked, so multi-select is discoverable and works on touch.
**Step 2:** Manual: checkboxes visible without hovering; bulk bar appears on select.
**Step 3:** `git add styles.css && git commit -m "ux: make bulk-select checkboxes discoverable (not hover-only)"`

### Task 5.6: Keyboard-operable sort headers

**Files:** `index.html:47-50`, `app.js` (header click handler ~927, renderHeader ~407)

**Step 1:** Add `tabindex="0" role="button"` to each sortable `<th>`; reflect sort state with `aria-sort`; handle Enter/Space in the header keydown to trigger the same sort as a click.
**Step 2:** `node --check app.js`. Manual: Tab to a header, press Enter → sorts; `aria-sort` toggles.
**Step 3:** `git add index.html app.js && git commit -m "a11y: keyboard-operable sort headers with aria-sort"`

---

## Workstream 6 — List virtualization (bigger; do last)

**Why:** `render()` rebuilds all ~742–1028 rows via `innerHTML` on every keystroke/sort/toggle (`app.js:399`) → input lag. This is the highest-leverage perf fix but the riskiest (must not break sort, expand, select, popover, jump-nav).

### Task 6.1: Windowed row rendering

**Files:** `app.js` (`render`/`currentRows`/`rowHtml` region ~339-506), `styles.css` (row height), `index.html` (scroll container)

**Step 1:** Implement fixed-height windowing in `#table-scroll`: compute visible range from `scrollTop`/row height, render only that slice plus a top/bottom spacer (e.g. `<tr>` with a tall `height`), re-render the window on scroll (rAF-throttled). Keep the full sorted array in memory; map row indices to data indices.

**Step 2:** Re-verify every interaction against the window: sort headers, Match expand (variable height — either disable virtualization while any row is expanded, or special-case expanded rows), bulk-select (selection survives scroll), category popover (close on scroll already exists), jump-to-top/bottom.

**Step 3: Verify**

`node --check app.js`. Manual with groups shown (~1028 rows): typing is smooth, no lag; scroll is smooth; expand/sort/select all still work.

**Step 4: Commit**

```bash
git add app.js styles.css index.html
git commit -m "perf(ui): virtualize the people table (windowed rendering)"
```

> If virtualization proves too risky to land cleanly, a cheaper fallback is to
> debounce `onInput` re-render (~120ms) and skip `buildFacets()` when only a row
> expanded — commit that instead and defer full virtualization.

---

## Final pass

### Task 7.1: Full rebuild + suite + privacy gate + README sync

**Steps:**
1. `.venv/bin/python build.py` → completes; `out/fts.db` present.
2. `.venv/bin/python -m pytest -q` → all green (target: 82 + new tests).
3. `node --check app.js` → clean.
4. Restart server; smoke-test: `pizza`, `"happy birthday"`, a name, Refresh, delete a throwaway filter, Lost-touch view.
5. Update `README.md`: add hybrid/keyword search + quoting to the search section; bump the test count; note the eval harness (`tests/eval/`).
6. Privacy gate: `git ls-files | xargs grep -niE '<phone/name/secret patterns>'` returns nothing new; confirm `out/`, `data/`, `config.json`, `tests/eval/queries.jsonl`, `tests/eval/baseline.json` are gitignored.
7. Commit the README sync.
8. **Pause for explicit user approval before `git push`** (public repo).

---

## Task summary (commit cadence)

| # | Task | Files | Tests |
|---|------|-------|-------|
| 1.1 | FTS query sanitizer | keyword_search.py | test_keyword_search.py |
| 1.2 | build_fts + fts_query | keyword_search.py | test_keyword_search.py |
| 1.3 | RRF fusion | keyword_search.py | test_keyword_search.py |
| 1.4 | Build out/fts.db | build.py | manual rebuild |
| 1.5 | Hybrid fuse in /api/search | server.py | manual curl |
| 1.6 | Drop gate + UI hint | app.js, index.html | node --check + manual |
| 2.1 | appconfig.resolve | appconfig.py | test_appconfig.py |
| 2.2 | Wire config | build/classify/embeddings.py | suite |
| 2.3 | example + docs | config.example.json, README.md | — |
| 2.4 | Arch preflight | start.sh, build.py | suite + bash -n |
| 2.5 | README onboarding | README.md, requirements.txt | grep |
| 2.6 | vCard warning | build.py | ast |
| 3.1 | apple_ns_to_iso UTC/safe | build.py | test_timestamps.py |
| 3.2 | Skip bad rows | build.py | rebuild + suite |
| 3.3 | NANP norm_key | build.py | test_norm_key.py |
| 3.4 | JOBS lock + cap | server.py | test_jobs.py |
| 3.5 | userstate validate/backup | server.py | test_userstate.py |
| 3.6 | Background rebuild jobs | server.py, app.js | manual |
| 4.1 | Metric functions | tests/eval/metrics.py | test_metrics.py |
| 4.2 | Synthetic eval | tests/eval/* | test_eval_synthetic.py |
| 4.3 | Real-data runner | tests/eval/run_eval.py | local |
| 5.1 | aria-live | index.html | manual |
| 5.2 | Retry on failure | app.js | manual |
| 5.3 | Model-loading banner | app.js | manual |
| 5.4 | Empty-state | index.html, app.js | manual |
| 5.5 | Visible checkboxes | styles.css | manual |
| 5.6 | Keyboard sort headers | index.html, app.js | manual |
| 6.1 | Virtualization | app.js, styles.css, index.html | manual |
| 7.1 | Final pass + privacy gate | README.md | full suite |
