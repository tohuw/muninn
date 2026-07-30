---
type: "Knowledge Article"
title: "Corpus measurements and performance breakpoints"
description: "Measured FTS5, vector-search, and corpus-growth numbers that justify Muninn's retrieval design."
tags: ["performance", "benchmarks", "sqlite", "fts5", "retrieval"]
timestamp: "2026-07-30T00:00:00Z"
category: "performance"
status: "current"
updated: "2026-07-30"
summary: "FTS5 indexes the corpus in under a second with sub-2ms queries; brute-force numpy cosine is ~2ms at 60k chunks, so no vector database is ever needed. Broad OR queries degrade linearly, which is why expansion is capped. Disk is the real long-term constraint."
related: ["derived-calibration", "archive-of-record", "lessons-for-huginn"]
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
