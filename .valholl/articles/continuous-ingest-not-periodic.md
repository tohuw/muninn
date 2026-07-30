---
type: "Knowledge Article"
title: "Continuous ingest, not periodic"
description: "A periodic indexer plus a deleting source loses data silently; Muninn needs a lightweight always-on indexer."
tags: ["ingest", "daemon", "hooks", "durability"]
timestamp: "2026-07-30T00:00:00Z"
category: "durability"
status: "current"
updated: "2026-07-30"
summary: "claudex's cron-style indexer was 7 days stale in practice, with 148 unindexed transcripts. Combined with Claude Code's 30-day sweep, any session created and pruned between runs is lost forever. Muninn runs a lightweight background indexer plus a SessionEnd hook, with a sweep as backstop."
related: ["archive-of-record", "unstable-jsonl-format", "provenance-classification"]
---

# Continuous ingest, not periodic

## The evidence

`claudex` is designed to be refreshed by cron (`0 3 * * *`). Measured on
2026-07-30, its index had last been built on **2026-07-23** — seven days stale,
with **148 raw transcripts newer than the index**, including the very session
that discovered the problem.

This is not a claudex bug; it is a structural flaw in periodic indexing of an
ephemeral source. With a 30-day delete sweep on one side and a daily-at-best
indexer on the other, the guarantee is only ever "probably captured." Any
session created and pruned between successful runs is gone with no error and no
trace. A stale index also silently fails the thing users most expect to work:
searching for what they did *this week*.

## The design

Three layers, cheapest first:

1. **`SessionEnd` hook** — fires when a session ends and officially receives
   `transcript_path`. This is the primary path: near-real-time, event-driven,
   no polling. A session is archived seconds after it finishes.
2. **Lightweight background indexer** — a long-running watcher (Huginn already
   depends on `watchfiles`) over the source directories, catching sessions whose
   hook did not fire: crashes, `kill -9`, hook misconfiguration, other agents.
   It must be genuinely cheap — idle CPU near zero, work proportional to change,
   and incremental via byte offsets rather than re-reading whole files.
3. **Sweep on start** — a reconciliation pass that diffs source directories
   against the index and backfills gaps. This is the backstop that makes the
   guarantee closable rather than best-effort.

## Requirements

- **Idempotent.** Re-ingesting a session must be a no-op, not a duplicate.
- **Incremental.** Append-only transcripts are tailed from a stored offset, with
  recovery for truncation and rotation. Reuse Huginn's `sources/transcript.py`
  `Tail` rather than reimplementing it.
- **Observable.** `muninn doctor` reports index lag (newest source file vs.
  newest indexed) and warns when lag exceeds a threshold. Staleness must be
  *visible*; the claudex failure was invisible.
- **Never blocks the agent.** Ingest work happens out of band; a slow or failing
  indexer must never delay a Claude Code session.

## Cross-platform, best effort

The indexer targets macOS, Windows, and Linux — **best effort**, with the same
candor Huginn shows about untested surfaces. Structure it like Huginn's
`platform/` package (`base.py` + per-OS modules) so OS-specific behavior is
isolated and testable rather than smeared through the ingest path.

Platform-specific concerns:

- **Paths.** `~/.claude` and `~/.codex` are the same on POSIX; on Windows the
  config/state split differs (`%APPDATA%` vs `%LOCALAPPDATA%`), and `$CODEX_HOME`
  must be honored everywhere.
- **Background execution.** launchd (macOS), systemd user unit or a plain
  long-running process (Linux), Scheduled Task or a tray-owned process
  (Windows). Huginn already ships `agent_install.py` for launchd only — Muninn
  needs the other two, and this is a good candidate to contribute upstream.
- **File watching.** `watchfiles` covers all three, but Linux inotify watch
  limits are a real failure mode at this file count (thousands of transcripts);
  fall back to polling when the limit is hit rather than dying.
- **WSL.** Sessions may live inside a distro. Huginn already solves this by
  executing a helper *inside* the distro and passing normalized JSON across the
  boundary; reuse that approach rather than reaching into `\\wsl$` paths.
- **File locking.** Windows disallows deleting/renaming open files, so the
  atomic temp-file + replace pattern needs care; SQLite WAL access can also be
  blocked, which Huginn already works around for the Codex state DB by keeping a
  snapshot copy.

Best-effort means: degrade to a documented lesser mode rather than failing, and
report actual platform status honestly in `doctor` and in a `WINDOWS.md`-style
doc, as Huginn does ("never once tested on a real Windows machine").

## Lesson for Huginn

Huginn already runs a daemon and already watches these exact directories. It
has no notion of *lag reporting* for the data it derives. The
`doctor`-reports-lag pattern is worth porting upstream — see
[[lessons-for-huginn]].
