# muninn

A local-only console for **agent history** — what your AI agents did, across
Claude Code, Codex, and vendor data exports. Fast hybrid search, correlation of
similar conversations, quick resume, and context briefs that read equally well
to humans and agents.

_Developed with AI assistance. See the git history for which agents contributed._

Huginn is Thought; Muninn is Memory. [Huginn](https://github.com/tohuw/huginn)
answers "what are my agents doing right now." Muninn answers "what did we do,
decide, and learn." They are complementary and share a single menubar surface —
nobody wants two ravens up there.

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
# Ingest local transcripts. Idempotent; safe to run repeatedly.
uv run muninn index

# Search the archive.
uv run muninn search "extension point"
uv run muninn search "auth redirect" --since 2026-06

# Survey your corpus and derive calibration (see below).
uv run muninn survey
```

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
- [ ] Background indexer: `SessionEnd` hook + watcher + reconciling sweep
- [ ] Structured filters: `--repo --branch --file --tool --model --outcome`
- [ ] `muninn doctor` — index lag, parse health, calibration drift
- [ ] Index-time enrichment: topic, outcome, decisions, artifacts
- [ ] Hybrid retrieval with optional embeddings
- [ ] Console and shared menubar
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
