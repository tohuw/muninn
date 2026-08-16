---
type: "Knowledge Article"
title: "Retrieval that is not asked"
description: "Every query interface can only return what someone thought to ask for, which excludes the material an archive is most valuable for holding."
tags: ["retrieval", "recall", "menubar", "enrichment", "interface"]
timestamp: "2026-08-16T00:00:00Z"
category: "retrieval"
status: "current"
updated: "2026-08-16"
summary: "Search, log, correlate and show all wait to be asked, so all four are blind to the material a person has forgotten they have — you do not query for what you do not know is there. `muninn recall` takes a place instead of a question, and the unfinished-work section it produces is the first thing Muninn says without being addressed. The design consequence is that a proactive surface must be silent by default, because a section that always renders is one people stop reading."
related: ["what-muninn-is", "shared-menubar", "provenance-classification", "embedding-is-not-a-chore"]
---

# Retrieval that is not asked

## The gap every query interface has

Muninn's retrieval surface was, until spec 019, five commands that are all the
same shape underneath. `search` takes a query. `log` takes a date range.
`correlate` takes a session id. `show` and `resume` take an identifier. Each is
a different index over the corpus, and the differences between them are real —
[[embedding-is-not-a-chore]] is largely about one of those differences — but
they share a precondition so basic it is easy to never notice:

**The user has to already know what they are looking for.**

That precondition is fine for most of what an archive is asked. Someone
remembers a bug and wants the session. Someone knows they worked on this in
June. Someone has a session and wants its neighbours. In every one of those,
the thing being retrieved is already in the user's head, and the archive's job
is to produce the detail attached to it.

It excludes exactly one category, and it is the category that makes a
long-lived archive worth keeping at all: **the material you have forgotten you
have.** You do not search for it, because you do not know it is there. No
improvement to ranking reaches it. A perfect search engine, given a query
nobody thinks to type, returns nothing — and returns it in a way that looks
like the archive is working correctly, because it is.

The corpus makes the scale concrete. At the time of writing this archive holds
928 sessions across ~40 repositories, spanning months. The proportion of that a
person can enumerate from memory is small, and it decays. The proportion they
can *form a query for* is smaller still, because a query needs a remembered
handle — a word, a file, an error — not just a vague sense that something
happened.

## Unfinished work is the sharpest case

Among things you can forget you have, one kind is materially worse than the
rest: work you started and did not finish.

A finished session that slips your mind costs a re-derivation at worst, and
often nothing — the work is done, the code is in the repo, the outcome
survives whether or not you remember the session. An *unfinished* one is
different in kind. The value of the work already done is contingent on
returning to it. Half a migration is not half the value of a migration; it is a
repository in a state nobody intended, and the longer the gap the more it costs
to re-enter.

Nothing in Muninn surfaced these before. `--outcome ongoing` existed as a
filter, but it is a filter — you have to already suspect there are loose ends
and go looking. The sessions most in need of surfacing are precisely the ones
their owner has stopped thinking about.

This is also the one place where enrichment's cost pays a dividend nothing else
collects. `topic` and `summary` improve results a user would have found anyway.
`outcome` is the only enriched field that identifies sessions **by a property
the user cannot observe from the outside** — you cannot tell an abandoned
session from a finished one by its length, date, repo, or tools. It takes
reading the thing. That makes `outcome` the field that justifies enrichment on
its own, and until spec 019 nothing consumed it except a filter flag.

## Taking a place instead of a question

The substitution `recall` makes is small and it is the whole idea: **a
repository is a question the user is already answering with their feet.**

Someone working in `~/repos/muninn` has, by being there, declared a topic
without typing one. That is a weaker signal than a query — it says nothing
about *which* aspect they care about — but it has one property no query has: it
is available without the user doing anything, which means it is available at
the moment before they have thought of a question, which is the moment the
forgotten material has to be surfaced in or not at all.

Muninn already knows where that is, from its own ingest: the most recently
written session's `cwd`. It deliberately does not ask Huginn, which would be
the more direct answer — the raven protocol forbids one raven presenting
another's credential, and Huginn's roster is behind exactly such a credential.
The last-ingested session is a good enough answer for free, and it degrades
gracefully: a stale answer names a repo the user worked in recently, which is
wrong in a boring way rather than a confusing one.

Tool-invoked sessions are excluded from that determination for the reason
[[provenance-classification]] gives generally — on the measured corpus 92% of
"sessions" are `claude -p` byproducts. A programmatic invocation's `cwd` is not
a place a person is sitting, and letting one win the recency contest would
point recall at whatever directory a script last ran in.

## A proactive surface has to be silent by default

Putting unfinished work in the menu bar — see [[shared-menubar]] — is where the
idea stops being a command and starts being a claim on attention, and that
changes the design constraints.

Every other section of Muninn's menu reports state: session counts, recent
work, the daemon's own lifecycle rows. The user reads them when they want them.
An unfinished-work section is the first that *asks something of them*, which is
why it sits above Recent and why it is the only part of this menu with an
argument for existing at all.

It is also the only part that can wear out. A section that renders on every
open teaches people to stop seeing it — the failure mode of every notification
badge that is always lit. So the section is absent, not empty, when there is
nothing loose. Absence is the honest rendering: the menu is smaller on the days
there is nothing to say, and a row that appears is a row that means something.

This is the same discipline as the counters elsewhere in this archive, applied
to attention rather than to data.

## Two silences that are not the same

The distinction this feature can most easily get wrong is one it shares with
`--outcome` (see the skill's note on false negatives), and getting it wrong
would be worse here because recall speaks unprompted.

"No unfinished work in this repository" and "no session in this repository has
ever been judged" produce an identical empty list. Enrichment is the only thing
that writes `outcome`, it runs behind a model-cost gate, and on a fresh archive
*every* session has a null outcome. Reporting the first when the second is true
tells a user their loose ends are handled at the precise moment nothing has
looked — an actively harmful reassurance, delivered without being asked for.

So recall reports which silence it is, in an `unavailable` map alongside the
results, and the related-work section reports its own version of the same thing
when no embedding provider is installed. The rule generalises past this
feature: **a surface that volunteers information owes a sharper account of its
own gaps than one that answers questions**, because the user did not come with
a hypothesis to check the answer against.

## What this does not become

Recall reports; it does not act. It opens nothing, resumes nothing, and writes
nothing back to the archive. That boundary is the same one spec 017 drew around
the menu's action rows and it is load-bearing for the same reason: the moment a
proactive surface can take an action on its own reading of what you meant, its
mistakes stop being ignorable.

The menu path also declines the embedding half of recall deliberately. Related
work needs the whole vector matrix in memory, which is unremarkable for a CLI
call and far outside a menu fetch's budget — and 009's rule that the menubar
must never cost the indexer its ingest applies whether the cost is I/O or
several hundred megabytes of float32.
