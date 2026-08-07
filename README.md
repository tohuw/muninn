# muninn

A local-only console for **agent history** — what your AI agents did, across
Claude Code, Codex, and vendor data exports. Fast search, quick resume, and — as
they land — correlation of similar conversations and context briefs that read
equally well to humans and agents.

_Developed with AI assistance. See the git history for which agents contributed._

Huginn is Thought; Muninn is Memory. [Huginn](https://github.com/tohuw/huginn)
answers "what are my agents doing right now." Muninn answers "what did we do,
decide, and learn." They are complementary and share a single menubar surface —
[Roost](https://github.com/tohuw/roost) — because nobody wants two ravens up
there.

> **Status: early, but no longer minimal.** Storage, ingest, the daemon, search
> with structured filters, corpus calibration, enrichment and hybrid retrieval
> all work and are covered by tests. The console is not built. Semantic search
> needs `uv sync --extra semantic` (or a plugin) — without a provider it refuses
> rather than quietly falling back to lexical. See [Roadmap](#roadmap).

## Why this exists

Three problems that `grep` over your transcripts does not solve.

**Your transcripts are being deleted.** Claude Code sweeps session JSONLs older
than `cleanupPeriodDays` (default **30 days**) on startup, and subagent
transcripts go with their parent. On the machine this was developed on, the
oldest surviving transcript was 29 days old and everything older was already
gone. Muninn's index is an archive of record: for much of a corpus it is the only
surviving copy.

**You do not remember your own wording.** You remember the *situation*. That is a
recall problem, not a ranking problem, which is why retrieval is hybrid
(lexical + semantic) rather than a better regex.

**You want the moment something was decided**, not every line where a word
appears. That needs enrichment at index time, not smarter matching at query time.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/tohuw/muninn.git
cd muninn
uv sync
```

## Usage

```sh
# Run it as a service. This is the normal way to run Muninn.
uv run muninn serve
uv run muninn install-agent   # ...and have it start at login

# Ingest local transcripts once, by hand. Idempotent; safe to run repeatedly.
uv run muninn index

# Search the archive.
uv run muninn search "extension point"
uv run muninn search "auth redirect" --since 2026-06 --repo muninn

# What did I do last week, in order.
uv run muninn log --since 2026-07

# Read one session in full (id prefixes are fine).
uv run muninn show a7efca23

# Reopen a session in the tool that created it — if its transcript still exists.
uv run muninn resume a7efca23

# Import a claude.ai or ChatGPT data export.
uv run muninn import ~/Downloads/data-2026-08-01.zip

# Survey your corpus and derive calibration (see below).
uv run muninn survey

# Extract topic/outcome/decisions from substantive sessions. Costs model calls —
# --dry-run tells you how many before you spend any.
uv run muninn enrich --dry-run
uv run muninn search "the retry decision" --outcome fixed

# Health: index lag, queue, calibration drift, daemon and login-agent state.
uv run muninn doctor
```

Coming from `claudex` or `codexdex`? `uv run muninn backfill` ingests their prose
indexes — see [Superseding the predecessors](#superseding-the-predecessors).

### Run the daemon

`muninn serve` is what makes the archive-of-record guarantee real. It sweeps on
startup, then drains the `SessionEnd` queue and reacts to transcript changes for
as long as it runs — so a session written while nothing was watching is still
recovered, and a session deleted by Claude Code's 30-day sweep was captured
before it went. It also publishes Muninn's row in the shared menubar. **It is the
normal way to run Muninn**, and `install-agent` below is how you stop having to
remember it; `muninn index` remains a one-shot ingest for when you want to watch
one happen.

It is built to be supervised, not to supervise itself: it records
`~/.local/state/muninn/daemon.json` (pid, port, and where to relaunch it from) and
exits cleanly on `SIGTERM`, `SIGHUP` and Ctrl-C, withdrawing everything it
published. `muninn doctor` reports whether it is running, its port, and whether
its menubar descriptor is published.

Only one ingest loop may run at a time — a second is refused, naming the first —
because two loops would drain one queue twice and fight over one descriptor.
`muninn index --watch` is still the foreground/debug path: the same ingest loop
with no port and no state file, for watching ingest happen on a console.

```sh
uv run muninn install-hooks          # a SessionEnd hook, so a session is queued
uv run muninn install-hooks --check  # the moment it ends. --check never writes.
```

The hook only ever *enqueues*: it shares a 1.5 s budget with every other
`SessionEnd` hook, so it must not open the archive. The daemon drains the queue.

See [`docs/specs/010-daemon.md`](docs/specs/010-daemon.md).

### The shared menubar

While `muninn serve` runs it publishes a **raven descriptor** into a shared
directory and answers `GET /api/menu` on loopback. The menubar that reads those is
**[Roost](https://github.com/tohuw/roost)** — a separate Apache-2.0 project, one
macOS menu bar / Windows tray item that renders whichever ravens are running.
Install it from its own repository; Muninn does not ship, depend on, or install
it, and publishing is best-effort, so ingest never pays for a menubar.

Roost's `SPEC.md` is normative for the wire format. Muninn's producer side is
[`docs/specs/009-raven-descriptor-menu.md`](docs/specs/009-raven-descriptor-menu.md),
including the two decisions worth knowing before reading the code: every row is a
link (Muninn publishes no action endpoint), and there is no `token_path`, so
`/api/menu` is unauthenticated by design and the `Host`/`Origin` checks are the
only thing defending that port.

**Muninn is absent from the menubar when its daemon is not running.** That is a
legitimate steady state, not a bug: no descriptor exists, and Roost draws nothing
for a raven it cannot find — the same as one that was never installed. A crashed
daemon's stale descriptor renders as "Not running" with the reason on screen,
because Roost checks the recorded pid.

### Start it at login

```sh
uv run muninn install-agent      # macOS: a LaunchAgent; Linux: a systemd user unit;
                                 # Windows: an HKCU Run entry
uv run muninn uninstall-agent    # removes it, and stops the daemon it supervises
```

The mechanism is the shared [`corvidae`](https://pypi.org/project/corvidae/)
package, so Huginn and Muninn share one implementation rather than each carrying a
copy. **Both ravens can be installed at once** — that is the point of a shared
menubar — and every path, label and registry value Muninn uses is disjoint from
Huginn's.

What each platform actually gives you differs, and the difference is not papered
over: macOS restarts the daemon if it dies, Linux restarts it on *failure* only
(so `systemctl --user stop muninn` stays effective, and a headless host also wants
`loginctl enable-linger $USER`), and the Windows Run key starts it once per login
and never restarts it.

`install-agent` **refuses while an ingest loop is already running**, because a
supervisor relaunching a process that exits immediately is a restart loop rather
than a service. Stop the loop and run it again; `muninn doctor` names what holds
the lock, and now also reports whether a login agent is installed.

One thing to know before relocating state: the installed agent runs from the OS's
environment, **not** the shell you installed from, so `XDG_STATE_HOME` and
`RAVENS_STATE_DIR` exported in a terminal have no effect at login. Set them
somewhere login sessions see (`launchctl setenv`, a systemd user drop-in).

`install-agent` checks this rather than leaving you to find out from a log file.
If the paths your shell resolves are not the ones a login session will, it
refuses and prints both sides — because the failure mode is not an error, it is a
daemon that comes up every morning ingesting a different archive than you think.
Pass `--force` if you have already set them where login sees them. `muninn
doctor` reports the same divergence for an agent that is already installed, which
catches an environment that changed afterwards.

## How it works

### Raw transcripts are the source of truth

Muninn parses `~/.claude/projects/**/*.jsonl` and
`~/.codex/sessions/**/rollout-*.jsonl` directly. An earlier prototype calibrated
from a *derived* prose index and undercounted conversations by 15–27%, because
that index was 7 days stale. Deriving facts from a derived artifact compounds
staleness.

Because the transcript format is explicitly **not a stable API** and can change
on any release, every adapter fails soft: a malformed record or missing field is
a counted parse failure, never an exception. Parse-failure rates are reported so
a format change shows up as a rising rate rather than as silently missing
history.

### Provenance is first-class

Not every entry in `~/.claude/projects` is a conversation. Sessions are
classified structurally as **human**, **tool-invoked**, or **subagent**.

This matters more than it sounds. On the development corpus, **92% of Claude
"sessions" were programmatic `claude -p` calls** made by another tool. Pooling
them with real conversations made the corpus look 40× larger and the median
session 16× shorter, which in turn produced wrong retention estimates, wrong
growth rates, and a badly mis-tuned enrichment threshold. Every statistic Muninn
reports is scoped to a provenance class.

The archive-of-record guarantee covers human and subagent sessions.
Tool-invoked prose is prunable — it is a reproducible byproduct of some other
tool's call volume.

### Thresholds are derived, never hard-coded

`muninn survey` measures your *present* corpus and writes an inspectable
`calibration.json` beside the archive.

A fixed threshold encodes one person's habits as everyone's defaults. A proposed
"enrich sessions ≥300 words" rule selected 37% of Claude sessions but 91% of
Codex sessions — the same constant meaning two different policies depending on
which agent you favor. Derived gates on the same corpus landed at 4,046 and
2,480 words respectively, both hitting ~85% text coverage. What is held fixed is
the **coverage**; the word count is whatever your corpus needs to reach it.

Every statistic is scoped to a provenance class, and the survey's first act is to
report what is strange about your data — a tool-invoked majority, a source with
no human sessions, sessions whose only copy is the archive.

`muninn doctor` recommends re-surveying when the corpus shape drifts: the corpus
doubles, a source appears, the source mix shifts, or the stored gate stops doing
what it was derived to do. (Query-latency regression is named in the design notes
and is **not** measured — it needs a benchmark harness.)

### Search

SQLite FTS5 over prose chunks. Measured on a real corpus: **0.8 s to index, 33 MB,
0.1–1.9 ms queries**. Scaling tests to 162k chunks kept phrase queries under 1 ms
while broad `OR` queries degraded linearly to 45 ms — which is why query
expansion is capped rather than unbounded.

Semantic recall is optional and pluggable via an `EmbeddingProvider` protocol.
`muninn embed` generates vectors; `search --semantic` fuses them with the lexical
results by reciprocal rank, and `muninn correlate` answers "conversations like
this one" — best on short and medium sessions; see
[spec 006](docs/specs/006-hybrid-retrieval.md) for a measured caveat about very
long ones. Without a provider installed, `--semantic` **exits non-zero and says
so** — it never returns lexical results labelled as semantic.

Measured on a real archive of **112,193 chunks** (384-dim vectors from the local
MLX provider, 172 MB in memory): **0.99 ms for a cosine top-20**, and 1.3 s for a
`correlate` including model load. So **no vector database is ever needed** — a
matrix multiply and an `argpartition` are enough well past any plausible corpus.
The only real cost is generating the embeddings once, which is why `embed` is a
separate, resumable command.

```sh
uv sync --extra semantic        # the local Apple-silicon provider, plus numpy
uv run muninn embed             # one-time; resumable, --dry-run to plan
uv run muninn search "that time SSE kept dropping" --semantic
uv run muninn correlate a7efca23
```

## Roadmap

- [x] Storage, ingest, provenance classification, losslessness contract tests
- [x] Distributable corpus survey (`tools/corpus-survey.py`)
- [x] Background indexer: `SessionEnd` hook + watcher + reconciling sweep
- [x] Daemon (`muninn serve`) owning ingest, the menubar raven, and clean shutdown
- [x] Login-agent installer (launchd / systemd / Windows), via the shared `corvidae` package
- [x] Structured filters: `--repo --branch --file --tool --model --provenance --source --since --until`, plus `muninn log`
- [x] `muninn doctor` — index lag, parse health, queue depth, ledger tail, calibration drift
- [x] `muninn survey` — derived thresholds in an inspectable `calibration.json`
- [x] Prose-index backfill (`muninn backfill`) from `claudex` / `codexdex`
- [x] `muninn resume` — reopen a session, or say honestly why it cannot be
- [x] Index-time enrichment (`muninn enrich`): topic, outcome, decisions, artifacts
- [x] Hybrid retrieval: `muninn embed`, `search --semantic/--deep`, `muninn correlate`
- [ ] `muninn brief` — a synthesis across matching sessions, carrying provenance per claim
- [x] Shared-menubar raven: descriptor and `/api/menu`, rendered by [Roost](https://github.com/tohuw/roost)
- [ ] Console
- [ ] Agent skill

### Superseding the predecessors

Muninn supersedes [`claudex`](https://github.com/tohuw/claudex) and
[`codexdex`](https://github.com/tohuw/codexdex), folding their prose-index
approach into one archive with per-source adapters.

Their indexes are archives too, and that matters more than it sounds: they cover
sessions whose raw transcripts were swept months ago. `muninn backfill` ingests
them, recording each session with `origin = 'prose-index'` and never overwriting
a richer raw-derived one. Run against the development machine's real corpus it
moved 3,730 sessions and 26,420,905 words with zero differences, and reported the
8 files it skipped along with the reason for each.

**Keep the predecessors' indexes until you have verified your own archive holds
what they did.** Archiving the repositories is harmless; deleting `~/.claudex`
is not.

## Design notes

`.valholl/` holds the knowledge base — an
[OKF](https://github.com/tohuw/yggdrasil) bundle recording *why* decisions were
made and what was measured, including the methodological errors made along the
way so they are not repeated. Start at
[`.valholl/index.md`](.valholl/index.md).

## Platform support

macOS, Linux and Windows on a best-effort basis. See
[WINDOWS.md](WINDOWS.md) for what is actually verified there, including four
tests that are skipped on the Windows CI runner and why.

## Tests

```sh
uv run python -m unittest discover tests -v
```

Stdlib `unittest`, no dependencies. Filesystem tests run against a tempdir and
never touch a real archive.

## Contributing to a sibling

Muninn's design surfaced nine concrete improvements for Huginn — an exact-match
plugin API version that silently disables plugins, a menubar with no extension
seam, missing lag reporting, and transcript pollution from its own LLM calls
among them. They are written up in
[`.valholl/articles/lessons-for-huginn.md`](.valholl/articles/lessons-for-huginn.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
