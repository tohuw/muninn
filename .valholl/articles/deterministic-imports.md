---
type: "Knowledge Article"
title: "Deterministic imports: receipts, not counters"
description: "Import results must be self-evident claims backed by a ledger, not store-relative counters an agent has to interpret."
tags: ["ingest", "imports", "determinism", "provenance", "agents", "idempotency"]
timestamp: "2026-07-31T00:00:00Z"
category: "ingest"
status: "current"
updated: "2026-07-31"
summary: "A claudex incident showed how an idempotent re-import reading '0 written, 61 cached' led an agent to report 'the export contained nothing new' — false, because another actor had imported the same export three minutes earlier and the evidence was overwritten. Muninn's import layer must make that misreading structurally impossible: content-addressed imports, an append-only ledger, export-vs-delta reporting, enumerated skips, an import lock, and explicit deletion receipts. Agents transport claims the tool can prove; they never manufacture them."
related: ["archive-of-record", "continuous-ingest-not-periodic", "provenance-classification"]
---

# Deterministic imports: receipts, not counters

Muninn will be operated largely *by agents* — a human says "add this export"
and an agent runs the import and reports back. The import interface is
therefore not just a data path; it is an epistemic boundary. Whatever the tool
prints is what the agent will assert to the human. If the output requires
interpretation, some agent will eventually interpret it wrong, confidently.

## The incident (claudex, 2026-07-30)

Predecessor claudex, real timeline, evening of 2026-07-30 (EDT):

```
21:10:10  claude.ai generates a ~30-day windowed export
21:15:17  actor A ingests it   → new conversations (Jul 28-30) indexed
21:18:22  actor B (an agent, asked by the user) ingests the same export
          → "cloud indexed: 0 written, 61 cached, 4 empty (total 65)"
```

Actor B read "0 written" and told the user **"the export contained nothing
new"** — false. The export carried conversations updated as recently as 39
minutes before it was generated. Three compounding tool defects made the
false claim easy and the true one hard:

1. **Counters were store-relative.** "0 written, 61 cached" conflates two
   very different worlds: *this export contains nothing the store lacks
   because it is genuinely stale* versus *someone imported this exact export
   moments ago*. The output did not distinguish them; the agent guessed.
2. **The ingest record was last-writer-wins.** claudex kept one state entry
   per export name. Actor B's re-run **overwrote actor A's entry**,
   destroying the only direct evidence of the earlier import. Attribution
   was eventually recovered from index-file mtimes — forensics that worked
   by luck.
3. **Skips were counted, not named.** "4 empty" could have been four
   genuinely empty conversations or four voice sessions silently dropped by
   a parser gap. A count cannot be audited; a list can.

A fourth failure belonged to the agent: it had already deleted the export
directory (authorized, but before the claim was challenged), destroying the
source. Verification had to happen against the store alone.

## Design requirements

**Content-addressed imports.** Digest every import source (for exports: a
hash over item-id → updated-at pairs is enough; a file digest also works).
Import is then a pure function of (store, source): re-importing the same
source is detected *by identity*, and the tool can say "duplicate of import
`#14`" instead of emitting counters. The incident's misreading becomes
structurally impossible — the second run would have printed *"this exact
export was imported at 21:15:17 by actor A."*

**Append-only import ledger.** Every import attempt appends a row: actor,
timestamp, source digest, outcome, delta, skip list. Re-runs reference the
original row; nothing is ever overwritten. The 21:15 mystery becomes one
ledger read. This also gives the corpus provenance a spine — see
[provenance-classification](provenance-classification.md).

**Report the source and the delta separately.** Two sections, never merged:

- *What this source contains*: item count, time span, newest item timestamp,
  window characterization (claude.ai exports may cover only ~30 days —
  absence from a windowed export proves nothing about upstream deletion).
- *What this run changed*: items added/updated, and for the overlap,
  attribution ("previously imported by ledger `#14`").

"Nothing new in this run" and "nothing new in this source" are different
facts. The output must never let one masquerade as the other.

**Structured outcome for agents.** Alongside human prose, a machine-readable
result with an explicit enum — `imported | duplicate-of:<ledger-id> |
partial | rejected` — plus digest, delta, and attribution. Agents parse and
relay; they do not interpret prose.

**Enumerate skips.** Every skipped item appears with its id and a reason
(`no-content`, `unparseable-format`, `unknown-schema-version`). Silent
counts hide data loss; see
[unstable-jsonl-format](unstable-jsonl-format.md) for why format drift makes
this a live risk, not a hypothetical.

**Import lock.** Concurrent imports of any source serialize. The loser gets
"import in progress by actor A since T", not a race whose winner is decided
by filesystem timing.

**Deletion receipts.** "Safe to delete the source" is a claim only the tool
may make, and only when the ledger proves the source digest fully ingested.
An agent should never decide this by judgment — in the incident, the agent's
judgment happened to be right, and it still destroyed the evidence needed to
answer the very next question.

## The contract, in one line

An agent interacting with Muninn's import layer should only ever *transport*
claims the ledger can prove — never manufacture claims by interpreting
counters. Determinism is what makes that possible: same source, same store,
same answer, with provenance attached.
