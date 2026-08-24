---
type: "Knowledge Article"
title: "The name and the topic"
description: "A session's own conversation name was stored at ingest and never surfaced; title and topic answer different questions and neither substitutes for the other."
tags: ["retrieval", "data-model", "cli", "pitfalls"]
timestamp: "2026-08-24T00:00:00Z"
category: "retrieval"
status: "current"
updated: "2026-08-24"
summary: "Muninn stored sessions.title from the first claude.ai importer onward and no command ever printed it, so reading a conversation's own name meant opening the SQLite file directly — the one thing the agent-facing contract tells callers not to do. Title is the handle a person uses to find a conversation again; topic is what enrichment decided it was about. They are shown side by side rather than competing for one slot."
related: ["retrieval-that-is-not-asked", "what-muninn-is", "decisions-outlive-diffs"]
---

# The name and the topic

`sessions.title` has existed since the export importers landed. The claude.ai
importer reads it from `conv["name"]`, the ChatGPT one from `conv["title"]`, and
the prose-index backfill from a `title:` header. It was written on every ingest
and read by nothing except the menubar's label fallback.

## How the gap surfaced

Someone asked for a conversation by the only name they had for it. They had a
PDF of their own neuropsychological assessment and wanted the session where they
had discussed it; `muninn search` found it immediately by content, and then they
asked the obvious follow-up — *what is that conversation actually called?*

Every retrieval path could answer what the session was **about**:

```
2a693af6  claude-cloud 2026-05-15  (493,440w)
          Late-identified adult autism diagnosis, clinical assessment,
          integration into identity and creative work, and family
          boundary-setting
```

None could answer what it was **called**. The name was two inches away in the
same row of the same table — `Neurospicy 3` — and the only way to reach it was:

```sh
sqlite3 muninn.db "select title from sessions where session_id like '2a693af6%'"
```

That is precisely the bypass [the agent-facing
contract](../../AGENTS.md) exists to prevent. When the CLI cannot answer a
question the archive can, the boundary stops being a boundary.

## Why they are not the same field

The temptation is to treat these as two candidates for one "label" slot and pick
a winner. They are not interchangeable:

| | `title` | `topic` |
|---|---|---|
| Written by | the person, in the vendor UI | enrichment, from the prose |
| Answers | *what is this conversation called?* | *what is this conversation about?* |
| Good for | finding it again, referring to it | knowing what you would find |
| Present when | the source had a name | enrichment has run |
| Stale when | never — a name does not decay | the session grew past its baseline |

`Neurospicy 3` is useless to anyone who did not name it and is the *only* useful
string to the person who did — it carries the series, the private joke, and the
position in a sequence. The derived topic is the reverse: legible to a stranger,
and not what its author would ever type to find it.

So both are printed, and the title is quoted to mark it as a name rather than a
description:

```
2026-05-15  2a693af6  claude-cloud  -  (493,440w)  "Neurospicy 3"  Late-identified adult autism diagnosis...
```

The one place they still contend is `recall` and `why`, where the label is
truncated to a fixed width and only one can fit. Those prefer topic and fall
back to title, matching the ordering `raven._session_label` already used for the
menubar — usefulness to a reader who is scanning, not a judgement about trust.

## Two smaller decisions

**A missing title is `null`, not an absent key.** Codex rollouts and Claude Code
sessions have no conversation name at all, so a caller must be able to tell "this
source does not name conversations" from "this Muninn predates titles". A dropped
key cannot express that difference; an explicit `null` can.

**Whitespace is collapsed before printing.** A title is transcript-derived text
and therefore arbitrary. A newline inside one would split a session across two
output lines, and the second half would read as a row of its own — a formatting
bug that looks like a data bug. This is the same reasoning behind
`raven.safe_label`, applied at a different boundary.

## What this does not do

Titles are **not searchable**. The FTS index covers prose chunks, and a title is
not prose; `muninn search "Neurospicy"` finds sessions that *say* the word, not
sessions *named* it. Surfacing the field was the gap that had a user behind it,
and indexing names is a separate change with its own ranking questions — a title
matching exactly should almost certainly outrank a passing mention of the same
word in a transcript, and that is a scoring decision, not a projection one.
