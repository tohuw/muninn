---
type: "Knowledge Article"
title: "Corpus measurements and performance breakpoints"
description: "Measured FTS5, vector-search, embedding-memory and corpus-growth numbers that justify Muninn's retrieval design."
tags: ["performance", "benchmarks", "sqlite", "fts5", "retrieval", "memory", "embeddings"]
timestamp: "2026-07-30T00:00:00Z"
category: "performance"
status: "current"
updated: "2026-10-15"
summary: "FTS5 indexes the corpus in under a second with sub-2ms queries; brute-force numpy cosine is ~2ms at 60k chunks, so no vector database is ever needed. Broad OR queries degrade linearly, which is why expansion is capped. Disk is the real long-term constraint. Embedding is the memory constraint rather than the CPU one: 85% of chunks reach the 512-token cap, so one forward pass sized the daemon's resident footprint at 2.1 GB until the ONNX arena was disabled and the batch cut to 8."
related: ["derived-calibration", "archive-of-record", "lessons-for-huginn", "embedding-is-not-a-chore"]
---

# Corpus measurements and performance breakpoints

All figures measured 2026-07-30 on an Apple M4 Pro / 26 GB, Python 3.13,
SQLite 3.53.3, numpy 2.4.1.

## Lexical search (SQLite FTS5, stdlib)

Real corpus (3,836 Claude prose files, 1.88M words, 8,093 chunks):

- Index build: **0.8 s**
- Database: **33 MB**
- Queries: **0.1–1.9 ms**

Scaling on replicated real prose (not synthetic — see pitfall below):

| chunks | ~sessions | db size | phrase query | broad OR query |
|---|---|---|---|---|
| 8,093 | 1,156 | 33 MB | 0.1 ms | 1.9 ms |
| 32,372 | 4,624 | 130 MB | 0.2 ms | 7.8 ms |
| 80,930 | 11,561 | 323 MB | 0.4 ms | 22.1 ms |
| 161,860 | 23,122 | 645 MB | 0.7 ms | 45.5 ms |

`INSERT INTO chunks(chunks) VALUES('optimize')` took 2 s at 162k chunks.

**Findings:**
- Phrase and single-term queries stay effectively flat. FTS5 is not the
  bottleneck at any plausible scale.
- **Broad `OR` queries degrade linearly** (1.9 → 45.5 ms). This is why query
  expansion is **capped at ~4 terms**; the naive "expand to 8 synonyms" approach
  is precisely what degrades.
- **Disk is the real constraint**, not CPU: ~645 MB per 23k sessions. Hence
  tiered retention.

## Vector search (brute-force numpy)

| chunks | dims | latency | memory |
|---|---|---|---|
| 25,000 | 768 | 0.7 ms | 77 MB |
| 60,000 | 768 | 1.4 ms | 184 MB |
| 60,000 | 1024 | 1.9 ms | 246 MB |

**Finding: Muninn never needs a vector database, HNSW, or faiss.** A matrix
multiply plus `argpartition` is ~2 ms at well beyond current scale. The entire
cost of "semantic" is *generating* embeddings once, not searching them.

## Embedding memory

Measured 2026-10-15 on a 32-core Windows 11 host, Python 3.13.15,
onnxruntime 1.28.0, numpy 2.5.1, `BAAI/bge-small-en-v1.5` via
`muninn/embed_onnx.py`. These are a different machine and date from the figures
above; do not read them against the Apple numbers.

The occasion was a live `muninn serve` found holding **1.49 GB resident and
3.13 GB committed** three days after its embedding backlog went empty. Huginn's
daemon beside it held 51 MB. The difference is that Muninn's daemon owns
`BackgroundEmbedder`, so onnxruntime and the model session live in its process.

### Chunks reach the 512-token cap; they do not sit below it

A 2,000-chunk sample of the real archive, tokenised with the model's own
tokenizer:

| | words | tokens |
|---|---|---|
| mean | 397 | — |
| median | 400 | 540 |
| p25 / p75 | — | 520 / 565 |
| p95 / max | — | 617 / 839 |

**85% of chunks are at or over `MAX_TOKENS = 512`.** This is structural, not a
tail: `store.DEFAULT_CHUNK_WORDS` is 400 and English prose runs ~1.35 tokens
per word, so a full-width chunk cannot fit. Padding is to the longest row in
the batch, so *the padded length is effectively the constant 512* and the batch
size is the only free variable.

Two consequences, and only the first was acted on:

1. Attention tensors are quadratic in sequence length, so `BATCH` alone sizes
   them: `batch x 12 heads x 512 x 512 x 4 B` is 100 MB at 8 and 400 MB at 32,
   with several live at once.
2. **Every full-width chunk loses its tail to truncation** — roughly the last
   30 words of 400 are not in the vector. Recorded here rather than fixed:
   widening the window is a re-embed of the whole archive, and it is a
   retrieval-quality decision, not a memory one.

### One pass sets the footprint for the life of the process

64 rows at the cap, measured as resident / committed after the pass:

| config | resident | committed | 64 rows |
|---|---|---|---|
| batch 32, arena on *(was)* | 2,110 MB | 3,206 MB | 2.79 s |
| batch 8, arena on | 674 MB | 1,506 MB | 2.80 s |
| batch 4, arena on | 448 MB | 1,220 MB | 2.35 s |
| batch 32, arena off | 241 MB | 956 MB | 4.32 s |
| **batch 8, arena off** *(is)* | **231 MB** | **944 MB** | **3.08 s** |

The reproduction landed within 80 MB of the live daemon's committed figure,
which is what identified it.

### The 64-row timings above overstate the cost; ignore them

Those seconds-per-64-rows are the microbenchmark that was to hand, and taken at
face value they say the fix costs ~10% throughput. Rerun as a corpus pass —
3,395 real chunk bodies, fed 64 at a time exactly as `BackgroundEmbedder` does
— `batch 8, arena off` finished in **137 s, resident 265 MB, peak 541 MB**,
against the **138 s** already on record in README.md for `batch 32, arena on`.

**Finding: the memory fix is free.** A 64-row run is mostly the arena's first
allocation, so it measures the thing being removed rather than the inference
being done. The published throughput figure needed no revision, which is the
only reason it is worth writing this down: a plausible 10% regression was about
to be documented, and the number that disproved it cost one 137-second run.

**Finding: this was never a leak.** A second pass did not grow the process
(2,109 → 2,104 MB), and a subsequent pass of *short* rows did not shrink it.
ONNX Runtime's arena allocator is honouring a high-water mark and by design
never returns it to the OS. In a batch process that is free. In a daemon that
lives for weeks and embeds for minutes of that, the peak **is** the
steady-state footprint, and no amount of idling recovers it.

`enable_cpu_mem_arena = False` with `BATCH = 8` holds the process at ~230 MB at
no measurable throughput cost (see the section below). The 1.9 GB
is bounded in `tests/test_embed_onnx.py::FootprintTest`, which asserts the
arena flag and the batch arithmetic without the optional runtime installed,
because the build where this regresses is the build that cannot measure it.

The general form, for whichever provider is written here next: **a default
tuned for a batch job is the wrong default inside a daemon**, and for an arena
allocator peak memory is the only number worth measuring, because it is also
the resting number.

## Corpus shape

Human conversations only (see [[provenance-classification]]):

| | Claude Code | Codex |
|---|---|---|
| conversations | 84 | 142 |
| words | 630,643 | 779,669 |
| median words | 3,125 | 1,698 |
| p90 / p99 | 16,899 / 51,613 | 12,410 / 90,327 |
| median user turns | 8 | 4 |

Codex compresses **338 MB of raw rollouts to 6.8 MB of prose (2% retained)** —
rollouts are overwhelmingly tool traffic and reasoning. Claude Code's raw
`~/.claude/projects` was 452 MB for 30 days of retained sessions.

## Methodological pitfalls (recorded so they are not repeated)

1. **Do not benchmark FTS5 with random vocabulary.** A first attempt using
   random tokens built a pathological inverted index that reached 6.3 GB and had
   to be killed. Real prose is far more repetitive. Always benchmark on
   replicated real text.
2. **Do not derive session dates from file mtime.** The claudex index was
   rebuilt wholesale, so every file's mtime showed one month, making a
   multi-month corpus look like a single month's output. Parse timestamps from
   the transcript content instead.
3. **Do not pool provenance classes.** See [[provenance-classification]] for the
   40x error this caused.
4. **Do not pool subagent transcripts with top-level sessions.** They are a
   distinct population (median 1,684 words vs. far shorter tool calls).
5. **Do not benchmark an embedder with short strings.** `embed(["one", "two"])`
   allocates almost nothing and passes identically with the arena on or off,
   which is why the 2.1 GB above survived a test suite that exercised the
   provider thoroughly. Use rows at `MAX_TOKENS`, which the measurement above
   shows is what the real corpus sends anyway.
6. **Measure a daemon's memory where it rests, not where it starts.** Every
   figure in the embedding section is invisible at startup: the process loads
   at 215 MB and the cost appears only after the first real batch, then never
   leaves. A check at launch would have reported it healthy.
7. **Do not price a change from a microbenchmark of the thing it removes.** The
   64-row runs said the memory fix cost 10% throughput. It costs nothing at
   corpus scale; the short run was timing the arena's first allocation. The
   same shape as pitfall 1 — a benchmark whose workload is unrepresentative
   does not return a noisy answer, it returns a confident wrong one.
