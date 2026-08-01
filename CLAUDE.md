# Muninn — working notes for agents

Muninn is a local-only console for **agent history**: search, correlate and resume
past AI-agent sessions across Claude Code, Codex, and vendor data exports.
Companion to [Huginn](https://github.com/tohuw/huginn) — Huginn is Thought (what
agents are doing now), Muninn is Memory (what they did).

## The one thing to internalize

**This archive is a system of record for data that no longer exists anywhere
else.** Claude Code deletes session transcripts after `cleanupPeriodDays`
(default 30). Measured on the development machine: the oldest surviving raw
transcript was 29 days old, and everything older had already been swept. It
survived only because a predecessor tool had archived it.

So a subtle ingest bug does not corrupt data you can re-derive — it destroys the
only copy, silently, and nobody notices for months. That is why this repo is
unusually strict about enumerated skips, append-only history, and tests that
encode guarantees rather than behavior.

## Where the reasoning lives

- **`.valholl/`** — the knowledge base (OKF bundle). It records *why* decisions
  were made and what was measured, including mistakes made along the way so they
  are not repeated. **Read the relevant article before changing a subsystem.**
  Start at `.valholl/index.md`.
- **`docs/specs/`** — implementation specs. Build order, acceptance criteria,
  guardrails. See `docs/specs/README.md`.
- The split: **wiki says *why*, spec says *what*, tests say *whether*.** If a
  spec contradicts a wiki article, the wiki wins — and the contradiction is a bug
  worth reporting.

## Contract tests — do not edit

These encode guarantees about unrecoverable data. They must pass **unmodified**:

- `tests/test_losslessness.py` — round-trip fidelity, idempotence, and that a
  reconciling pass over a vanished source never deletes archived prose.
- `tests/test_ledger.py` — import ledger invariants (once spec 001 lands).

If you believe one of these is wrong, **stop and say so**. Do not edit it to make
a change pass. Same rule for invariants generally: wanting to weaken one is a
finding to report, not an obstacle to route around.

## Hard rules

- **Never delete session prose.** A missing source file is the *expected* end
  state, not a signal to clean up. Record `source_present = 0` and move on.
- **Provenance is structural, never length-based.** Sessions are `human`,
  `tool-invoked`, or `subagent`. Every statistic is scoped to a class. Pooling
  them once skewed every measurement by ~40x.
- **Never trust a subagent transcript's `sessionId`** — it is the *parent's*.
  Use the filename stem plus `agentId` / `isSidechain`. Trusting it silently
  dropped 251 transcripts and 725,706 words.
- **Enumerate, don't count.** A skipped item gets an id and a reason from a closed
  vocabulary. A count cannot be audited after the fact; every silent skip in the
  predecessor tools was a data-loss path nobody noticed.
- **Never derive facts from a derived artifact.** Calibrate and parse from raw
  transcripts. Calibrating from a stale prose index undercounted conversations by
  15–27%.
- **No exception messages in stored data.** They can embed transcript text or
  credentials. Store the exception *class name*.
- **Thresholds are derived, never hard-coded.** `muninn survey` measures the
  present corpus; `calibration.json` is an inspectable artifact.

## Conventions

- **Python ≥3.12**, `uv`, `hatchling`. Dependencies stay minimal (`fastapi`,
  `uvicorn`, `watchfiles`). Anything ML-shaped goes in the optional `[semantic]`
  extra — it must not be a default install.
- **CalVer**: `YYYY.MM.DD` with optional `.MICRO`, enforced by
  `tests/test_version.py`. `muninn.__version__` must equal `pyproject.toml`.
  Git tags add a leading `v`.
- **Style**: `from __future__ import annotations`, type hints, ruff with a
  deliberately small ruleset (`E`, `F`, `W`, line length 130). Comments explain
  *why*, not *what*, and cite the wiki article or issue behind a non-obvious
  decision. Match the density in `muninn/store.py`.
- **Tests**: stdlib `unittest`, run under pytest in CI. Filesystem tests use a
  tempdir and never touch a real archive.
- **Commits**: conventional prefix, version in the subject for releases, em-dash
  separator. Bodies wrapped ~72 cols and explanatory — narrate the failure mode,
  not the diff. Co-author trailer naming the model.

## Commands

```sh
uv sync                                        # set up
uv run python -m unittest discover tests -v    # tests
uv run ruff check muninn tests tools           # lint
uv run muninn index                            # ingest transcripts (one shot)
uv run muninn serve                            # the daemon: continuous ingest + menubar raven
uv run muninn index --watch                    # the same ingest loop, foreground, publishing nothing
uv run muninn search "query"                   # search
uv run muninn doctor                           # archive health, index lag, daemon state
uv run python tools/corpus-survey.py --self-test   # privacy self-test
```

## Don't

- **Don't push without being asked.** Commit freely; pushing is the human's call.
- **Don't add dependencies** without raising it first.
- **Don't put Cisco-specific anything in this repo.** It is public. The internal
  distribution is a separate repository, and that boundary has already been
  violated once and had to be scrubbed with a history rewrite.
- **Don't read the user's real `~/Downloads`, `~/.claude`, or `~/.codex` in
  tests.** Use fixtures and tempdirs.

## The agent-facing contract

Muninn is operated largely *by agents*: a human says "add this export" and an
agent runs it and reports back. So the CLI is an epistemic boundary — whatever it
prints is what an agent will assert to a human. Output that requires
interpretation will eventually be interpreted wrong, confidently.

Hence: every import emits a structured receipt with an explicit outcome enum, and
source facts are reported separately from run deltas. An agent should only ever
*transport* claims the tool can prove, never manufacture them by reading
counters. See `.valholl/articles/deterministic-imports.md` — this rule exists
because that failure already happened.
