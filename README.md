# muninn

A local-only console for **agent history** — what your AI agents did, across
Claude Code, Codex, and vendor data exports. Find the half-remembered decision,
recover its evidence, and resume its source when that source still exists. As
they land, correlation and context briefs will read equally well to humans and
agents.

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

**Your agents did work you will need again, but their memory is disposable by
default.** Some agent tools delete local transcripts; Claude Code sweeps session
JSONLs older than `cleanupPeriodDays` (default **30 days**) on startup, and
subagent transcripts go with their parent. On the machine this was developed on,
the oldest surviving transcript was 29 days old and everything older was already
gone. Muninn's index is an archive of record: for much of a corpus it is the only
surviving copy.

**You do not remember your own wording.** You remember the *situation*. That is a
recall problem, not a ranking problem, which is why retrieval is hybrid
(lexical + semantic) rather than a better regex.

**You want the moment something was decided**, not every line where a word
appears. That needs enrichment at index time, not smarter matching at query time.

For example: ask “Which conversation decided that rate limiting belongs in the
core rather than the provider?” Muninn can return the matching session, decision
excerpt, repository, date, provenance, and whether the original transcript is
still present.

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

### Agent access

Agents should prefer an installed `muninn` command. A checkout is also a
complete, no-global-install fallback:

```sh
uv run --directory /path/to/muninn muninn search "extension point"
```

Use that form when an agent's shell cannot resolve `muninn`; it preserves the
CLI as the archive boundary without changing the user's `PATH`.

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

### The indexing pipeline

Indexing runs in three layers, cheapest first, because the source deletes itself
and any single layer can miss:

1. **A `SessionEnd` hook** writes a job to a queue directory the moment a session
   ends. This is the primary path: event-driven, no polling, and a session is
   archived seconds after it finishes. The hook is deliberately tiny — it imports
   no parser and no SQLite — because it runs inside a ~1.5 s budget the agent
   gives it.
2. **A watcher** (`watchfiles`) reacts to raw file changes, catching sessions
   whose hook never fired: a crash, a `kill -9`, a misconfigured hook, another
   tool writing transcripts.
3. **A sweep** reconciles every configured root against the archive — on every
   startup, then every 15 minutes. This is the only layer that *closes* the
   guarantee, because events that happened while the daemon was down were missed
   by both of the others.

Two properties make running all three safe rather than wasteful. Imports are
**incremental**: append-only transcripts are tailed from a stored byte offset, so
re-reading a 50 MB session costs nothing. And they are **idempotent**, keyed by
content digest in an append-only import ledger, so the same session arriving by
hook *and* sweep produces one row plus a `duplicate` receipt — never two.

Then each session's prose is split for search. **Chunks are 400-word windows on a
320-word stride**, so consecutive windows overlap by 80 words; a phrase that
straddles a boundary is still findable. Those windows go into a SQLite FTS5 table
(`porter unicode61` tokenizer) as `(session_id, ordinal, body)`. On the real
corpus this averages ~7 chunks per session.

Chunks and vectors are **derived data**: both can be rebuilt from the archived
prose, so re-indexing a session is safe and deleting a stale chunk is not data
loss. The prose itself is not — see the archive-of-record guarantee below.
Re-chunking a session drops vectors whose ordinal no longer exists, rather than
leaving orphans that cosine search would rank and then fail to render.

(Chunking is currently mechanical. Turn-aware chunking — windows that respect
message boundaries — lands with the source adapters and is not built yet.)

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

`muninn survey` also projects **what a full pass would cost**, per stage — see
[spec 016](docs/specs/016-cost-estimation.md). Volumes are measured from your
corpus; rates are declared with a source and a confidence, and any figure
depending on an unverified rate is marked. Measured on a real 680-session
archive: **1.76 tokens per word** for embedding and **2.02** for enrichment —
well above the ~1.3 that describes English prose, because agent transcripts are
dense with code and identifiers. Ingest, lexical search and `correlate` are
listed at zero rather than omitted, because most of the tool costs nothing.

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
Once a provider is installed, **`muninn serve` embeds new sessions in the
background** — newest first, so the session you just finished is searchable before
the backlog from months ago finishes draining ([spec
014](docs/specs/014-automatic-embedding.md)). `muninn embed` remains for a
deliberate foreground backfill, and `--no-embed` declines the automatic one.
`search --semantic` fuses vectors with the lexical
results by reciprocal rank, and `muninn correlate` answers "conversations like
this one" — best on short and medium sessions; see
[spec 006](docs/specs/006-hybrid-retrieval.md) for a measured caveat about very
long ones. Without a provider installed, `--semantic` **exits non-zero and says
so** — it never returns lexical results labelled as semantic.

Measured on a real archive of **112,193 chunks** (384-dim vectors from the local
MLX provider, 172 MB in memory): **0.99 ms for a cosine top-20**, and 1.3 s for a
`correlate` including model load. So **no vector database is ever needed** — a
matrix multiply and an `argpartition` are enough well past any plausible corpus.
The only real cost is generating the embeddings once, which is why the work is
resumable, committed per batch, and reported as a backlog by `muninn doctor` —
an automatic process that spends money should not be silent about how much it
has decided to do.

```sh
uv sync --extra semantic        # the local Apple-silicon provider, plus numpy
uv run muninn serve             # embeds in the background from here on
uv run muninn embed --dry-run   # or do the backlog in the foreground, deliberately
uv run muninn search "that time SSE kept dropping" --semantic
uv run muninn correlate a7efca23
```

### Where the model is used, and where it is not

Worth stating plainly, because "AI history tool" invites the assumption that a
model is involved throughout:

**Indexing calls no model at all.** Parsing, provenance classification,
chunking, the FTS5 index, the import ledger, `muninn search`, `muninn log`,
`muninn resume`, `muninn survey` and `muninn doctor` are pure Python and SQLite.
Unplug every provider and all of that still works. Muninn is useful with no model
configured.

A model enters in exactly four places, all optional:

| Operation | Model | What it sees |
|---|---|---|
| `muninn embed`, and the daemon's background worker | **Embedding** | Every pending chunk's text, one vector each |
| `search --semantic` | **Embedding** | **Your query only** — one call |
| `search --deep` | **Text (LLM)** | The query plus the top candidate snippets, to reorder them |
| `muninn enrich` | **Text (LLM)** | One session's prose, to extract topic / outcome / decisions / artifacts |

The text provider is a **local CLI**, not an SDK: `claude -p` by default (Haiku,
for cost — one call per substantive session), or `codex exec` with
`--provider codex-cli` (`gpt-5.6-luna`, Codex's cheap tier). Both send the prompt
on stdin, never argv, and both pass `policy.check` before the subprocess starts.
A distribution can *declare* which one is default — see [spec
015](docs/specs/015-provider-selection.md); `muninn doctor` prints the declaration
and `--provider` overrides it per command.

Four consequences of that shape:

- **Semantic search does not send your history anywhere at query time.** The
  archive's vectors were computed once, up front; a query embeds *the query*, and
  ranking is a matrix multiply over stored vectors — ~1 ms at 60k. The expensive
  half is generating the vectors, which is why that is a background job.
- **`muninn correlate` calls no model whatsoever.** It resolves a provider only to
  read its model *id* as a lookup key, then compares stored mean vectors. Asking
  "what else is like this session" costs nothing.
- **Every call routes through one redaction boundary and one policy check.**
  `enrich` and `--deep` both reach a provider through a single function that
  redacts first, so there is exactly one path from archived prose to a model.
  Model id and provider are checked against intersecting `ModelPolicy` allowlists
  that **fail closed** — an unparseable policy becomes refuse-everything, not
  allow-everything.
- **Transcript text is treated as data, never instructions.** Archived prose can
  contain web content and other agents' output, so the enrichment prompt frames it
  as data and the response is parsed as a closed vocabulary rather than trusted.
  An unclear outcome becomes a named sentinel instead of free text drawn from
  model output.

And the reliability rule that differs between them: `--deep` **falls back to the
input order** on any provider failure, because a worse ordering is still a
ranking — but `--semantic` with no provider at all **exits non-zero and says so**,
because silently returning lexical results labelled semantic reports the wrong
thing confidently. Two embedding models are never mixed in one search: the model
id is part of the vector's primary key, and mixing spaces returns confident
nonsense rather than an error.

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
- [x] Automatic background embedding, owned by the daemon and gated on a provider
- [ ] `muninn brief` — a synthesis across matching sessions, carrying provenance per claim
- [x] Shared-menubar raven: descriptor and `/api/menu`, rendered by [Roost](https://github.com/tohuw/roost)
- [ ] Console
- [x] Agent skill

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
