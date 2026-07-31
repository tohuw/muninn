---
type: "Knowledge Article"
title: "The import ledger: schema and invariants"
description: "Concrete schema, digest scheme and invariants that make deterministic imports enforceable rather than aspirational."
tags: ["ingest", "imports", "schema", "determinism", "sqlite", "ledger"]
timestamp: "2026-07-31T00:00:00Z"
category: "ingest"
status: "current"
updated: "2026-07-31"
summary: "Implements the requirements in deterministic-imports as a concrete SQLite schema: an append-only import_ledger keyed by content digest, per-item receipts, an enumerated skip vocabulary, an advisory import lock, and a structured ImportReceipt. Records the exact defects reproduced in Muninn's own pre-ledger ingest code and the invariants that now forbid them."
related: ["deterministic-imports", "archive-of-record", "continuous-ingest-not-periodic", "unstable-jsonl-format", "provenance-classification"]
---

# The import ledger: schema and invariants

[[deterministic-imports]] states *why* imports must produce receipts rather than
counters. This article fixes *what* gets built, so the requirement is enforceable
by test rather than left to an implementer's judgement.

## The defects, reproduced in Muninn itself

Before the ledger, Muninn's own `ingest.IngestResult` had the same shape that
caused the claudex incident. Reproduced 2026-07-31 against the real code:

```
ACTOR A imports:      scanned=1 ingested=1 updated=0 unchanged=0
ACTOR B, 3 min later: scanned=1 ingested=0 updated=0 unchanged=1
```

An agent reading actor B's line says *"nothing new"*. The true statement is
*"this exact source was already imported at 21:15 by a prior run."* Worse, the
`ingest_state` table used `ON CONFLICT(source_path) DO UPDATE`, so actor B's run
**overwrote actor A's `last_seen_at`** — the only direct evidence of the first
import. Both defects were present in code written the day before the incident
article landed. This is not a hypothetical risk; it is the default outcome of
counter-shaped reporting.

## Schema

Three new tables. `ingest_state` survives as a pure performance cache — it may be
deleted and rebuilt at any time without losing history, which is precisely why it
is allowed to be last-writer-wins.

```sql
-- Append-only. NOTHING may UPDATE or DELETE a row here.
CREATE TABLE import_ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,          -- ISO8601 UTC
    finished_at   TEXT,                   -- NULL while in flight or if crashed
    actor         TEXT NOT NULL,          -- "cli", "hook", "watcher", "agent:<name>"
    source_kind   TEXT NOT NULL,          -- claude-transcripts | codex-rollouts
                                          -- claude-export | chatgpt-export | prose-index
    source_ref    TEXT,                   -- path or export name, display only
    source_digest TEXT NOT NULL,          -- see "Digest scheme"
    item_count    INTEGER NOT NULL DEFAULT 0,   -- items the SOURCE contains
    span_earliest TEXT,                   -- earliest item timestamp in source
    span_latest   TEXT,                   -- latest item timestamp in source
    windowed      INTEGER NOT NULL DEFAULT 0,   -- 1 if source may be a partial window
    outcome       TEXT NOT NULL,          -- imported | duplicate | partial | rejected
    duplicate_of  INTEGER,                -- ledger_id of the original import
    added         INTEGER NOT NULL DEFAULT 0,
    updated       INTEGER NOT NULL DEFAULT 0,
    unchanged     INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    error         TEXT,                   -- exception CLASS name only, never a message
    FOREIGN KEY (duplicate_of) REFERENCES import_ledger(ledger_id)
);
CREATE INDEX idx_ledger_digest ON import_ledger(source_digest);
CREATE INDEX idx_ledger_started ON import_ledger(started_at);

-- Per-item receipts: what actually happened to each item, and why.
CREATE TABLE import_items (
    ledger_id   INTEGER NOT NULL,
    item_id     TEXT NOT NULL,            -- session id / conversation uuid
    disposition TEXT NOT NULL,            -- added | updated | unchanged | skipped
    reason      TEXT,                     -- required when disposition = 'skipped'
    PRIMARY KEY (ledger_id, item_id),
    FOREIGN KEY (ledger_id) REFERENCES import_ledger(ledger_id)
);
CREATE INDEX idx_import_items_item ON import_items(item_id);

-- Advisory lock. One row, or none.
CREATE TABLE import_lock (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_id  INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    pid        INTEGER
);
```

### Why per-item receipts, not just counts

A count cannot be audited. `skipped=4` might be four empty conversations or four
voice sessions a parser gap dropped silently. With `import_items`, the question
*"which four, and why?"* is one query. This is the same discipline
[[unstable-jsonl-format]] demands: format drift must surface as an enumerable
list, not a number that looks tidy.

## Digest scheme

The digest must identify a *source's content*, be stable across re-reads, and
survive irrelevant churn (a re-download with a different filename, a directory
copied elsewhere).

- **Exports** (`claude-export`, `chatgpt-export`): SHA-256 over the sorted
  `item_id\tupdated_at_raw` pairs, newline-joined, UTF-8, **prefixed by the
  source kind**. Deliberately **not** a file digest: exports are regenerated with
  different byte layouts but identical content, and content identity is the
  question being asked.

  Three rules learned from reading the predecessors:

  1. **Hash the raw timestamp, never a converted one.** claude.ai gives
     `updated_at` as an ISO string; ChatGPT gives `update_time` as an epoch
     **float**. Converting float→ISO inside the digest boundary makes the digest
     depend on sub-second float formatting and therefore on the Python version.
     Hash the source's own representation, stringified verbatim.
  2. **Include the source kind in the preimage.** Both vendors ship a top-level
     JSON array in a file named `conversations.json`; only the presence of
     `mapping` (ChatGPT) versus `chat_messages`/`uuid` (claude.ai) tells them
     apart. Without a discriminator, two different exports can collide.
  3. **Record a plain file digest alongside it.** It is nearly free, and it
     catches upstream schema changes that leave the `(id, updated_at)` pairs
     identical. It is also the only usable identity when `updated_at` is absent.
- **Live transcript trees** (`claude-transcripts`, `codex-rollouts`): SHA-256
  over sorted `relative_path\tsize_bytes\tmtime_ns` triples. These are
  append-only and grow constantly, so the digest identifies *the tree as observed
  at that moment* — its purpose is attributing a scan, not detecting duplicates.
- **Prose indexes** (`prose-index`): same triple scheme as transcript trees.

Digests are prefixed with the scheme (`items-sha256:…`, `tree-sha256:…`) so a
future scheme change cannot silently collide with an old one.

## Outcome vocabulary

Exactly four values, chosen so an agent can branch without interpreting prose:

| outcome | meaning |
|---|---|
| `imported` | the run changed the archive; `added + updated > 0` |
| `duplicate` | this exact `source_digest` was already imported; `duplicate_of` is set |
| `partial` | some items imported, some failed; `skipped > 0` and the archive changed |
| `rejected` | nothing imported — unreadable source, lock contention, or failed validation |

`duplicate` is the value that would have prevented the incident. It cannot be
confused with an empty source, because an empty source yields `imported` with
`item_count = 0`.

## Skip reason vocabulary

Enumerated and closed. An unrecognized reason is a bug, not a free-text field.

`no-content` · `unparseable-json` · `unknown-schema` · `missing-item-id` ·
`missing-timestamp` · `duplicate-item-in-source` · `read-error` ·
`unsupported-sender-role` · `unsupported-content-type` ·
`superseded-by-richer-origin`

The last one matters for backfill: a prose-index item must never overwrite a
richer raw-derived session (see [[archive-of-record]]), and when it declines to,
that is a *recorded decision*, not a silent no-op.

`unsupported-sender-role` and `unsupported-content-type` exist because the
predecessors dropped these silently. claudex harvested only `type == "text"`
blocks and only `human`/`assistant` senders, so a voice-only or image-only
conversation reported as "empty" — indistinguishable from genuinely empty. That
ambiguity is exactly what the incident's *"4 empty"* line could not resolve.

## Conservation: the arithmetic must close

```
added + updated + unchanged + skipped == item_count
```

This identity is an assertion, not a hope. In claudex it did **not** hold: a
conversation with a falsy `uuid` was `continue`d before any counter incremented,
so it vanished from the totals entirely — a silent data-loss path that the
printed line `0 written, 61 cached, 4 empty (total 65)` happened to obscure by
summing correctly that time. codexdex had the same hole for items lacking
`mapping`.

Muninn must therefore verify the sum at the end of every import and raise if it
disagrees. A drop that nobody counted is the failure this whole subsystem exists
to prevent.

## Invariants (each one a test)

1. **Append-only.** No code path UPDATEs or DELETEs `import_ledger` except
   setting `finished_at`, `outcome`, and the count columns on the *in-flight* row
   it created. A second import of the same digest appends a new row.
2. **Digest determinism.** Same source content ⇒ same digest, across processes and
   working directories.
3. **Duplicate detection is by identity.** A second import of an unchanged export
   yields `outcome='duplicate'` with `duplicate_of` pointing at the first row —
   never `imported` with zero counts.
4. **Source facts and run deltas never merge.** `item_count`/`span_*`/`windowed`
   describe the source; `added`/`updated`/`unchanged`/`skipped` describe the run.
   A reporting layer that renders them as one blended line is a defect.
5. **Every skip is named.** `skipped` must equal the number of `import_items`
   rows with `disposition='skipped'`, and each must carry a `reason` from the
   closed vocabulary.
6. **Windowed sources cannot support deletion claims.** When `windowed = 1`,
   absence of an item from the source is not evidence it was deleted upstream. No
   code may mark a session missing based on a windowed source.
7. **Lock serializes.** A second concurrent import gets `rejected` with the
   holder's actor and acquisition time — not a race.
8. **Errors carry class names only.** Never an exception message, which can embed
   transcript text or credentials.
9. **A crashed import is visible.** A row with `finished_at IS NULL` and no live
   `pid` is reported by `doctor` as an incomplete import rather than being
   silently reaped.
10. **Conservation holds.** `added + updated + unchanged + skipped == item_count`
    for every completed import. A mismatch is an error, not a rounding detail.
11. **Item ids are never coerced.** A missing or empty item id is a
    `missing-item-id` skip. codexdex defaulted it to the literal `"unknown"`,
    so every id-less conversation overwrote the previous one on disk.

## Concurrency: the lock must guard the decision, not just the body

Stress-tested 2026-07-31 with four threads importing the same tree
simultaneously. Data integrity held — 30 sessions, no duplication, no orphaned
lock row — but two defects surfaced that no single-threaded test could reach:

1. **`open_store` had no busy timeout**, so one thread died with
   `sqlite3.OperationalError: database is locked` on the `journal_mode` pragma
   before any lock logic ran. Fix: a `timeout` on `sqlite3.connect` plus
   `PRAGMA busy_timeout`, applied before other pragmas.
2. **Three of four runs reported `imported` where two should have reported
   `duplicate`.** `find_import_by_digest` correctly requires
   `finished_at IS NOT NULL`, but the losers called it *before* the winner
   committed, saw no prior import, and fell through to `imported` with
   `added=0, unchanged=30`.

The second is the incident's ambiguity wearing a different hat: a run that
changed nothing reporting the same outcome as a run that imported everything.
`added=0, updated=0, unchanged=N` with outcome `imported` is `0 written, 61
cached` again.

**The rule this establishes: the import lock must be acquired before the
duplicate-detection lookup, not merely around the import body.** Otherwise the
lock guards the work but not the *claim about* the work, and the claim is the
part that gets relayed to a human.

Note what must not be "fixed": the `finished_at IS NOT NULL` condition is
correct. Relaxing it so in-flight rows are visible would let a crashed import
masquerade as a successful one.

This matters more as the system grows. Concurrent imports are rare today — a
human racing themselves — but a hook, a watcher, and a periodic sweep make
overlap the normal case rather than the exception.

## Deletion receipts

`muninn import --verify-safe-to-delete <source>` answers only from the ledger: it
recomputes the digest and confirms a `finished_at IS NOT NULL` row with
`outcome IN ('imported','duplicate')` covers it, and that every item in the source
has an `import_items` row with a non-skipped disposition. Anything else is a
refusal with the reason.

The agent never decides this. In the incident the agent's judgement happened to be
correct and it *still* destroyed the evidence needed to answer the next question.

## Structured output

Every import emits both prose and a machine-readable receipt. Agents parse the
JSON and relay it; they do not summarize the prose.

```json
{
  "schema": "muninn.import-receipt/1",
  "ledger_id": 15,
  "outcome": "duplicate",
  "duplicate_of": 14,
  "source": {
    "kind": "claude-export",
    "digest": "items-sha256:9f2c…",
    "item_count": 65,
    "span": ["2026-06-30T11:02:00Z", "2026-07-30T20:31:00Z"],
    "windowed": true
  },
  "delta": {"added": 0, "updated": 0, "unchanged": 61, "skipped": 4},
  "skips": [{"item_id": "0200…", "reason": "no-content"}],
  "attribution": {"ledger_id": 14, "actor": "cli", "finished_at": "2026-07-30T21:15:17Z"}
}
```

Note what this makes impossible: there is no way to read this object and conclude
"the export contained nothing new." `outcome` says `duplicate`, `attribution`
names the prior import, and `span` shows content through 20:31.
