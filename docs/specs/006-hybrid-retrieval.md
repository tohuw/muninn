# Spec 006 — Hybrid retrieval

**Status:** ready to implement after 004 and 005
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/corpus-measurements.md`. It contains the
measurement that removes a whole category of work from this spec.

## Why

Lexical search fails on the most common recall pattern: **you do not remember your
own wording, you remember the situation.** That is what semantic retrieval fixes.

## The measurement that shapes this spec

Brute-force numpy cosine over 60,000 chunks × 1024 dims: **1.9 ms**.

Therefore: **no vector database, no HNSW, no faiss, ever.** A matrix multiply and
an `argpartition` are sufficient well past any plausible corpus size. Anyone
proposing an ANN index for this workload has not measured it.

The entire cost of "semantic" is *generating* embeddings once, not searching them.

## Scope

**In:** `EmbeddingProvider` protocol, a local MLX provider behind the optional
`[semantic]` extra, embedding storage, reciprocal-rank fusion, `--deep` LLM
rerank, correlation of similar sessions.

**Out:** the Bedrock embedding provider (internal distribution), the console.

## Files

| File | Action |
|---|---|
| `muninn/embed.py` | **new** — protocol, storage, cosine search |
| `muninn/embed_mlx.py` | **new** — local provider, imported lazily |
| `muninn/fuse.py` | **new** — reciprocal-rank fusion |
| `muninn/rerank.py` | **new** — `--deep` LLM rerank |
| `muninn/store.py` | `chunk_vectors` table + migration |
| `muninn/cli.py` | `embed` subcommand; `--deep`, `--semantic` on `search`; `correlate` |
| `tests/test_embed.py`, `tests/test_fuse.py` | **new** |

## Storage

```sql
CREATE TABLE chunk_vectors (
    session_id TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    model      TEXT NOT NULL,      -- provider+model id; vectors are model-specific
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL,      -- float32 little-endian, dim * 4 bytes
    PRIMARY KEY (session_id, ordinal, model)
);
```

`model` is in the primary key because **vectors from different models are not
comparable**. A model change must not silently mix embedding spaces; it produces a
new set, and search uses one model at a time.

Load into a single contiguous `numpy` array at query time, cached in memory with
the archive's mtime as the cache key. 60k × 1024 float32 is 246 MB — acceptable,
but report it in `doctor` so growth is visible.

## Provider protocol

```python
class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int
    def available(self) -> str | None:   # None if usable, else the reason
        ...
    def embed(self, texts: Sequence[str]) -> "np.ndarray":  # (len, dim) float32, L2-normalized
        ...
```

`available()` must **not** make a network call or load weights — it is called
during discovery and startup, and a slow probe there is a hang. Normalize to unit
length inside `embed()` so search is a plain dot product.

The default build has **no provider**. `muninn search --semantic` without one
prints how to install the extra and exits non-zero; it must not crash and must not
silently fall back to lexical-only while claiming semantic results.

`embed_mlx` is imported **lazily**, inside the function that constructs it, so the
default install never pays for a missing dependency.

Route every provider through `policy.check()` from spec 005. An embedding call is
still a model call.

## Fusion

Reciprocal-rank fusion over the two ranked lists:

```
score(s) = Σ over lists  1 / (k + rank(s))     with k = 60
```

RRF is chosen deliberately over score normalization: bm25 and cosine are not
commensurable, and any attempt to scale one into the other invents a weighting
nobody can defend. RRF only needs ranks.

Default `search` = capped lexical (spec 004) fused with semantic when a provider
exists. Target under ~50 ms; assert it in a test with a warm cache.

## `--deep`

Adds an LLM rerank over the top ~30 fused candidates, plus correlation. Budget
~2.5 s. Rules:

- `--deep` **without** a provider is an error, not a silent downgrade.
- The reranker receives *snippets*, never whole transcripts, and the text is
  redacted first (spec 005's gate).
- Transcript excerpts are framed as observed data, never instructions.
- Cache rerank results keyed by `(query, candidate session ids)` so pressing enter
  twice is free.

## `muninn correlate`

```
muninn correlate SESSION_ID [--limit 10]
```

Nearest neighbours of a session's mean vector, excluding itself. This is the
"conversations like this one" feature, and it is why embeddings are worth having
beyond query-time recall.

## Build order

1. `chunk_vectors` table + migration.
2. `muninn/embed.py`: protocol, `store_vectors`, `load_matrix`, `cosine_topk`.
3. `muninn/fuse.py`: `rrf(lists, k=60)` — pure, trivially testable.
4. `muninn embed [--limit N] [--dry-run]` to backfill.
5. Wire `--semantic` into `search`; fuse when available.
6. `muninn/embed_mlx.py` behind the extra.
7. `--deep` rerank and `correlate`.
8. Tests.

## Acceptance criteria

`tests/test_fuse.py`:

1. **RRF is order-correct** — a session ranked 1st in one list and 3rd in another
   beats one ranked 2nd and 2nd; verify against hand-computed scores.
2. **RRF handles disjoint lists** and single-list input.

`tests/test_embed.py` — with a `FakeProvider` producing deterministic vectors:

3. **Round-trip** — vectors stored and reloaded are bit-identical float32.
4. **Model isolation** — two models' vectors coexist; a search with model A never
   returns model B's rows.
5. **Dimension mismatch is refused** — storing a wrong-`dim` vector raises.
6. **Cosine top-k is correct** against a hand-computed small matrix.
7. **Search latency** — 20k synthetic vectors, warm cache, under 50 ms for a
   fused query. (Skip with a message on a machine without numpy.)
8. **No provider ⇒ clear failure** — `--semantic` with no provider exits non-zero
   with an actionable message and does **not** return lexical results labelled as
   semantic.
9. **`--deep` with no provider is an error**, not a downgrade.
10. **Lazy import** — importing `muninn.embed` does not import `numpy` or `mlx`;
    assert via `sys.modules` in a subprocess. This keeps the default install
    honest.
11. **Redaction before rerank** — a planted secret never reaches the reranker.
12. **`correlate` excludes the query session** and returns the planted near-twin
    first.
13. **Vectors survive re-chunking** — if a session's chunks are rebuilt, stale
    vectors for removed ordinals are deleted (no orphans).

Also: all prior contract tests pass unmodified; `uv run ruff check` clean; the
**default install still passes the whole suite with no numpy present** — semantic
tests skip cleanly rather than fail.

## Definition of done

```sh
uv run python -m unittest discover tests -v          # default install, semantic skips
uv sync --extra semantic && uv run python -m unittest discover tests -v   # full
uv run ruff check muninn tests tools
uv run muninn search "that time SSE kept dropping"   # fast path
uv run muninn doctor                                 # reports vector count + memory
```

Commit; do not push.

## Guardrails

- **No vector database, no ANN index.** 1.9 ms at 60k × 1024 is the measurement.
- **No new default dependency.** numpy and MLX live in `[semantic]` only, imported
  lazily.
- **Never silently fall back** from semantic to lexical while claiming semantic.
- **Never mix embedding models** in one search.
- **Every model call routes through `policy.check()`.**
- **Do not** implement the Bedrock provider here.
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
- If RRF's `k` or the fusion weighting seems wrong for real queries, **report the
  observation with examples** rather than tuning constants blindly.
