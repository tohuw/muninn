---
type: "Knowledge Article"
title: "The index is an archive of record"
description: "Claude Code deletes transcripts after cleanupPeriodDays; Muninn's index is often the only surviving copy."
tags: ["retention", "durability", "ingest"]
timestamp: "2026-07-30T00:00:00Z"
category: "durability"
status: "current"
updated: "2026-07-30"
summary: "Claude Code sweeps session JSONLs older than cleanupPeriodDays (default 30) on startup. Muninn's index therefore holds data that exists nowhere else, which makes losslessness and continuous ingest hard requirements."
related: ["what-muninn-is", "continuous-ingest-not-periodic", "unstable-jsonl-format"]
---

# The index is an archive of record

Claude Code deletes local session transcripts on startup once they are older
than `cleanupPeriodDays` (**default 30 days**, minimum 1, cannot be disabled).
Subagent transcripts are deleted *with* their parent session.

Sources: [claude-directory](https://code.claude.com/docs/en/claude-directory.md),
[sessions](https://code.claude.com/docs/en/sessions.md).

## Measured evidence (2026-07-30)

On the development machine, transcript age distribution showed a hard cliff
exactly at the documented boundary:

```
 0-9 days old: 3,760 files
10-19 days:      109
20-29 days:       44
30+ days:          0      <- nothing survives
```

Oldest surviving transcript was 2026-07-01; the survey ran on 2026-07-30. All
June history had already been swept from `~/.claude/projects`. It survived only
because `claudex` had archived it.

## Consequences for design

- **Losslessness is a test, not an aspiration.** The migration/ingest
  losslessness test is written *before* the ingest code.
- **Tiered retention must never drop the last surviving copy** of a pruned
  transcript. That constraint outranks any byte budget.
- **Ingest must be continuous.** See [[continuous-ingest-not-periodic]].
- Raising `cleanupPeriodDays` buys time but is not a fix: it only protects data
  created *after* the change, and unbounded retention has a real disk cost
  (~452 MB per 30 days observed, on a machine with 5.9 GB free).

## What survives the sweep

`~/.claude/history.jsonl` is kept indefinitely, but holds only *your* prompts —
no assistant responses. It is a weak fallback, not an archive.

Codex (`~/.codex/sessions`) showed sessions back to 2026-06-08, suggesting no
equivalent 30-day sweep. This is **unverified** — no official documentation was
found. Do not rely on it.
