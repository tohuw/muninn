---
type: "Knowledge Article"
title: "Continuous ingest, not periodic"
description: "A periodic indexer plus a deleting source loses data silently; Muninn needs a lightweight always-on indexer."
tags: ["ingest", "daemon", "hooks", "durability", "signals"]
timestamp: "2026-07-30T00:00:00Z"
category: "durability"
status: "current"
updated: "2026-08-01"
summary: "claudex's cron-style indexer was 7 days stale in practice, with 148 unindexed transcripts. Combined with Claude Code's 30-day sweep, any session created and pruned between runs is lost forever. Muninn runs a lightweight background indexer plus a SessionEnd hook, with a sweep as backstop — owned since spec 010 by a real daemon, because a loop a human has to remember to start is not a guarantee."
related: ["archive-of-record", "unstable-jsonl-format", "provenance-classification", "shared-menubar", "lessons-for-huginn"]
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

## The layers need an owner, and for a while they had none

The three layers above describe *what* runs. They do not say what keeps layers 2
and 3 running, and for two specs that question had no answer: `muninn index
--watch` was a command a human typed. So the durability argument this whole
article makes reduced to "as long as someone remembers", which is the same
structural flaw as cron with a different trigger — and, per the measurement above,
"probably captured" is exactly what is not good enough against a source that
deletes.

Since docs/specs/010-daemon.md there is a daemon, `muninn serve`, and **owning the
loop is the whole of its job.** It does not reimplement it: `indexer.watch()` is
still the engine, still sweeps before watching, still drains the queue every
iteration. The daemon adds the lifecycle around it — a state file an external
supervisor can read, an advisory lock so two loops cannot both drain one queue,
and a teardown that survives the signal a service manager actually sends.

Two things learned building it, both worth keeping:

- **A stop signal that does not unwind is worse than a crash**, because it looks
  like one. Python's default `SIGTERM` disposition kills the process without
  running any `finally:`, so the raven descriptor survives naming a dead port. That
  makes "stopped by launchd" indistinguishable from "crashed", and Huginn shipped
  exactly this (its issue #43) with its Quit menu item as the trigger — the common
  path, not an edge case. `SIGHUP` has the same effect and is easier to forget: a
  daemon started from a terminal that then closes gets it.
- **The failure from two ingest loops is not corruption, it is a clobbered
  descriptor.** The import ledger already serialises the imports themselves. What
  it does not protect is the lifecycle: the loop that loses the race deletes the
  winner's descriptor on its way out, so a healthy daemon silently leaves the
  menubar.

## The supervisor installer, and the crash loop the lock does not prevent

The supervisor *installer* — launchd, systemd, Windows startup — now exists as
`muninn install-agent`, built on the shared `corvidae` package rather than written
twice. See [[lessons-for-huginn]] #5, which called this out before either project
acted on it, and `docs/specs/010-daemon.md` for the concrete values.

Two things learned filling that seam, both about the *interaction* between a
supervisor and the single-instance lock rather than about either alone:

- **An exit code the lock produces becomes a crash loop once a supervisor is
  reading it.** `muninn serve` exits 1 when the lock is held, which is right on its
  own — a supervisor must not report a daemon it did not start as running. But
  launchd's `KeepAlive` relaunches a process that exits 1 forever, and systemd's
  `Restart=on-failure` does the same until it gives up and leaves the unit
  `failed`. So a perfectly healthy `muninn index --watch` in a terminal turns a
  fresh install into a log full of "already running". The lock prevented the data
  failure and *created* a lifecycle one, at the exact moment the user asked for a
  service. `install-agent` therefore refuses while a loop holds the lock, rather
  than the daemon changing how it exits.
- **A supervisor does not inherit the environment of the shell that installed
  it.** Measured: an install run with `HOME`, `XDG_STATE_HOME` and
  `RAVENS_STATE_DIR` all redirected to a tempdir produced a daemon that ingested
  into the real archive, because launchd starts the agent from its own
  environment. This is the same class of invisibility this article is about — the
  install reported success and the daemon ran, and only the log said where. Not
  worked around: capturing a terminal's transient state into config that runs at
  every login for years is worse than the surprise, and an `EnvironmentVariables`
  plist key is the exact thing Huginn's #41 XML injection manufactured out of a
  directory name.

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
