# Specs

Implementation specs for Muninn. Each is self-contained enough to hand to an
implementer, and each names the wiki articles that carry the reasoning behind it.

The division of labour is deliberate: **the wiki says *why*, the spec says
*what*, the tests say *whether*.** If a spec and a wiki article disagree, the
wiki wins and the contradiction is a bug worth reporting.

| Spec | Status | Depends on |
|---|---|---|
| [001 — Import ledger](001-import-ledger.md) | ready | — |
| [002 — Export importers](002-export-importers.md) | ready | 001 |
| [003 — Background indexer](003-background-indexer.md) | ready | 001 |
| [004 — Structured filters](004-structured-filters.md) | ready | — |
| [005 — Enrichment](005-enrichment.md) | implemented | 001, 004, 011 |
| [006 — Hybrid retrieval](006-hybrid-retrieval.md) | implemented | 004, 005 |
| [007 — Tiered retention](007-tiered-retention.md) | ready | 001, 005 |
| [008 — Plugin contract](008-plugin-contract.md) | ready | — |
| [009 — Raven descriptor and `/api/menu`](009-raven-descriptor-menu.md) | implemented | 003 |
| [010 — The daemon](010-daemon.md) | implemented | 003, 009 |
| [011 — Survey and calibration](011-survey-calibration.md) | implemented | 001, 003 |
| [012 — Prose-index backfill](012-prose-index-backfill.md) | implemented | 001 |
| [013 — `muninn resume`](013-resume.md) | implemented | 004 |
| [014 — Automatic embedding](014-automatic-embedding.md) | implemented | 006, 010 |
| [015 — Provider selection](015-provider-selection.md) | implemented | 005, 008 |
| [016 — Cost estimation](016-cost-estimation.md) | implemented | 011, 005, 006 |
| [017 — Menu lifecycle actions](017-menu-lifecycle-actions.md) | implemented | 009, 010 |
| [018 — Automatic enrichment](018-automatic-enrichment.md) | implemented | 014, 016, 011, 005 |
| [019 — `muninn recall`](019-recall.md) | implemented | 005, 006, 009 |
| [020 — `muninn why`](020-why.md) | implemented | 005, 019 |

Specs 002 and 003 both modify `muninn/cli.py`, as do 004, 005, 010 and 011. Run
overlapping specs sequentially, or in separate git worktrees, so they cannot
collide.

Spec 011 unblocks **005**: enrichment's gate reads `calibration.json`, and until
011 landed nothing wrote that file, so 005 could not be implemented as written.

Spec 007 deletes data on purpose and is the most dangerous of the set. Its one
inviolable rule: never drop prose for a session whose raw source is already gone.

Spec 009 covers only Muninn's **producer** side of the shared menubar — the raven
descriptor and the `/api/menu` payload. It publishes no console: `/` and
`/session/<id>` are deliberately stubs, because a real UI on that port would carry
transcript prose and would force spec 009's "unauthenticated by design" decision
to be reopened.

Spec 010 answers the owner decision spec 009 left open ("The lifecycle question"):
Muninn now has a daemon, `muninn serve`, and it — not `muninn index --watch` — is
what publishes the descriptor. **Read 010 before 009's lifecycle section**, which
010 supersedes on that one point and on nothing else. `index --watch` remains as
the foreground/debug ingest path.

Spec 010 also covers the **login-agent installer** (`muninn install-agent` /
`uninstall-agent`), which it originally deferred as a follow-up seam and now
records as filled. The mechanism is the shared `corvidae` package's
`LoginAgentSpec`/`LoginAgent`, not a second copy in this repo, and the daemon
itself needed no change — see 010, "The login-agent installer". Muninn's launchd
label, plist, systemd unit, log path and Windows Run value are all disjoint from
Huginn's, because both ravens are meant to be installed at once and every
collision would be silent.

Spec 014 moves embedding from a command a human types to work the daemon owns.
It changes nothing about how vectors are stored or searched — spec 006 still
governs all of that — only *when* they are generated. Read it before concluding
from 006 that `muninn embed` is the only way vectors appear.

Spec 015 adds a second text provider (`codex exec`) and lets a plugin *declare*
the default one. It reverses a decision spec 008's `resolve_provider` recorded —
read 015's "Why" before concluding the old refusal still stands — and it changes
nothing about which models are *permitted*: selection and permission stay
separate, with `muninn.policy` the only chokepoint.

Spec 019 is the first retrieval path that does not take a query, and it is the
first consumer of `outcome` beyond a filter flag. It depends on 005 in a way the
others do not: with enrichment unrun its main section is not merely empty but
*unknowable*, which is why the payload distinguishes the two silences rather
than leaving a caller to infer one.

Spec 020 joins the archive to git: `git blame` answers who and when, and the
reasoning that produced a line lives in a conversation. It is the second path
after 019 to take a *thing* rather than a question, and it records two
measurements worth reading before writing anything that attributes work to a
session — session lifetimes span four orders of magnitude, and `cwd` is not the
repository.

Later phases not yet spec'd: the console, the agent skill, and the Cisco
distribution's plugins.

## How to work one of these

1. Read the wiki articles listed at the top of the spec first. They exist because
   the reasoning is not re-derivable from the code, and several of them record
   measurements that contradict reasonable assumptions.
2. Follow the build order. Each step ends with a green test run.
3. Treat the acceptance criteria as the definition of done — one test per
   invariant, no exceptions folded together.
4. Existing test files named as contracts (`test_losslessness.py`,
   `test_ledger.py`) must pass **unmodified**. If you believe one is wrong, stop
   and say so rather than editing it. They encode guarantees about data that
   cannot be recovered if lost.
5. If an invariant seems to be blocking a test from passing, that is a finding,
   not an obstacle to route around. Report it.

## Verify against the real corpus, not just fixtures

Every spec's definition of done includes a command that runs against the real
archive. That is deliberate and it is not optional.

Spec 001 shipped with 31 passing tests and a clean lint, and crashed on the very
first real run: 11 of 388 transcripts in the real corpus share a session id with
another file under a different encoded `cwd` (a renamed or symlinked repo is
reachable by two paths). Every fixture had unique ids, so no test could have
caught it.

**Fixtures encode what you expect; only real data encodes what is true.** Run the
real-corpus command before reporting done.

## Why the guardrails are worded so strictly

Muninn is an archive of record. Claude Code deletes transcripts after
`cleanupPeriodDays` (default 30), so for much of a corpus this archive is the
only surviving copy. A subtle ingest bug does not corrupt data you can re-derive
— it destroys the only copy, silently, and the loss is discovered months later
when someone searches for something that should be there.

That is also why several specs ask for enumerated lists where a count would be
simpler: a count cannot be audited after the fact, and every silent skip in the
predecessor tools was a data-loss path nobody noticed.
