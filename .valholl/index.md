---
okf_version: "0.1"
title: "Muninn knowledge base"
description: "Architecture, durability, ingest, calibration, and governance knowledge for Muninn."
updated: "2026-08-17"
---

# Muninn knowledge base

A [Valhöll](https://github.com/tohuw/yggdrasil) bundle targeting the Open
Knowledge Format (OKF) 0.1 Draft. Written early and updated often — these
articles record *why* decisions were made and what was measured, not just what
the code does.

## Overview

- [What Muninn is](articles/what-muninn-is.md) — Memory to Huginn's Thought

## Durability

- [The index is an archive of record](articles/archive-of-record.md) — transcripts are deleted after 30 days
- [Continuous ingest, not periodic](articles/continuous-ingest-not-periodic.md) — why a cron indexer loses data, why the loop needs a daemon to own it, and what a login-agent supervisor does with the lock
- [The transcript JSONL format is not a stable API](articles/unstable-jsonl-format.md) — accepted risk, and how it degrades

## Ingest

- [Deterministic imports: receipts, not counters](articles/deterministic-imports.md) — the claudex "0 written" incident, and the import contract that prevents it
- [The import ledger: schema and invariants](articles/import-ledger-schema.md) — the concrete schema, digest scheme and testable invariants
- [Session lifecycle facts, documented vs. observed](articles/session-lifecycle-facts.md) — the 1.5s hook budget, and what the vendor does not guarantee

- [Embedding is not a chore](articles/embedding-is-not-a-chore.md) — why the daemon owns embedding, and why the automatic loop is written to quit rather than retry

- [Superseding a predecessor](articles/superseding-a-predecessor.md) — a byte-verified backfill that was still missing four of six directories

## Retrieval

- [Decisions outlive diffs](articles/decisions-outlive-diffs.md) — version control keeps what changed and discards why; joining git to the conversations that produced it, and the two measurements that bound the join
- [Retrieval that is not asked](articles/retrieval-that-is-not-asked.md) — every query interface is blind to what you have forgotten you have; why `recall` takes a place instead of a question, and why a surface that speaks unprompted has to be silent by default

## Data model

- [Provenance classification](articles/provenance-classification.md) — human / tool-invoked / subagent, and the 40x error

## Calibration

- [Derived calibration, not hard-coded thresholds](articles/derived-calibration.md) — survey the corpus, do not guess
- [Corpus measurements and performance breakpoints](articles/corpus-measurements.md) — measured FTS5 and vector numbers

## Extensibility

- [The shared menubar (menu-as-data)](articles/shared-menubar.md) — one raven, two minds; published by `muninn serve` since spec 010
- [Lessons for Huginn](articles/lessons-for-huginn.md) — improvements to contribute upstream; #5 became the shared `corvidae` login-agent seam

## Process

- [Delegating implementation to subagents](articles/delegating-implementation.md) — what worked, and the two process failures that cost work

## Governance

- [Model policy chokepoint](articles/model-policy-chokepoint.md) — fail-closed, intersecting policies
