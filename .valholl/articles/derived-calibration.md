---
type: "Knowledge Article"
title: "Derived calibration, not hard-coded thresholds"
description: "Muninn surveys the present corpus to derive its own thresholds, and re-surveys when doctor detects drift."
tags: ["calibration", "survey", "doctor", "configuration"]
timestamp: "2026-07-30T00:00:00Z"
category: "calibration"
status: "current"
updated: "2026-07-30"
summary: "Thresholds tuned to one developer's corpus do not generalize; the same 300-word gate selected 37% of sessions on one source and 91% on another. muninn survey derives thresholds from the live corpus into an inspectable calibration.json, and doctor recommends re-surveying on drift."
related: ["provenance-classification", "corpus-measurements", "lessons-for-huginn"]
---

# Derived calibration, not hard-coded thresholds

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
  sessions covering 85% of text"), which yields a threshold rather than being one
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

## Re-survey on drift

The survey is not one-shot. `muninn doctor` recommends re-running when:

- p95 query latency regresses past a threshold
- corpus has grown ≥2x since the last calibration
- source mix shifts materially (e.g. a user moves from Claude to Codex)
- the gate now selects a materially different fraction than its coverage intent
- **provenance mix changes** — a new tool starts making `claude -p` calls
- index lag exceeds its threshold (see [[continuous-ingest-not-periodic]])

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
