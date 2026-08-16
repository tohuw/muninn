---
type: "Knowledge Article"
title: "Derived calibration, not hard-coded thresholds"
description: "Muninn surveys the present corpus to derive its own thresholds, and re-surveys when doctor detects drift."
tags: ["calibration", "survey", "doctor", "configuration"]
timestamp: "2026-07-30T00:00:00Z"
category: "calibration"
status: "current"
updated: "2026-08-16"
summary: "Thresholds tuned to one developer's corpus do not generalize; the same 300-word gate selected 37% of sessions on one source and 91% on another. muninn survey derives thresholds from the live corpus into an inspectable calibration.json, and doctor recommends re-surveying on drift."
related: ["provenance-classification", "corpus-measurements", "lessons-for-huginn", "retrieval-that-is-not-asked"]
---

# Derived calibration, not hard-coded thresholds

> **Amended 2026-08-16.** Everything below about *deriving rather than guessing*
> stands. What changed is that enrichment stopped using the derived gate to
> choose sessions — the argument was right and the axis was wrong. See
> [The axis was wrong](#the-axis-was-wrong) at the end.

Muninn hard-codes no corpus thresholds. `muninn survey` measures the *present*
corpus and writes `calibration.json`; everything downstream reads it.

## Why

A fixed threshold encodes one person's habits as everyone's defaults. Concretely,
during design a proposed "enrich sessions ≥300 words" gate selected **37% of
Claude sessions but 91% of Codex sessions** — the same constant meaning two
completely different policies depending on which agent someone favors. A
Codex-primary developer, a light user, and someone doing 5x the volume all land
in different places.

Worse, the 300-word figure was itself partly an artifact: it was derived from a
median contaminated by tool-invoked sessions whose word count was mostly the
index file's own header. The constant measured the *file format*, not behavior.
See [[provenance-classification]].

## The artifact

`muninn survey` emits a versioned, human-readable `calibration.json`:

- **Provenance breakdown** per source and class
- **Distributions** (median, p75, p90, p99) for words and turns, human-only
- **Derived enrich gate** — expressed as *coverage intent* ("smallest set of
  sessions covering 85% of text"), which yields a threshold rather than being one.
  Since v2026.08.16.10 this **describes** the corpus rather than selecting from
  it; see "The axis was wrong" below
- **Chunk strategy and target size**
- **Anomalies** — plain-language warnings about the corpus itself

Because it is a file, it is inspectable, diffable, version-controllable, and
reviewable. Calibration stops being a magic number in source and becomes
evidence.

## Measured output (dev machine, 2026-07-30)

Surveying the **raw transcripts** (authoritative):

```
[claude] 115 conversations, 683,447 words
   enrich gate  >= 4,046 words -> 45 sessions (39.1% of convos, 85.0% of text)
[codex]  168 conversations, 790,528 words
   enrich gate  >= 2,480 words -> 61 sessions (36.3% of convos, 85.1% of text)
```

Note that the *derived* gates differ by ~1.6x between sources while landing on
nearly identical coverage — exactly what a fixed constant could not have done.

## Never calibrate from a derived index

An earlier prototype surveyed the **claudex/codexdex prose indexes** instead of
the raw transcripts, and undercounted conversations badly:

| | via prose index | via raw JSONL | missed |
|---|---|---|---|
| claude conversations | 84 | 115 | 27% |
| codex conversations | 142 | 168 | 15% |
| claude gate | ≥5,724 words | ≥4,046 words | 41% too high |
| codex gate | ≥3,053 words | ≥2,480 words | 23% too high |

The cause was **index staleness**, not a parsing difference: the claudex index
was last built 2026-07-23 while 149 raw transcripts were newer. A gate derived
from it would have been systematically too strict, under-enriching the corpus.

Rule: **calibrate from the source of truth, never from a derived artifact** —
and report index lag so staleness is visible rather than silently biasing the
result. See [[continuous-ingest-not-periodic]].

### The rule is about staleness, not indirection

`muninn survey` as implemented surveys **the archive**, which is itself derived,
and that is not a violation. The prose-index failure above was not caused by
indirection: it was caused by the index being seven days behind while 149 newer
transcripts sat unindexed. Muninn's archive is ingested *from* raw transcripts,
holds sessions whose raw files have since been swept, and is already
provenance-classified and deduplicated — a better input than a re-walk, and a far
faster one.

What it can still be is behind. So index lag is measured on every survey and
recorded **inside `calibration.json`**, raised as an anomaly there. Staleness
travels attached to the number it invalidates rather than filed in a separate
report nobody reads at the same moment.

## Re-survey on drift

The survey is not one-shot. `muninn doctor` recommends re-running when:

- p95 query latency regresses past a threshold — **not implemented**; it needs a
  benchmark harness rather than a query, and a silently missing check would be
  worse than an admitted one
- corpus has grown ≥2x since the last calibration
- source mix shifts materially (e.g. a user moves from Claude to Codex)
- the gate now selects a materially different fraction than its coverage intent
- **provenance mix changes** — a new tool starts making `claude -p` calls
- index lag exceeds its threshold (see [[continuous-ingest-not-periodic]])

### Compare drift against what the gate *did*, never against its target

Worth recording because the implementation got it wrong first. The gate is the
*smallest* set of conversations reaching the target, so **it always overshoots**:
a lone 5,000-word conversation covers 100% of an 85% target. Checking achieved
coverage against the target therefore reports a correct, freshly written
calibration as already drifted — and does it worst on exactly the small corpora
where a survey is most tentative and its output most likely to be doubted.

The stored calibration records what the gate achieved when it was derived, and
drift is the distance from *that*. Two axes, because they fail independently:

| axis | catches |
|---|---|
| coverage % | the gate no longer covers the text it was derived to cover |
| share of conversations % | coverage is unmoved (~99% either way) while selection went 60% → 97%, so the enrichment cost bound is gone |

A single axis would have missed the second case entirely, which is the more
likely one: adding long conversations barely moves coverage and changes what
enrichment costs completely.

## Validation that this works

The survey prototype flagged, unprompted, that 92% of the corpus was
tool-invoked from a single directory — the precise contamination that had already
produced a 40x error in hand analysis. A design whose first act is to report what
is strange about your data catches errors that careful reasoning did not.

## Honest limits

All measurements to date come from **one user, two months, one machine**. That is
sufficient to establish orders of magnitude and reject bad designs (FTS5 is fast;
vector search needs no index; disk is the eventual constraint) and insufficient
to calibrate defaults. Hence: derive, report, re-derive.


## The axis was wrong

This article won its argument. A hard-coded 300-word gate really did mean two
different policies on two sources, and deriving the number really was better
than guessing it. The gate that replaced it was correct in the way this article
claims — and it was still selecting on the wrong thing.

**Word-coverage is a budget rule wearing a relevance rule's clothes.** "The
smallest set of sessions covering 85% of the text" is exactly right if the
question is *how do I spend a fixed number of tokens?* Enrichment cost scales
with session length, so the longest sessions are both the most expensive and the
fastest way to accumulate coverage. The rule buys coverage-of-words efficiently.

Nobody wants coverage of words. What enrichment produces is `topic`, `outcome`
and `summary` — the fields that make a session **findable**, and the only ones
that identify a session by something you cannot see from the outside. And
findability is per *conversation*, not per word.

Measured on a real 2,163-session archive, the two diverge violently:

| | sessions | words | input tokens | share of spend |
|---|---|---|---|---|
| above the gate | 152 | 16.6M | 33.6M | ~80% |
| below the gate | **687** | 2.9M | 5.9M | ~20% |

85% of the words is 18% of the conversations. The 82% it declines are the
*cheapest sessions in the corpus* — the gate spends its budget on the expensive
ones and skips the ones that cost a single model call each. Adding all 687 costs
about a quarter again of what the 152 cost.

The error underneath is that **length was standing in for value**, and it is a
bad proxy at the short end. A ten-turn session that resolved something is small,
cheap to summarise, and precisely the session a person fails to remember a month
later — which is the moment an archive is supposed to earn its keep. A session
with no facets cannot be found by subject, cannot be filtered by `--outcome`,
and can never appear in [[retrieval-that-is-not-asked]]'s unfinished list. The
gate was quietly deciding that short work was not worth being able to find.

So enrichment now selects on a **floor**: enough words that "what happened here?"
has an answer, and both sides having spoken. That is deliberately mechanical.
It does not estimate importance, because nothing available can — which is the
whole lesson, one level up from where this article originally applied it. The
derived gate is still computed and reported, because *"the longest 18% of your
sessions hold 85% of your words"* is a true and useful fact about a corpus. It
is simply a description, not a rule.

**The generalisable form:** deriving a threshold from data protects you from
someone else's habits. It does not protect you from measuring the wrong
quantity. Ask what the threshold is *for* before asking what its value should
be — a gate tuned to three significant figures on the wrong axis is still on the
wrong axis.
