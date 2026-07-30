---
type: "Knowledge Article"
title: "What Muninn is"
description: "Muninn is the agent-history console: Memory to Huginn's Thought."
tags: ["overview", "architecture", "huginn"]
timestamp: "2026-07-30T00:00:00Z"
category: "overview"
status: "current"
updated: "2026-07-30"
summary: "Muninn archives, searches, and correlates past AI-agent sessions. It is a companion to Huginn, which watches live sessions. Together they share one menubar surface."
related: ["archive-of-record", "provenance-classification", "derived-calibration"]
---

# What Muninn is

Muninn is a local-only console for **agent history** — what your AI agents did,
across Claude Code, Codex, and vendor data exports. It provides fast hybrid
search, correlation of similar conversations, quick session resume, and
context briefs that read equally well to humans and agents.

In the Norse pairing, Huginn is **Thought** and Muninn is **Memory**. That maps
cleanly onto the software: [Huginn](https://github.com/tohuw/huginn) answers
"what are my agents doing right now"; Muninn answers "what did we do, decide,
and learn." They are complementary, not competing, and they deliberately share
a single menubar surface — nobody wants two ravens, let alone fifty apps, in
their menubar.

Muninn supersedes two earlier single-purpose tools, `tohuw/claudex` and
`tohuw/codexdex`, folding their prose-index approach into one unified archive
with per-source adapters. Those repos are archived with a pointer here.

## Why it exists

Three problems that generic transcript grep does not solve:

1. **Transcripts are deleted.** Claude Code sweeps session JSONLs older than
   `cleanupPeriodDays` (default 30). The archive is often the only surviving
   copy. See [[archive-of-record]].
2. **You do not remember your own wording.** You remember the *situation*. That
   is a recall problem, not a ranking problem — hence hybrid lexical + semantic
   retrieval.
3. **You want the moment something was decided or fixed**, not every line where
   a word appears. That requires enrichment at index time, not better matching.

## Non-goals

- Not a cloud service. Everything is local by default.
- Not a live-activity monitor — that is Huginn's job.
- Not a general-purpose search engine over arbitrary documents.
