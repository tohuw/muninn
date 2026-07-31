---
okf_version: "0.1"
title: "Muninn knowledge base"
description: "Architecture, durability, ingest, calibration, and governance knowledge for Muninn."
updated: "2026-07-31"
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
- [Continuous ingest, not periodic](articles/continuous-ingest-not-periodic.md) — why a cron indexer loses data
- [The transcript JSONL format is not a stable API](articles/unstable-jsonl-format.md) — accepted risk, and how it degrades

## Ingest

- [Deterministic imports: receipts, not counters](articles/deterministic-imports.md) — the claudex "0 written" incident, and the import contract that prevents it

## Data model

- [Provenance classification](articles/provenance-classification.md) — human / tool-invoked / subagent, and the 40x error

## Calibration

- [Derived calibration, not hard-coded thresholds](articles/derived-calibration.md) — survey the corpus, do not guess
- [Corpus measurements and performance breakpoints](articles/corpus-measurements.md) — measured FTS5 and vector numbers

## Extensibility

- [The shared menubar (menu-as-data)](articles/shared-menubar.md) — one raven, two minds
- [Lessons for Huginn](articles/lessons-for-huginn.md) — improvements to contribute upstream

## Governance

- [Model policy chokepoint](articles/model-policy-chokepoint.md) — fail-closed, intersecting policies
