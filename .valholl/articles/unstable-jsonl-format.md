---
type: "Knowledge Article"
title: "The transcript JSONL format is not a stable API"
description: "Muninn parses formats that vendors may change without notice, so adapters must fail soft and parse health must be visible."
tags: ["ingest", "risk", "sources", "resilience"]
timestamp: "2026-07-30T00:00:00Z"
category: "durability"
status: "current"
updated: "2026-07-30"
summary: "Claude Code's docs state explicitly that the JSONL transcript format is not part of the stable API and direct parsers can break on any release. Muninn must parse it anyway for historical data, so adapters degrade rather than crash and doctor reports parse-failure rates."
related: ["archive-of-record", "continuous-ingest-not-periodic", "provenance-classification"]
---

# The transcript JSONL format is not a stable API

Claude Code's documentation states plainly that the JSONL session format is **not
part of the stable API**, and that scripts parsing `.jsonl` files directly can
break on any release. The supported alternatives are `/export`, hooks receiving
`transcript_path`, and the Agent SDK's `SessionStore`.

Source: [sessions](https://code.claude.com/docs/en/sessions.md).

## Why Muninn parses it anyway

There is no alternative for **historical** data. A supported export only helps
going forward; the archive's whole value is that it holds sessions that no longer
exist elsewhere (see [[archive-of-record]]). `claudex`, `codexdex`, and Huginn all
parse these files directly for the same reason.

So this is an accepted, documented risk — not an oversight.

## Mitigations

- **Fail soft per record.** A record that does not match expectations is counted
  and skipped, never fatal. One malformed line must not abort a session's ingest,
  and one bad session must not abort a sweep.
- **Fail soft per field.** Treat every field as optional. Derive what is available;
  never assume a key exists because it did last release.
- **Report parse health.** `doctor` surfaces parse-failure counts and rates by
  category. A format change should appear as a *rising failure rate*, not as
  silently missing history.
- **Version the adapters.** Each source adapter declares which format variants it
  understands, so a break is attributable rather than mysterious.
- **Prefer supported inputs where they exist.** Use the `SessionEnd` hook's
  official `transcript_path` for live capture; reserve direct directory scanning
  for backfill and reconciliation.
- **Keep the prose index authoritative.** Once a session is distilled into the
  archive, later upstream format churn cannot retroactively damage it. The index
  is the durable artifact; the raw file is a transient input.

## Corollary

Because the input is unstable and the output is a system of record, **ingest must
be idempotent and losslessness must be a test**. Re-running ingest against a
changed upstream format must never corrupt or drop what was already archived.
