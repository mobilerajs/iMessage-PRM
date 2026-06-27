# Eval harness

A tiny harness to measure **retrieval quality** of the hybrid (keyword + semantic)
search, so changes to the retriever can be judged with numbers instead of vibes.

## What's here

| File | Committed? | What it is |
|------|:---:|------------|
| `metrics.py` | yes | Pure `recall_at_k` / `precision_at_k` / `mrr`. |
| `test_metrics.py` | yes | Unit tests for the metrics. |
| `queries.example.jsonl` | yes | **Synthetic, PII-free** example gold set (fake keys `pA`/`pB`/`pC`). |
| `test_eval_synthetic.py` | yes | Wires metrics + FTS retrieval together on the example set (recall must be 1.0). |
| `run_eval.py` | yes | Local runner that scores the **real** gold set against the live `out/` index. |
| `queries.jsonl` | **no — gitignored** | Your **real** local gold set. References real conversation keys (PII). |
| `baseline.json` | **no — gitignored** | Saved metrics snapshot from `--update-baseline`. |

> **Privacy:** `queries.jsonl` and `baseline.json` are gitignored and must never be
> committed — this is a public repo and the real gold set references real phone
> numbers/emails as conversation keys. Only the synthetic `queries.example.jsonl`
> is committed.

## What the runner measures

`run_eval.py` replicates the **retrieval** half of the server's
`_hybrid_candidates` — keyword (FTS5/BM25) fused with semantic (cosine over the
embedding index) via Reciprocal Rank Fusion — **without the LLM confirm step**.
So the numbers are *pre-confirm* retrieval quality: how good the candidate set is
before the model filters it. That's the right thing to optimize the retriever
against, and it's model-free for keyword (the semantic path uses the small bge
embedder, not the 2.5 GB Qwen LLM, and is skipped if the embedding index is
missing).

Quoted queries (e.g. `"happy birthday"`) are treated as **literal** and scored
keyword-only, matching the server.

## The cheap labeling loop

1. Build the index once: `.venv/bin/python build.py` (creates `out/digests.json`,
   `out/fts.db`, `out/embeddings.npy`).
2. Start the server (`./start.sh`) and run a query you care about in the UI.
3. Look at the results and decide which conversations are **truly relevant**.
   Find their `key` values in `out/digests.json` (each digest has a `"key"` —
   the normalized phone/email). Copy those keys.
4. Append a line to `tests/eval/queries.jsonl`:

   ```jsonl
   {"q": "pizza", "type": "topic", "relevant": ["<key1>", "<key2>"]}
   {"q": "\"happy birthday\"", "type": "literal", "relevant": ["<key>"]}
   {"q": "mom", "type": "identity", "relevant": ["<key>"]}
   ```

   Tag each query with a `type`:
   - `topic` — semantic/keyword topic search (e.g. *pizza*, *mortgage rate*).
   - `identity` — finding a person (e.g. *mom*, *my dentist*).
   - `literal` — exact phrase, quoted (e.g. `"happy birthday"`).

5. Run the eval:

   ```bash
   .venv/bin/python tests/eval/run_eval.py
   ```

   It prints per-type and overall `recall@10/25`, `precision@10/25`, and `MRR`.

6. Save a baseline to compare future changes against:

   ```bash
   .venv/bin/python tests/eval/run_eval.py --update-baseline
   ```

   This writes `tests/eval/baseline.json` (also gitignored).

## Quick start from the example

```bash
cp tests/eval/queries.example.jsonl tests/eval/queries.jsonl
# edit queries.jsonl: replace the fake pA/pB/pC keys with real keys from out/digests.json
.venv/bin/python tests/eval/run_eval.py
```
