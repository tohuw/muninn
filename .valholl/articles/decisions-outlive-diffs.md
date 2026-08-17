---
type: "Knowledge Article"
title: "Decisions outlive diffs"
description: "Version control keeps what changed and discards why; the argument that produced a line is the part that ages well, and it lives in a conversation."
tags: ["retrieval", "git", "decisions", "attribution", "measurement"]
timestamp: "2026-08-17T00:00:00Z"
category: "retrieval"
status: "current"
updated: "2026-08-17"
summary: "A diff records the outcome of a decision and throws away the alternatives, so `git blame` can say who and when but never why. Muninn holds the conversations where the reasoning happened, and joining the two answers the question people actually ask. Attribution by time overlap only works if it is bounded: session length spans four orders of magnitude, so mere overlap puts eight candidate sessions behind every commit."
related: ["retrieval-that-is-not-asked", "archive-of-record", "provenance-classification", "corpus-measurements"]
---

# Decisions outlive diffs

## What version control throws away

A commit is the *outcome* of a decision with the decision removed. It records
that the retry count became two. It cannot record that three was tried first and
made the flake worse, that a backoff was considered and rejected because the
caller already has one, or that the whole path only exists because a vendor's
API answers 200 on failure.

That missing half is the part that ages well. The diff stops being interesting
the moment you have read it; the reasoning stays useful for as long as the code
does, and is what someone needs when they find the code strange years later.

Conventions exist to smuggle some of it back — a good commit message, an ADR, a
comment above the odd line. All of them are a person remembering, at the moment
of writing, that a future reader will be confused. That is exactly the kind of
foresight nobody reliably has, which is why the reasoning is usually absent
precisely where it is most needed.

Meanwhile the reasoning *was* written down. It was written down at length, with
the alternatives, in the conversation that produced the change — and then it was
left in a transcript nobody would ever open again.

## Two halves of one answer

Git knows which commits touched a file. [[archive-of-record]] knows what was
being attempted while those commits were written, and since enrichment it knows
it in a structured form: topic, outcome, and a list of decisions.

Neither half answers "why is this file like this". Together they do, and the
join is cheap — a commit timestamp against a session's lifetime.

This is the same move as [[retrieval-that-is-not-asked]] one layer over: take a
*thing* the person is already holding — there a repository, here a file — rather
than requiring them to compose a question. Somebody staring at a confusing
function has the file open. That is the whole query.

## Overlap is only evidence when it is bounded

The obvious implementation says: the session live when the commit landed is the
session that wrote it. It is nearly right, and left unbounded it is useless.

Session length on a real 2,163-session corpus spans four orders of magnitude:

| | |
|---|---|
| median | **12 minutes** |
| p75 | 14 hours |
| p90 | 7 days |
| p95 | **27 days** |
| longest | 271 days |

21% of sessions stay open more than a day; 10% more than a week. A session
parked for a month overlaps every commit made that month, so at one arbitrary
commit instant **eight sessions were "open"** — of which at most one wrote the
commit. Offering all eight, even politely labelled, buries the true attribution
under whatever the person happened to leave running. The first live run filed
three Cyberpunk-modding sessions under a change to a cost module.

So attribution requires a *connection*, not a coincidence: the session must have
touched the file, or something else in the same repository. Everything else is
reported as unexplained, which is honest and frequently correct — plenty of
commits are written by hand.

The generalisable form: **a signal with a long tail is not a weak signal, it is
a different signal.** Labelling it "low confidence" and showing it anyway treats
a distribution problem as a presentation problem.

## `cwd` is not the repository

The other obvious implementation matches a session's working directory to the
repository. It is wrong, and the measurement is stark: on the archive this was
built against, **zero** sessions had a `cwd` under the muninn checkout — while
that repository's entire recent history had been written from sessions rooted in
a sibling one.

`cwd` records where an agent was *launched*. Anyone who works across two
repositories from one shell breaks it, which is most people most days. The
failure is silent: an empty result that reads like "there is no history here"
rather than "you asked the wrong way".

The files a session actually touched are the honest signal. They come from the
tool calls the agent made, so they describe where the work landed rather than
where it started. Where the two disagree, the files are right.

**This has a consequence beyond attribution.** `--repo` filters and `recall`'s
scoping both key on `cwd`, and inherit the same blind spot: work done in one
repository from a shell rooted in another is filed under the wrong name. That is
worth fixing and is not fixed yet; it is recorded here so the next person meets
it as a known limitation rather than as a mystery.

## What it must be willing to say

Three answers matter as much as the attributions:

- **"No session was open when this landed."** Not everything is written by an
  agent, and a silent gap reads as a tool failure rather than a fact.
- **"This session edited the file but committed nothing."** Exploration, a
  reverted attempt, work still in the tree — invisible to git by construction,
  and often the half that explains the shape of what did land.
- **"No file records exist for this path."** File lists come from an agent's own
  tool calls, so anything edited by hand leaves none. Absence of records is not
  absence of history.
