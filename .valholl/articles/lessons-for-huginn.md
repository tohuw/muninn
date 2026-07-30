---
type: "Knowledge Article"
title: "Lessons for Huginn"
description: "Improvements Muninn's design surfaced that should be contributed back upstream to Huginn."
tags: ["huginn", "upstream", "extensibility", "contributions"]
timestamp: "2026-07-30T00:00:00Z"
category: "extensibility"
status: "current"
updated: "2026-07-30"
summary: "Building Muninn surfaced concrete weaknesses in Huginn: an exact-match plugin API version, an imperative menubar with no extension seam and a stale hardcoded path, no lag reporting for derived data, launchd-only background install, and no model-policy chokepoint. Each is a candidate upstream contribution."
related: ["shared-menubar", "continuous-ingest-not-periodic", "model-policy-chokepoint", "provenance-classification"]
---

# Lessons for Huginn

Designing Muninn as a genuine companion (rather than a fork) exposed several
things worth fixing in [Huginn](https://github.com/tohuw/huginn) itself. Each is
listed with the evidence that motivated it. Note Huginn's default branch is
`master`, not `main`.

## 1. Exact-match `API_VERSION` is a footgun

`huginn/plugins.py` requires `api_version == API_VERSION` exactly, with no
compatibility range. Huginn's own `.valholl` notes the consequence: a routine
upstream bump **silently disables every plugin**, visible only in
`huginn doctor`, with no crash and no warning.

**Fix:** support a declared compatibility range (`min_api`/`max_api`), and make a
version mismatch a loud, surfaced error rather than a quiet no-op. Muninn adopts
ranges from the start.

## 2. The menubar has no extension seam, and is imperative

`macos/HuginnMenuBar.swift` rebuilds its entire menu via `removeAllItems()` on a
3-second timer. There is no way for another app to contribute items. Huginn's
only documented extension point is the `huginn.plugins` entry-point group, which
models *live, state-bearing* sessions — a poor fit for history.

**Fix:** menu-as-data. Render a declarative JSON menu spec fetched from each
discovered raven, so companions contribute menu content with zero Swift changes.
This also retires the teardown-and-rebuild cycle. See [[shared-menubar]].

## 3. Stale hardcoded path

`repoPath` in `HuginnMenuBar.swift` is hardcoded to `/Users/hljod/Projects/huginn`
— a path that does not exist on the current machine. It works only because the
daemon is usually already running.

**Fix:** derive the repo path from the bundle location or a state file.

## 4. No lag reporting for derived data

Huginn watches `~/.claude` and `~/.codex` and derives state from them, but has no
notion of *how stale* its derived view is. The failure this predicts was observed
in the sibling tool `claudex`: a cron-driven index was **7 days stale with 148
unindexed transcripts**, silently.

**Fix:** report data lag (newest source artifact vs. newest processed) in
`doctor`, and warn past a threshold. Staleness must be visible. See
[[continuous-ingest-not-periodic]].

## 5. Background install is launchd-only

`huginn/agent_install.py` supports macOS launchd only, though Huginn ships a
Windows tray app and `WINDOWS.md` documents platform gaps candidly.

**Fix:** add systemd user units (Linux) and Scheduled Task / tray-owned process
(Windows). Muninn needs all three anyway; the implementation should be shared.

## 6. No model-policy chokepoint

Huginn's plugin registry is purely **additive** — plugins contribute providers and
nothing can veto one. An organization cannot express "only these models may be
used," because anything one plugin restricts, another can re-add.

**Fix:** a fail-closed policy chokepoint where contributed policies *intersect*
rather than union. See [[model-policy-chokepoint]].

## 7. Provenance blindness

Huginn generates blurbs via `claude -p`, and those invocations land in
`~/.claude/projects` looking like sessions. On the development machine this
produced **3,534 phantom sessions — 92% of the corpus**. Huginn already filters
`entrypoint: sdk-cli` for *live* session display, but its own writes are
indistinguishable from user work to any downstream consumer.

**Fix:** have Huginn mark its own invocations distinctly (a dedicated cwd is
already a de facto marker; an explicit one would be better), so downstream tools
can exclude them reliably. See [[provenance-classification]].

## 8. Ask/blurb calls litter `~/.claude/projects` with orphaned transcripts

`huginn/llm/chat.py` runs `claude -p` with `cwd=<CACHE_DIR>/chat` and then
`shutil.rmtree`s that directory in a `finally` block (lines 277 and 335). Claude
Code derives its per-project transcript directory from cwd, so every such call
writes a transcript under a path that Huginn then deletes.

Observed consequence on the development machine: **3,534 transcripts**
accumulated under `~/.claude/projects/-Users-tohuw--local-state-huginn-cache/`
— 90% of all entries in that tree. Nothing surfaced the accumulation; it was
found only by surveying the corpus. They were subsequently deleted by hand as
pure bug residue, which is the correct disposition: they have no archival
value and should never have been persisted.

Two distinct problems:

1. **Pollution.** Huginn's internal LLM calls are indistinguishable from user
   sessions to any downstream consumer, and dominate the directory by count.
   This is [[provenance-classification]] with teeth: it is Huginn *creating* the
   contamination that skewed every corpus statistic by ~40x.
2. **Orphaning.** The transcript outlives the directory that gives it meaning,
   so the entries are unattributable after the fact and can only be cleaned up
   by recognizing the cwd pattern.

**Fixes, in order of preference:** set `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` (or
`--no-session-persistence`) for internal invocations so they never persist at
all; failing that, use a stable dedicated cwd rather than one that is created and
destroyed per call, so the entries are at least attributable and prunable.

## 9. Documented reusable internals

Muninn wants `sources/transcript.py` (`Tail`, the dialect analyzers),
`llm/context.py` (`redact_secrets`), and `model.py`. These are genuinely good and
genuinely reusable, but carry no compatibility guarantee, forcing a commit-pin
relationship.

**Fix:** either declare a small stable surface for them, or extract them into a
shared package both projects depend on. The pin works; it just means every
upstream refactor is a coordination cost.
