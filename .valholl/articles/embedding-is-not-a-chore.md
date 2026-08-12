---
type: "Knowledge Article"
title: "Embedding is not a chore"
description: "A retrieval step a human has to remember degrades answers silently; the daemon owns embedding for the same reason it owns ingest."
tags: ["retrieval", "embeddings", "daemon", "signals", "cost"]
timestamp: "2026-08-12T00:00:00Z"
category: "ingest"
status: "current"
updated: "2026-08-12"
summary: "Until spec 014, `muninn embed` was a command a human typed, so semantic search silently covered only the part of the archive someone had last remembered to embed — and the uncovered part was always the most recent. The daemon now owns a background worker, gated on a provider being installed, ordered newest-first, with a stall guard because the failure mode of an automatic loop against a metered provider is a bill rather than an error."
related: ["continuous-ingest-not-periodic", "corpus-measurements", "model-policy-chokepoint", "derived-calibration"]
---

# Embedding is not a chore

## The same lesson, one layer up

[[continuous-ingest-not-periodic]] is about a *source* that deletes: a periodic
indexer plus a 30-day sweep gives you "probably captured", and the sessions it
loses are lost silently. Embeddings have no such deadline — prose stays in the
archive and a vector can always be regenerated from it — so nothing here is a
data-loss argument.

The failure is a different one, and it is worse in one specific way: **it does
not look like a failure at all.**

Everything else about retrieval is automatic. The daemon ingests continuously.
FTS5 rows are written on every import, inside the same transaction. `muninn
search` needs no preparation and no warm-up. Semantic search alone had a manual
prerequisite — someone had to run `muninn embed` — and when they had not, the
result was not an error and not an empty result set. It was a *worse ranking*,
returned with exactly as much confidence as a good one.

And the part of the archive that went uncovered was never random. It was always
the newest sessions, because those are the ones added since the last time anyone
remembered. So the query most likely to be answered badly was "what did we decide
last week", which is the query the tool exists for.

This is the shape of every mistake this project keeps re-learning: a guarantee
that depends on a human remembering is not a guarantee, and the version of it
that fails is the version nobody notices failing.

## What was decided

Since docs/specs/014-automatic-embedding.md, `muninn serve` owns a background
worker (`muninn/embedder.py`) that drains the embedding backlog continuously.
`muninn embed` remains, unchanged, as the foreground path: bounded, `--dry-run`
able, and the right thing for a deliberate backfill someone wants to watch.

Both call `embed.pending_chunks` and `embed.store_vectors`. That is not a tidiness
preference — two definitions of "already embedded" would drift, and the direction
drift fails in is a worker that re-embeds rows the CLI thinks are done, which is
money per pass against a hosted provider.

Four decisions inside that are worth the words, because each one is a place a
reasonable implementer would do something else.

### It is gated on a provider being installed, not on a config flag

Automatic embedding is on by default. It also costs the default install exactly
nothing, because the default install ships **no embedding provider** at all
(spec 006: numpy and MLX live in the optional `[semantic]` extra, and a plugin
supplies the hosted ones). `BackgroundEmbedder.start()` returns `False` and logs
at info level.

So the gate is *"did you already opt into embeddings"*, answered by whether a
provider exists, rather than a second flag that means the same thing and can
disagree with the first. Someone who installed a provider and still does not want
the daemon spending it passes `--no-embed`.

### Newest sessions first

The worker orders its backlog by `sessions.started_at` descending; the CLI keeps
id order. This is the one place the two paths deliberately differ, and it is the
difference between the feature working and the feature technically existing.

On a fresh archive the backlog is thousands of chunks — measured at ~7 chunks per
session, so a 1,156-session corpus is ~8,000 ([[corpus-measurements]]). Draining
that in id order means the session someone just finished is embedded *last*,
after everything they have not thought about in months. The promise being made is
"finish a session, it becomes searchable"; newest-first is what makes the promise
true during the hours the backlog takes to drain.

A NULL `started_at` sorts **last**, not first. SQLite sorts NULL lowest so `DESC`
happens to do that already, but the query says it explicitly, because an undated
session is missing data and not evidence of recency, and the accidental version of
that behaviour would flip if the sort direction ever changed.

### The failure classes are handled differently on purpose

Nothing in the worker propagates to the daemon. A daemon that dies because an
embedding provider had a bad afternoon has traded a convenience for the data loss
this project exists to prevent. But "never crash" is not the same as "always
retry", and collapsing the three cases would be wrong in both directions:

| Failure | Handling | Why not the other thing |
|---|---|---|
| No provider installed | Do not start; log at info | It is the default build's normal state, not a warning to train people to ignore |
| `PolicyRefused` | Stop permanently, record the reason | A refusal is a *decision* ([[model-policy-chokepoint]]); retrying cannot change it, and retrying a metered API forever is expensive |
| Anything else | Exponential backoff, keep trying | A laptop that was offline for an hour should catch up by itself |

### The stall guard is about money, not correctness

A pass that writes zero vectors while the backlog is non-empty means something is
wrong that retrying will not fix — a provider returning fewer vectors than texts,
or every row failing `DimensionMismatch`. Correctness-wise this is harmless: no
bad data is written, because `store_vectors` refuses a wrong-length vector rather
than padding it.

Cost-wise it is an unbounded loop calling a paid API forever while the queue never
shrinks. After three consecutive stalled passes the worker stops and says so.

**Stopping visibly is the cheaper failure**, and it is available here in a way it
is not for ingest: a vector can be regenerated from prose at any time, so giving
up costs nothing permanent. That asymmetry is why the ingest loop is written to
survive almost anything and this loop is written to quit.

## What makes it observable

`doctor` prints a **pending** count per model. This is the load-bearing part of
the whole design, for the reason this article opened with: now that embedding is
automatic, *a worker that has stopped and a worker that has finished look
identical from outside.* A backlog that is not shrinking between two `doctor` runs
is the signal, and the daemon's log says which of the three failure classes it
was.

The daemon also announces the backlog size at startup. An automatic process that
spends money must not be silent about how much work it has just decided to do.

## What was not done, and why

- **No wakeup from the ingest loop.** The worker polls every 30 s when idle. A
  cross-thread signal from `indexer.watch` would save at most 30 s of latency on
  "my last session is searchable" and would couple the ingest loop to a feature
  that must never be able to block it.
- **No rate limit or token budget.** It would be a guessed constant, and
  [[derived-calibration]] is this project's standing objection to those. The
  backlog is bounded by the corpus, the startup announcement makes its size
  visible, and `--no-embed` is the off switch.
- **No embedding inside the import transaction.** That is where FTS5 rows are
  written, and it is exactly where a provider round trip must not be: ingest
  never waits on anything else.
