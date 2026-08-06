# muninn

A local-only console for **agent history** — what your AI agents did, across
Claude Code, Codex, and vendor data exports. Fast hybrid search, correlation of
similar conversations, quick resume, and context briefs that read equally well
to humans and agents.

_Developed with AI assistance. See the git history for which agents contributed._

Huginn is Thought; Muninn is Memory. [Huginn](https://github.com/tohuw/huginn)
answers "what are my agents doing right now." Muninn answers "what did we do,
decide, and learn." They are complementary and share a single menubar surface —
[Roost](https://github.com/tohuw/roost) — because nobody wants two ravens up
there.

> **Status: early.** The storage and ingest foundation works and is covered by
> tests. Search is minimal, enrichment and the console are not built yet. See
> [Roadmap](#roadmap).

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
uv run muninn search "auth redirect" --since 2026-06

# Survey your corpus and derive calibration (see below).
uv run muninn survey

# Health, including whether the daemon is running and on what port.
uv run muninn doctor
```

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
`calibration.json`. Everything downstream reads it.

A fixed threshold encodes one person's habits as everyone's defaults. A proposed
"enrich sessions ≥300 words" rule selected 37% of Claude sessions but 91% of
Codex sessions — the same constant meaning two different policies depending on
which agent you favor. Derived gates on the same corpus landed at 4,046 and
2,480 words respectively, both hitting ~85% text coverage.

`muninn doctor` recommends re-surveying when the corpus shape drifts: query
latency regresses, the corpus doubles, the source or provenance mix shifts, or
index lag exceeds its threshold.

### Search

SQLite FTS5 over prose chunks. Measured on a real corpus: **0.8 s to index, 33 MB,
0.1–1.9 ms queries**. Scaling tests to 162k chunks kept phrase queries under 1 ms
while broad `OR` queries degraded linearly to 45 ms — which is why query
expansion is capped rather than unbounded.

Semantic recall is optional and pluggable via an `EmbeddingProvider` protocol.
Brute-force numpy cosine is ~2 ms at 60k chunks × 1024 dims, so **no vector
database is ever needed**; the only real cost is generating embeddings once.

## Roadmap

- [x] Storage, ingest, provenance classification, losslessness contract tests
- [x] Distributable corpus survey (`tools/corpus-survey.py`)
- [x] Background indexer: `SessionEnd` hook + watcher + reconciling sweep
- [x] Daemon (`muninn serve`) owning ingest, the menubar raven, and clean shutdown
- [x] Login-agent installer (launchd / systemd / Windows), via the shared `corvidae` package
- [ ] Structured filters: `--repo --branch --file --tool --model --outcome`
- [ ] `muninn doctor` — index lag, parse health, calibration drift
- [ ] Index-time enrichment: topic, outcome, decisions, artifacts
- [ ] Hybrid retrieval with optional embeddings
- [x] Shared-menubar raven: descriptor and `/api/menu`, rendered by [Roost](https://github.com/tohuw/roost)
- [ ] Console
- [ ] Agent skill

Muninn supersedes [`claudex`](https://github.com/tohuw/claudex) and
[`codexdex`](https://github.com/tohuw/codexdex), folding their prose-index
approach into one archive with per-source adapters.

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
