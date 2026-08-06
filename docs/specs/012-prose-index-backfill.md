# Spec 012 — Prose-index backfill

**Status: implemented.** Depends on 001 (ledger). Gates the retirement of
`tohuw/claudex` and `tohuw/codexdex`.

**Read first:** [`archive-of-record`](../../.valholl/articles/archive-of-record.md),
[`deterministic-imports`](../../.valholl/articles/deterministic-imports.md),
[`derived-calibration`](../../.valholl/articles/derived-calibration.md) (for why
a prose index is a fine thing to *ingest* and a terrible thing to *calibrate
from*).

## Why this had to exist before the predecessors could be archived

`claudex` and `codexdex` are superseded, and both were still active with no
pointer to Muninn — three tools indexing the same transcripts and no way for a
visitor to tell which to use. The obvious fix is to archive them.

The obvious fix was blocked, and on something easy to miss: **the predecessors'
indexes are archives too.** `~/.claudex/index` covers sessions whose raw
transcripts Claude Code swept months ago. Archiving the repositories is
harmless; anyone concluding from that that the *indexes* are now disposable
would destroy the oldest part of the corpus — data recoverable from nowhere
else.

So the honest order was: backfill, verify nothing was lost, *then* retire. This
spec is step one. Tracked as [#6](https://github.com/tohuw/muninn/issues/6).

## The command

```sh
muninn backfill                    # ~/.claudex and ~/.codexdex, if they hold anything
muninn backfill /path/to/index     # an explicit root
muninn backfill --json             # the import receipt, for an agent
```

A separate verb from `import` deliberately. `import` takes a vendor export —
something you downloaded, which the vendor still has. This takes a
predecessor's *archive*, which for much of its span is the only surviving copy,
and it is a one-time migration rather than a recurring operation.

## The three real formats

Read generically as `# key: value` headers plus a body, because they differ:

| source | distinguishing headers |
|---|---|
| `~/.claudex/index` | `kind: session\|subagent`, `parent`, `branch` |
| `~/.claudex/cloud/index` | `kind: cloud`, `name`, **no `cwd`** |
| `~/.codexdex/index` | `source`, `path`, `model`, `title` |

The **cloud sub-index is easy to miss** — a second directory holding a different
session class. Missing it would silently drop every claude.ai conversation the
predecessor archived, which on the development corpus is 737 sessions and 11.1M
words: the older and less replaceable half. Those become `claude-cloud` sessions,
the same source string `exports.parse_claude_export` writes, so a backfilled
conversation and a re-imported vendor export land on one row rather than two.

The header block ends at the first non-`#` line, so a markdown heading inside a
transcript cannot be mistaken for a header. Every field is optional and every
failure is an enumerated skip: a predecessor's format is at least as unstable as
a vendor's, since nobody is maintaining it.

## Decisions worth knowing

### Prose-index never overwrites raw

`origin` distinguishes them and raw wins. A raw transcript yields tool calls,
files touched, models, token counts and per-turn structure; a prose file yields
text and a few headers. The skip is **recorded per item** as
`superseded-by-richer-origin` — a reason reserved for exactly this before any
importer needed it — so "why is this session not from the backfill" is a query
against `import_items`, not an inference. Precedence runs one way only: a later
raw ingest *does* overwrite a backfilled row.

### `source_present = 0`, always, and `source_path` NULL

A prose entry exists because a predecessor archived a transcript, and for most
of this corpus that transcript is gone. Muninn cannot see the original from here
— claudex did not even record its path — so claiming presence would overstate
what survives and corrupt the statistic that matters most: how much of the
archive is the only copy. The NULL path also keeps these rows out of the local
sweep's reconciler, which only considers rows naming a path, so nothing flaps
them. Self-correcting in the safe direction: if the raw file does still exist,
the next `muninn index` ingests it as `raw` and sets presence back.

### The body is stored verbatim

Including the predecessors' `[USER <timestamp>]` markers, which differ from the
`[USER]` form Muninn's own adapters emit. Normalising them would be a lossy
rewrite of the only surviving copy of that text, performed to make it
cosmetically match text that is not at risk. The prose is the thing being
rescued; it is not the place to tidy.

### It never reconciles

Absence from a predecessor's index proves only that the predecessor did not
index it. This importer touches only sessions the index itself names.

### `tool_uses = 0` is a schema limit, not a claim

The columns are `NOT NULL` and a prose index recorded no tool activity — which
is "we cannot know", not "there was none". The schema cannot express the first,
and widening it for a backfill would be a large change for a narrow gain.
`origin` is what tells a reader how much to trust these rows. Named here rather
than left to be discovered.

## Verification against the real corpus

Run on the development machine's actual `~/.claudex` and `~/.codexdex`, into a
throwaway archive:

```
/Users/hljod/.claudex
source   prose-index · 3,582 items · 2025-09-19 .. 2026-08-06
this run added 3,580 · updated 0 · unchanged 0 · skipped 2

/Users/hljod/.codexdex
source   prose-index · 156 items · 2026-07-17 .. 2026-07-19
this run added 150 · updated 0 · unchanged 0 · skipped 6
```

Then, comparing every source file's parsed body against the archived text:

```
prose files discovered : 3,738
  skipped (no content) : 8      <- all 8 verified to have 0-byte bodies
  text missing         : 0
  text differs         : 0
words in source files  : 26,420,905
words in archive rows  : 26,420,905
rows claiming a surviving raw transcript: 0
```

| source | provenance | sessions | words |
|---|---|---|---|
| claude | human | 1,106 | 13,551,343 |
| claude | subagent | 367 | 415,893 |
| claude | tool-invoked | 1,367 | 409,616 |
| claude-cloud | human | 737 | 11,096,055 |
| claude-cloud | tool-invoked | 3 | 450 |
| codex | human | 19 | 269,063 |
| codex | tool-invoked | 131 | 678,485 |

A second run reported `duplicate of import #1` with attribution rather than
"imported, 0 added" — the distinction
[`deterministic-imports`](../../.valholl/articles/deterministic-imports.md)
exists to enforce.

**Note the codex caveat from #6:** codexdex was never run on this machine before
Muninn's design work, so its 156 files were indexed once, recently, and the
Codex-side migration path has correspondingly less real-world validation behind
it than the Claude side.

## Acceptance criteria

1. Every word of prose survives, byte for byte, and is searchable afterwards.
2. A second pass is a `duplicate`, changes nothing, and creates no second row.
3. A raw-derived session is left completely untouched, and the decision is
   recorded as `superseded-by-richer-origin` in `import_items`.
4. A later raw ingest overwrites a backfilled row.
5. Backfilled rows record `origin = 'prose-index'`, `source_present = 0`,
   `source_path = NULL`.
6. All three formats parse, including the cloud sub-index and codexdex's
   `source` header overriding the root's default.
7. Provenance stays structural — a subagent keeps its class and parent, a
   `.local/state` cwd is still tool-invoked.
8. A `#` inside the prose is not read as a header.
9. Every dropped file gets an id and a closed-vocabulary reason; the arithmetic
   closes.
10. An index of only junk is `rejected`, never "imported, 0 added".
11. A vanished index never deletes what it contributed.
12. An empty predecessor directory is reported as not found, not as an empty
    source.

## What remains before #6 can close

Backfill is done and verified. **Archiving the two repositories is a human
action** — it needs the repos' READMEs to point at Muninn first, and neither
should be archived until someone has run this against their own corpus and is
satisfied. The migration exists; declaring it complete is not this repo's call.

## Guardrails

- **Do not calibrate from a prose index.** Ingesting one is fine; deriving
  thresholds from one undercounted conversations by 15–27%. See
  [`derived-calibration`](../../.valholl/articles/derived-calibration.md).
- **Do not normalise the body.** Verbatim, including the old markers.
- **Do not let a prose file overwrite a raw session**, and do not make the skip
  silent.
- **Do not claim `source_present = 1`** for a backfilled row.
- **Do not reconcile.** Absence from a predecessor's index means nothing.
