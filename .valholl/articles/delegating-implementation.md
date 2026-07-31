---
type: "Knowledge Article"
title: "Delegating implementation to subagents"
description: "What worked and what destroyed work when handing Muninn specs to implementer agents."
tags: ["process", "agents", "specs", "worktrees", "pitfalls"]
timestamp: "2026-07-31T00:00:00Z"
category: "process"
status: "current"
updated: "2026-07-31"
summary: "Muninn's specs are written to be handed to implementer agents. The pattern works — spec 001 landed with 32 passing tests and correct invariants — but two process failures cost real work: worktree isolation branched from the wrong repository, and an uncommitted recovery was destroyed by cleanup. Records the fixes."
related: ["deterministic-imports", "import-ledger-schema", "session-lifecycle-facts"]
---

# Delegating implementation to subagents

Muninn is planned by one agent and largely implemented by others. `docs/specs/`
exists for that reason: each spec is self-contained, names the wiki articles
carrying its reasoning, and states acceptance criteria as one test per invariant.

The division that makes this work: **the wiki says *why*, the spec says *what*,
the tests say *whether*.** An implementer that has all three does not need the
planning conversation.

## What worked

Spec 001 (the import ledger) landed with 32 passing tests, clean lint, and every
invariant correctly implemented — verified by reading the code, not just the test
summary. Three things made that possible:

1. **Guardrails phrased as prohibitions with reasons.** "Do not modify
   `tests/test_losslessness.py`; it is a contract about data that cannot be
   recovered" is followed. "Please be careful" is not.
2. **An explicit escape hatch.** Every spec says: if an invariant seems to block a
   test from passing, that is a finding to report, not an obstacle to route
   around. The implementer used it correctly — it reported a real defect rather
   than weakening a constraint.
3. **A real-corpus command in the definition of done.** This is what caught the
   duplicate-session-id bug that 31 fixture-based tests missed.

## Failure 1: worktree isolation branches from the caller's directory

Two implementer agents were dispatched with worktree isolation while the planning
session's working directory was a *different repository*. Both received a worktree
of that unrelated repo, containing none of the files their prompts told them to
read.

Both handled it correctly: they refused to write Muninn code into an unrelated
repo and reported the mismatch. That refusal is the behavior to want — an agent
that "made it work" would have committed spec changes onto a foreign history.

**Fix:** do not rely on automatic worktree isolation across repositories. Create
the worktree explicitly (`git worktree add` inside the target repo) and pass the
absolute path in the prompt, with an instruction not to create others.

## Failure 2: cleanup destroyed an uncommitted recovery

One of those agents recovered on its own — it located the real repository, created
a correct worktree, and wrote a new module plus edits to another file. It then
paused to ask whether to keep or discard that work, exactly as it should have.

Before that report was read, the stray worktrees were cleaned up with
`git worktree remove --force` and the branch deleted. The work was never
committed, so nothing was recoverable: no dangling blobs, no reflog entry.

Nothing was lost from `main`, and the spec made the work reproducible. But it was
avoidable.

**Fixes:**

- **Read an agent's report before cleaning up anything it touched.** A background
  agent may have done useful work in an unexpected place.
- **`--force` on `git worktree remove` discards uncommitted changes silently.**
  Run without `--force` first; the refusal is information.
- **Instruct implementers to commit early**, even mid-task on a scratch branch.
  Uncommitted work in a worktree has no recovery path, and the cost of an extra
  commit is nothing.

## The transferable lesson

Both failures were *process* failures by the delegating agent, not implementation
failures by the implementers. Both implementers behaved better than the
orchestration did: they detected an inconsistent environment, declined to force
progress, and asked.

That is worth designing for explicitly. An implementer prompt should make it
easy to stop and ask — and the orchestrator should be slower to destroy state
than to create it, which is the same asymmetry [[archive-of-record]] asserts
about data.
