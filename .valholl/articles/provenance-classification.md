---
type: "Knowledge Article"
title: "Provenance classification"
description: "Sessions are classified human / tool-invoked / subagent at ingest; conflating them skewed every statistic by ~40x."
tags: ["ingest", "data-model", "calibration", "pitfalls"]
timestamp: "2026-07-30T00:00:00Z"
category: "data-model"
status: "current"
updated: "2026-07-30"
summary: "92% of indexed Claude sessions on the dev machine were Huginn's own claude -p blurb calls, not conversations. Treating them as sessions made the corpus look 40x larger and the median session 16x shorter. Provenance is therefore a first-class structural dimension, not a length heuristic."
related: ["derived-calibration", "what-muninn-is", "lessons-for-huginn"]
---

# Provenance classification

Not every entry in `~/.claude/projects` is a conversation. Many are programmatic
`claude -p` invocations made *by tools* — including Huginn itself, generating
session blurbs.

## The measurement that forced this

Survey of the development machine, 2026-07-30:

Measured from **raw transcripts** (3,915 Claude files + 175 Codex rollouts):

| source | class | sessions |
|---|---|---|
| claude | human | 115 |
| claude | tool-invoked | **3,549** |
| claude | subagent | 251 |
| codex | human | 168 |
| codex | tool-invoked | 7 |

**92% of Claude "sessions" were tool-invoked**, 3,534 of them from a single
directory: `~/.local/state/huginn/cache`. Most ran in under half a second with
exactly one user turn and one assistant turn.

Conflating these with conversations produced errors of this magnitude:

| Naive reading | Reality |
|---|---|
| ~3,800 sessions/month | ~115 human conversations/month |
| median session 197 words | median conversation ~3,100 words (claude) |
| claude 96% / codex 4% of corpus | 115 vs. 168 conversations — codex is *larger* |
| disk crisis from index growth | real corpus ~29 MB |

Every downstream estimate — LLM spend, growth rate, retention urgency, chunk
counts — was wrong in the same direction until provenance was separated.

## The classifier

Structural signals only, never body length:

- **subagent** — filename/path marks it as a subagent transcript. A distinct
  population (median 1,684 words) that must not be pooled with either other class.
- **tool-invoked** — `cwd` under a state/cache directory
  (`/.local/state/`, `/.cache/`, `/Library/Caches/`, `/.thlibo/`), OR zero user
  turns, OR a single user turn that completed in under 2 seconds.
- **human** — everything else.

Validation on the dev corpus: **0 false negatives** (no tool-invoked session had
≥3 user turns) and 5 borderline false positives (short single-turn human
sessions in project directories — correctly classified as human; they are real
if trivial).

Claude Code's own `entrypoint: sdk-cli` marker is a stronger signal than any
heuristic and should be preferred where present; Huginn's parser already filters
on it.

## One session id can appear under two project directories

Measured 2026-07-31: **11 of 388 transcripts in the real corpus shared a session
id with another file** under a different encoded `cwd`. For example the same
session id under both:

```
~/.claude/projects/-Users-tohuw-Projects-docsdex/7b01c7f0-….jsonl
~/.claude/projects/-Users-tohuw-Projects-ftd-docsdex/7b01c7f0-….jsonl
```

The cause is a renamed or symlinked repository: Claude Code encodes the working
directory into the project directory name, so one session reachable by two paths
is written under both. Subagent transcripts collide identically.

**This was silent data loss before the ledger existed.** The pre-ledger ingest
had no per-item tracking, so the second file simply upserted over the first and
one of the two sessions vanished with no error and no counter. The ledger's
`UNIQUE(ledger_id, item_id)` constraint is what surfaced it — as a crash, which
is the correct behavior for an archive of record: refusing to proceed is better
than quietly keeping one of two.

The resolution is a `duplicate-item-in-source` skip: first occurrence wins (paths
are iterated in sorted order, so it is deterministic), and the duplicate is
*recorded* rather than dropped. Note that the skip vocabulary already contained
this reason before the condition was observed — enumerating failure modes in
advance turned an incident into a lookup.

**Lesson for tests:** every fixture had unique session ids, so 31 tests passed
while the real corpus crashed on the first run. Fixtures encode what you expect;
only real data encodes what is true. Run new ingest code against the real corpus
before believing it.

## Rules

- Every statistic, gate, rate estimate, and spend projection is **scoped to a
  class**. An unscoped aggregate is a bug.
- **Tool-invoked sessions are never enriched.** Spending an LLM call to
  summarize an LLM call is pure waste.
- Classification is stored, not recomputed at query time, and is exposed as a
  `--provenance` filter.
- **Tool-invoked sessions are not archival.** Unlike human conversations, they
  are reproducible byproducts, and some are outright bug residue: 3,534 of them
  on the development machine came from Huginn writing transcripts into a cache
  directory it then deleted (see [[lessons-for-huginn]] #8). They were deleted
  by hand with no loss. So the archive-of-record guarantee applies to human and
  subagent sessions; tool-invoked rows are prunable, and their prose need not be
  retained at all once counted. This keeps the archive's growth proportional to
  actual work rather than to some other tool's call volume.
- The class boundary is *derived and reported*, never silently applied — see
  [[derived-calibration]].
