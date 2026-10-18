---
type: "Knowledge Article"
title: "Embedding the whole chunk"
description: "Truncating a chunk at the model's context limit left 7.9% of the archive unreachable to semantic search, concentrated in the code-heavy chunks; why windowing fixes it, why each window is stored as its own vector, and why the vector space had to be renamed twice."
tags: ["retrieval", "embeddings", "chunking", "measurement"]
timestamp: "2026-10-18T00:00:00Z"
category: "retrieval"
status: "current"
updated: "2026-10-18"
summary: "A chunk longer than the embedding model's 512-token window had its tail discarded. The chunker's 80-word overlap hid this for prose but not for code, which tokenises three times denser, so 7.9% of all prose was unreachable to semantic search and the worst-covered chunks were the most code-heavy ones. Chunks are now embedded as overlapping windows, each stored as its own vector so the existing max-over-chunks ranking picks the window that matches. The first version averaged the windows instead, as a deliberate compromise to avoid rebuilding the table; it lasted one day, and the article keeps both halves because the ordering — fix the guaranteed miss first, record the ranking risk, then go back for it — is the transferable part."
related: ["embedding-is-not-a-chore", "corpus-measurements", "derived-calibration"]
---

# Embedding the whole chunk

## The bug nobody could see

`muninn search --semantic` could not find text that was in the archive. Not
ranked badly — **absent**, with no error and no signal anywhere that a chunk
had been only partly read.

`store.chunk_text` emits 400-word windows. `bge-small` accepts 512 tokens.
Those two numbers were chosen independently, years apart in design time, and
nobody put them next to each other until a memory investigation happened to
tokenise a sample of real chunks:

| | words | tokens |
|---|---|---|
| median | 400 | **540** |
| p75 | — | 565 |
| p95 | — | 617 |

**85% of chunks exceeded the window.** The provider truncated each one and
embedded the prefix, which is the documented behaviour of every BERT-family
embedder and looks entirely correct in code review.

## Why the overlap was not the answer, though it nearly was

The first reaction — mine — was that this must be harmless, because
`chunk_text` already overlaps: 400-word windows on a 320-word stride, so
consecutive chunks share 80 words. A chunk losing ~21 words off its tail loses
them to a *neighbour that embeds them anyway*.

That reasoning is correct and it is also not enough, which measurement showed
and intuition did not:

```
truncated chunks:   6,403
  tail covered by a later chunk's embedded prefix:  4,698 (73.4%)
  tail genuinely unreachable:                       1,705 (26.6%)
words unreachable:  223,618 of 2,827,499 sampled   (7.9%)
```

The missing step is **tokens per word, which is not a constant.** English prose
runs about 1.35 tokens per word, so a 400-word prose chunk is ~540 tokens and
loses ~21 words — comfortably inside an 80-word overlap. Code, JSON, stack
traces and file paths run closer to **3 tokens per word**, so a 400-word chunk
of them is ~1,200 tokens and only its first ~170 words fit. That is a 230-word
hole, and an 80-word overlap cannot bridge it.

So the losses were not spread evenly across the corpus. **They concentrated in
exactly the chunks a coding-agent archive exists to hold.** The archive's whole
purpose is finding the session where something technical happened, and the
technical content was the content being dropped.

This is the second time this project has been caught by a uniform assumption
over a non-uniform corpus; [[provenance-classification]] records the first,
where pooling provenance classes produced a 40x error. The pattern is worth
naming: **an average over a corpus whose composition varies is a measurement of
nothing in particular.**

## What was done

`embed_onnx.embed` now tokenises without discarding the overflow. A text longer
than the window becomes several windows overlapping by `WINDOW_STRIDE` tokens,
each embedded, and the row's vector is their mean.

Three things about that are deliberate and each could reasonably have gone the
other way.

### The mean was a compromise, and it lasted one day

The first version averaged a chunk's windows into one vector, and that argued
against this project's own rule. `embed.search_sessions` is explicit that a
session scores as its **best** chunk and never its mean, because averaging lets
fifty irrelevant chunks dilute one exact hit. That argument applies one level
down, to windows within a chunk — weaker, since these are two or three adjacent
slices of a single 400-word passage rather than fifty unrelated paragraphs, but
not wrong.

It was taken anyway, because `chunk_vectors` was keyed
`(session_id, ordinal, model)`, which collapses a chunk's windows into one row,
and extending a primary key in SQLite means rebuilding the table. The ordering
principle is the transferable part: **unreachable text is a guaranteed miss;
dilution is a ranking risk.** Fix the certainty first, record the risk, and do
not hold the certain fix hostage to the better architecture.

Then the better architecture shipped, the same day, as its own change — which is
the point of separating them rather than an argument that the separation was
pointless. `window` joined the primary key (schema v4) and each window is now
its own row.

**The dilution was not hypothetical, and the test says so by arithmetic.**
`tests/test_embed_windows.py::DilutionTest` builds two sessions whose ranking
*inverts* between the two designs, with vectors written down so the expected
order is checkable by hand:

| session | windows | best | mean |
|---|---|---|---|
| `long` | `[0,1,0,0]`, `[1,0,0,0]` | **1.00** | 0.707 |
| `short` | `[0.8,0.6,0,0]` | 0.80 | **0.80** |

Averaging `long`'s two windows gives `[0.5,0.5,0,0]`, which normalises to
`[0.707,0.707,0,0]` — so the session that *literally contains the query* loses
to one that merely resembles it. The test asserts both directions: that
per-window storage ranks `long` first, and that storing the mean ranks `short`
first. Without the second assertion the first could pass for the wrong reason
and nothing would show the change was needed.

Two consequences worth having written down:

- **`search_sessions` did not change at all.** The max over a session's vectors
  was always the right operation; it simply had nothing to choose between. The
  better design here was not new logic, it was giving existing logic its input —
  which is usually the cheaper half of a fix and almost never the obvious one.
- **`correlate` changed without being touched**, and for the better. It takes a
  session's mean vector, which is now a mean over windows rather than over
  chunks, making it length-weighted: a dense 1,200-token chunk now counts three
  times as much as a short one. Per-chunk equal weighting was an artefact of the
  storage shape and not a decision anyone had made. The equal-weighting problem
  the mean design had — a short final window pulling as hard as a full one —
  disappears at query time for the same reason.

### The windowing recipe is part of the vectors' identity

Two providers can load byte-identical weights and produce vectors that must
never be compared, which is precisely what happened here: the same model, the
same dimension, one reading a chunk's first 512 tokens and one reading all of
it.

`chunk_vectors.model` had been carrying the identity of the vector space on the
assumption that the weights determine the vector. They do not — the *recipe*
does. So a provider may now declare a `space`, and `embed_onnx` derives it from
its own constants (`BAAI/bge-small-en-v1.5+w512s64win`), which means changing
either constant invalidates the stored vectors instead of silently mixing two
spaces. `embed.space_of` falls back to the model id, so a plugin provider
written against the three-attribute protocol keeps working untouched.

**The pooling is part of the recipe too, and that is not obvious.** Going from
one mean vector per chunk to one vector per window changed no constant: same
weights, same window size, same stride. The space name still had to change,
because the numbers under the key are different — so the suffix went from
`mean` to `win`, and a test spells the old name out from the current constants
to catch a revert that leaves the storage per-window. A derived-from-constants
scheme does not catch a change that is not in the constants, which is the
general version of the lesson.

The cost is honest and was accepted twice: **the whole archive re-embeds**,
~150,000 chunks, which the background worker drains by itself. `doctor` shows
both spaces while it runs and says the older one is dead weight, which is the
behaviour its "half-finished re-embed" line was already written for. The `mean`
space is the exception — it is deleted by the v4 migration rather than carried
forward, because no build carrying that migration can produce or read that name,
so the rows are unreadable *by construction* and nothing in the tool could ever
remove them. A space that merely went stale would be left alone for the operator
to judge.

### MLX was left alone

`embed_mlx` delegates to `mlx_embeddings.generate`, which truncates internally,
so Apple silicon very likely has the same defect. It is not fixed here because
it cannot be *measured* here, and this article exists because the last
confident unmeasured claim about this code path was wrong. The two providers
already keep separate model ids, so no mixing can occur in the meantime —
a machine that switches platforms re-embeds either way.

## What this says about the tests that passed

Every embedding test passed throughout. They asserted shape, dtype,
normalisation, that related text outranks unrelated text, and that batching
does not change vectors — all true, all passing, none of them able to notice
that 7.9% of the corpus was invisible.

The test that now pins it does not assert *how* embedding works. It builds two
texts sharing a filler prefix that alone exceeds the window, differing only in
a sentence appended after it, and asserts the one whose tail matches the query
scores higher. Under truncation those two vectors were byte-identical, so the
assertion was unsatisfiable by any amount of tuning — which is the property a
regression test wants. See [[corpus-measurements]] for the numbers and the
method, and the same lesson stated from the other direction in
docs/specs/021: a failure mode nothing asserts is indistinguishable from
working.
