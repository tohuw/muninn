# 019 — `muninn recall`

**Status:** implemented.
**Read first:** [retrieval-that-is-not-asked](../../.valholl/articles/retrieval-that-is-not-asked.md)
for why a query interface cannot reach the material this serves;
009-raven-descriptor-menu.md for the menu payload and its caps; 005-enrichment.md
for what writes `outcome`, and 006-hybrid-retrieval.md for the vectors the third
section reads.

## Why

Every retrieval command in Muninn takes something the user already has: a query,
a date range, a session id. That excludes the material a long-lived archive is
most valuable for holding — the work someone has forgotten they did — because
you do not search for what you do not know is there.

`recall` takes a **place** instead. A repository is a question the user is
already answering by being there, and it is available before they have thought
of a question, which is the only moment forgotten material can be surfaced in.

Unfinished work is the sharpest case and nothing surfaced it before. `outcome`
is also the only enriched field identifying sessions by a property that cannot
be observed from the outside — length, date, repo and tools do not distinguish
an abandoned session from a finished one — and until this spec nothing consumed
it but a filter flag.

## What ships

`muninn recall [--repo NAME] [--limit N] [--json]`, and one new menu section.

Three sections, ordered by how easily each is lost:

| Section | Source | Cost |
|---|---|---|
| **unfinished** | `outcome IN ('ongoing','abandoned')`, newest first | SQL |
| **prior** | same repo, newest first, minus anything already listed above | SQL |
| **related** | mean-vector neighbours in *other* repos, over `RELATED_FLOOR` | dot product, no model |

`--repo` defaults to the basename of the `cwd` of the most recently started
session. `--limit` defaults to 5 and applies per section. Exit is **0** when the
archive has nothing to say: nothing to recall is an answer, not a failure.

## Where "now" comes from

Muninn's own ingest — the most recently started session's `cwd` — and
**deliberately not Huginn**, which is the more direct answer and the forbidden
one. Huginn's roster sits behind a credential, and the raven protocol's rule is
that no raven presents another's. The last-ingested session costs nothing, needs
no network, and degrades into naming a recently-worked repo rather than into an
error.

Tool-invoked sessions are excluded from that determination, and from every
section. Per 004 and [[provenance-classification]] they are 92% of rows on the
measured corpus; a `claude -p` byproduct's `cwd` is not a place a person is
sitting, and letting one win the recency contest points recall at whatever
directory a script last ran in.

## The two silences

An empty `unfinished` list means either "nothing loose" or "nothing has ever
been judged", and these are opposite facts with an identical rendering.
Enrichment is the only writer of `outcome` and it runs behind a model-cost gate,
so on an unenriched archive every session has a null outcome.

Recall therefore reports **which**, in an `unavailable` map keyed by section
name, and `related` uses the same map when no embedding provider is installed.
Reporting "no loose ends" when nothing has looked is a false reassurance
delivered unprompted, which is worse than the same error in a command that was
asked a question — the user has no hypothesis to check it against.

This is 006's `--semantic` rule and the skill's `--outcome` false-negative note,
made structural: the distinction lives in the payload rather than in prose
telling a caller to remember it.

## The menu section

`Unfinished in <repo>`, above `Recent`, capped at `raven.UNFINISHED_LIMIT` rows.

It is above Recent because it is the only section of this menu that **asks
something of the user** rather than reporting state. It is **absent rather than
empty** when there is nothing loose: a section that always renders is one people
learn to stop reading, and absence is the honest rendering of a quiet day.

The menu path calls `recall.unfinished` directly and **must not** call
`recall.recall`, which pulls the whole vector matrix for the related section.
That is unremarkable in a CLI call and far outside a menu fetch's budget — 009's
"the menubar must never cost the indexer its ingest" applies whether the cost is
I/O or several hundred megabytes of float32.

Session ids are validated before they reach a URL, exactly as every other id in
this menu is; a row whose id fails validation drops the section rather than
emitting the row.

## Read-only, and staying that way

Recall reports. It opens nothing, resumes nothing, and writes nothing back to
the archive. This inherits 017's boundary for the same reason: once a surface
that speaks unprompted can also act on its own reading of intent, its mistakes
stop being ignorable. A "resume this" row is the obvious next request and it is
out of scope here, not deferred.

## Acceptance criteria

1. `current_repo` is the most recent **non-tool-invoked** session's `cwd`
   basename; `None` on an empty archive.
2. `unfinished` is exactly `ongoing` and `abandoned` — `exploratory` is a
   finished exploration, not a loose end — scoped to the repo, newest first.
3. `prior` never repeats a session already in `unfinished`.
4. Tool-invoked sessions appear in no section.
5. `unavailable` names `unfinished` when no session in the repo has any outcome,
   and does **not** name it once one does; `related` likewise on a missing
   provider.
6. `recall` with no `--repo` resolves to where the work is.
7. Exit 0 and non-empty output on an archive with nothing to recall; `--json`
   emits one object with the five documented keys.
8. Menu: the section appears with a real loose end, is **absent** with none and
   absent when the caller passes nothing, sits before `recent`, is capped, and
   drops rather than emits a row whose session id fails validation.
9. Menu, through the real provider: a session ingested and then enriched
   produces the section — built from the production write path (`upsert_session`
   carries ingest columns only), not a hand-assembled row.
10. The provider path never calls `recall.recall`.

## Out of scope

- **Any action row.** See above.
- **Querying Huginn for the current repo.** Forbidden by the raven protocol, not
  merely unimplemented.
- **The related section in the menu.** Budget, per 009.
- **Cross-machine recall.** Vector sets do not leave the box.
