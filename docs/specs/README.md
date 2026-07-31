# Specs

Implementation specs for Muninn. Each is self-contained enough to hand to an
implementer, and each names the wiki articles that carry the reasoning behind it.

The division of labour is deliberate: **the wiki says *why*, the spec says
*what*, the tests say *whether*.** If a spec and a wiki article disagree, the
wiki wins and the contradiction is a bug worth reporting.

| Spec | Status | Depends on |
|---|---|---|
| [001 — Import ledger](001-import-ledger.md) | ready | — |
| [002 — Export importers](002-export-importers.md) | ready | 001 |
| [003 — Background indexer](003-background-indexer.md) | ready | 001 |
| [004 — Structured filters](004-structured-filters.md) | ready | — |
| [005 — Enrichment](005-enrichment.md) | ready | 001, 004 |
| [006 — Hybrid retrieval](006-hybrid-retrieval.md) | ready | 004, 005 |
| [007 — Tiered retention](007-tiered-retention.md) | ready | 001, 005 |

Specs 002 and 003 both modify `muninn/cli.py`, as do 004 and 005. Run
overlapping specs sequentially, or in separate git worktrees, so they cannot
collide.

Spec 007 deletes data on purpose and is the most dangerous of the set. Its one
inviolable rule: never drop prose for a session whose raw source is already gone.

Later phases not yet spec'd: the console and shared menubar, the agent skill, and
the Cisco distribution's plugins.

## How to work one of these

1. Read the wiki articles listed at the top of the spec first. They exist because
   the reasoning is not re-derivable from the code, and several of them record
   measurements that contradict reasonable assumptions.
2. Follow the build order. Each step ends with a green test run.
3. Treat the acceptance criteria as the definition of done — one test per
   invariant, no exceptions folded together.
4. Existing test files named as contracts (`test_losslessness.py`,
   `test_ledger.py`) must pass **unmodified**. If you believe one is wrong, stop
   and say so rather than editing it. They encode guarantees about data that
   cannot be recovered if lost.
5. If an invariant seems to be blocking a test from passing, that is a finding,
   not an obstacle to route around. Report it.

## Why the guardrails are worded so strictly

Muninn is an archive of record. Claude Code deletes transcripts after
`cleanupPeriodDays` (default 30), so for much of a corpus this archive is the
only surviving copy. A subtle ingest bug does not corrupt data you can re-derive
— it destroys the only copy, silently, and the loss is discovered months later
when someone searches for something that should be there.

That is also why several specs ask for enumerated lists where a count would be
simpler: a count cannot be audited after the fact, and every silent skip in the
predecessor tools was a data-loss path nobody noticed.
