---
type: "Knowledge Article"
title: "Session lifecycle facts, documented vs. observed"
description: "What Claude Code guarantees about hooks and transcripts, what it does not, and what was measured directly."
tags: ["ingest", "hooks", "sessions", "risk", "observed-behavior"]
timestamp: "2026-07-31T00:00:00Z"
category: "ingest"
status: "current"
updated: "2026-07-31"
summary: "SessionEnd delivers transcript_path but shares a 1.5s budget across all SessionEnd hooks, cannot run async, and is not guaranteed to fire on crash or SIGKILL. So a hook may only enqueue work, never index. Subagent transcripts were measured to carry agentId and isSidechain, giving a reliable classifier the docs do not document."
related: ["continuous-ingest-not-periodic", "unstable-jsonl-format", "provenance-classification", "archive-of-record"]
---

# Session lifecycle facts, documented vs. observed

Muninn's indexer depends on when transcripts appear, grow, and vanish. This
article separates what the vendor documents from what was measured here, because
the difference determines which failure modes must be designed around.

## Documented

From the [hooks reference](https://code.claude.com/docs/en/hooks.md),
[sessions](https://code.claude.com/docs/en/sessions.md), and
[settings](https://code.claude.com/docs/en/settings.md):

- **`SessionEnd` payload** carries `session_id`, `transcript_path`, `cwd`,
  `hook_event_name`, and `reason`. `reason` is one of `clear`, `resume`,
  `logout`, `prompt_input_exit`, `bypass_permissions_disabled`, `other`.
- **`SessionEnd` shares a 1.5-second budget across all `SessionEnd` hooks**, does
  **not** support `async: true`, ignores `matcher`, and cannot block session exit.
- **`SubagentStop`** provides `agent_id` and `agent_type` but **not** a transcript
  path.
- **`cleanupPeriodDays` sweeps at startup only**, not periodically. An active
  session's own transcript is not at risk mid-session.
- **The JSONL entry format is internal and changes between versions.** Direct
  parsers can break on any release.

## Not documented (design defensively)

The vendor does not specify, so Muninn must not assume:

- Whether `SessionEnd` fires on Ctrl-C, Ctrl-D, window close, SIGKILL, or crash.
  The catch-all `other` reason implies gaps. **Assume it can be missed.**
- Whether transcript writes are incremental, buffered, or fsynced.
- Whether a mid-session tail can observe a partial final line.
- Whether `/compact` rewrites, truncates, or rotates the transcript.
- Whether `/clear` continues the same file or starts a new one.
- Subagent transcript path layout, and whether a subagent's `sessionId` is its
  own or its parent's.
- Any locking for concurrent sessions sharing a `cwd`.

## Observed here (2026-07-31)

Measured directly, not documented anywhere:

- **Subagent transcripts carry `agentId` and `isSidechain: true`.** In
  `agent-a71a4b6c142e411a7.jsonl`, `agentId` matched the filename stem and
  `isSidechain` was `true` on every entry.
- **A subagent's `sessionId` is its PARENT's.** The file's `sessionId` equalled
  the enclosing directory name, which is the parent session id. Trusting that
  field collapsed 251 subagent transcripts onto their parents and silently
  dropped 725,706 words — see [[provenance-classification]].
- Layout is `~/.claude/projects/<encoded-cwd>/<parent-session-id>/subagents/agent-<id>.jsonl`.

**Consequence:** `isSidechain` and `agentId` are the preferred subagent signals,
with the path layout as a fallback. Both are observed rather than contractual, so
a parse failure must degrade to a counted skip rather than an exception — the
discipline [[unstable-jsonl-format]] already requires.

## Design consequences for the indexer

1. **A hook may only enqueue, never index.** With 1.5 s shared and no async, real
   work cannot happen inside `SessionEnd`. The hook appends a small job record
   and exits immediately; the watcher or a sweep does the parsing.
2. **The sweep is not optional.** Because `SessionEnd` can be missed entirely, a
   reconciling scan is the only thing that closes the guarantee. This is the same
   conclusion [[continuous-ingest-not-periodic]] reached from the opposite
   direction — a cron indexer alone was 7 days stale.
3. **Subagents need their own trigger path.** `SubagentStop` gives no transcript
   path, so subagent transcripts are discovered by the watcher and sweep, keyed on
   `agentId`, never by hook payload.
4. **Never trust a final line.** Because flush behavior is undocumented, the
   tailer stops at the last newline-terminated record and re-reads from there.
5. **Treat `/compact` and `/clear` as possible rewrites.** If a file's size
   shrinks or its digest changes below the stored offset, re-parse it whole rather
   than appending.
