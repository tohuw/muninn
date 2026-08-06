---
type: "Knowledge Article"
title: "Superseding a predecessor: its archive is not one directory"
description: "A verified-lossless backfill can still be an incomplete harvest, because completeness was measured against an assumption instead of against the source."
tags: ["migration", "durability", "ingest", "verification"]
timestamp: "2026-08-06T00:00:00Z"
category: "durability"
status: "current"
updated: "2026-08-06"
summary: "Muninn's first prose-index backfill verified 26.4M words byte-for-byte and was still missing four of six directories, including project memory the vendor had deleted. Byte-level verification proves fidelity of what you read; it says nothing about whether you read everything."
related: ["archive-of-record", "provenance-classification", "deterministic-imports"]
---

# Superseding a predecessor: its archive is not one directory

Muninn supersedes `claudex` and `codexdex`, and their prose indexes hold
transcripts the vendor swept months ago. The backfill (spec 012) was written
carefully, tested against all three known formats, and verified against the real
corpus:

```
prose files discovered : 3,738
  text missing         : 0
  text differs         : 0
words in source files  : 26,420,905
words in archive rows  : 26,420,905
```

Zero missing, zero differing, 26.4 million words matched byte for byte. It was
still missing **four of six directories.**

## What the verification actually proved

It proved that every file *the walker chose to read* round-tripped perfectly. It
proved nothing about the choice.

The walker knew about `index/` and `cloud/index/`. The predecessor had also
grown, over time and without announcement:

| directory | holds | files |
|---|---|---|
| `cloud/projects/index/` | project definitions, memory inlined | 62 |
| `cloud/projects/index-deleted/` | **project memory cleared upstream** | 3 |
| `cloud/memory/index/` | user memory documents | 18 |
| `summaries/` + `cloud/summaries/` | per-session topic/outcome facets | 811 |

`index-deleted` is the one that turns this from tidiness into data loss. Its
manifest says, in as many words, `(project memory — cleared upstream)`. Those
files exist in exactly one place on earth, and it was the place the walker did
not look. A backfill reporting "0 missing" would have preceded archiving the
repository that held them.

## The shape of the mistake

**Completeness was measured against the walker's own list, not against the
source.** The audit asked "did everything I read arrive intact?" — a question
whose answer is always yes when the reader is correct — instead of "does the
archive now hold everything that directory tree holds?"

Those read almost identically in a report. They are not the same question, and
only the second one can fail in the direction that loses data.

The generalisation: **a predecessor's archive is one directory per kind of thing
it learned to archive.** Kinds accrete — a tool gains cloud sync, then projects,
then memory — and nothing in the format announces a new one. Any list of
directories written by reading the code once is a snapshot of what the tool did
*then*.

## What catches it

1. **Enumerate the tree, not your expectations.** `find <root> -type d` before
   writing the walker, and again before believing it. Both of Muninn's audits
   were one shell command.
2. **Verify in the direction that can fail.** Iterate the *source* and assert
   each item is present in the archive. Iterating the archive and finding it
   internally consistent is not verification.
3. **Look for the deleted things first.** A directory whose name contains
   `deleted`, `archive`, `orphan`, or `-old` is where the irreplaceable data is,
   because it is what the upstream no longer has.
4. **Treat derived artifacts as artifacts.** The 811 summaries were the
   predecessor's *enrichment output*. It is tempting to skip them because the
   successor can regenerate its own — but a summary was generated from a
   transcript that may no longer exist, by a model that is no longer the
   default. Regenerating is not recovering.

## Two secondary failures, both from the same root

Harvesting the new kinds surfaced a classifier bug worth recording on its own.
Project and memory entries have no turn counts, and `sources.classify` reads
`user_turns == 0` as the signature of a programmatic `claude -p` call — so 44
projects holding 586,186 words and 18 memory documents were filed as
`tool-invoked`, which is excluded from default search, from every survey
statistic, and from enrichment. **"Has no turns" and "had zero user turns" are
different facts**, and a classifier built for conversations will conflate them
the moment it is handed something that is not one. See
[[provenance-classification]].

The survey's anomaly rule caught it unprompted, reporting both new sources as
100% tool-invoked with zero human sessions — the second time that rule has found
a real defect nobody was looking for.

Separately, scoping the summary harvest to sessions the backfill *added* dropped
705 of 811. When a vendor export supplies a richer copy of a conversation, its
prose entry is correctly skipped as `superseded-by-richer-origin` — but the
summary is superseded by nothing, because the export contains no summaries at
all. **Precedence is per-artifact, not per-session.**

## The rule

> Byte-level verification proves fidelity. It does not prove coverage. Before
> retiring a predecessor, enumerate what it holds and verify from *its* side —
> and assume the list of things it holds is longer than the last time anyone
> looked.
