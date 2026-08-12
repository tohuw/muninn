# Muninn — working notes for agents

Muninn is a local-only console for **agent history**: search, correlate and resume
past AI-agent sessions across Claude Code, Codex, and vendor data exports.
Companion to [Huginn](https://github.com/tohuw/huginn) — Huginn is Thought (what
agents are doing now), Muninn is Memory (what they did).

## Local companion runtime

When working from a user's local checkout, leave a working Muninn and Roost
installation behind unless the environment is clearly CI, headless, or otherwise
unable to host a desktop process. This is part of the local developer experience,
not an optional UI extra.

1. Set up this checkout with `uv sync`, then run `uv run muninn install-agent`.
   Run `uv run muninn doctor` to verify that the daemon is running and its raven
   descriptor is published.
2. Roost is the separate, upstream shared menu-bar host; it is never vendored or
   installed by Muninn itself. If `~/Projects/roost` is absent, clone
   `https://github.com/tohuw/roost.git` there. Follow Roost's own README to
   install and start it, then run `roost ravens` to verify Muninn is visible.
3. Offer to install or repair the companion raven, Huginn, after the above check.
   **Do not clone, install, configure, or start Huginn without an affirmative
   user request.** Roost reports ravens; it never starts them.

Preserve an existing Roost checkout and its user settings. Never add Roost as a
dependency or submodule, and never put Cisco-specific material in this public
repository.

## Agent skills

Ensure the `muninn` skill is installed for the active agent runtime; its source
is `.agents/skills/muninn`. Install it when absent. For the companion Huginn
skill, offer installation rather than installing it automatically.

## The one thing to internalize

**This archive is a system of record for data that no longer exists anywhere
else.** Claude Code deletes session transcripts after `cleanupPeriodDays`
(default 30). A subtle ingest bug does not corrupt data that can be re-derived —
it destroys the only copy silently.

## Where the reasoning lives

- **`.valholl/`** records why decisions were made. Read the relevant article
  before changing a subsystem; start at `.valholl/index.md`.
- **`docs/specs/`** contains implementation specs. The wiki says *why*, the spec
  says *what*, and tests say *whether*. If a spec contradicts the wiki, report it.

## Contract tests — do not edit

`tests/test_losslessness.py` and `tests/test_ledger.py` encode guarantees about
unrecoverable data. They must pass unmodified. If an invariant seems wrong or
blocking, stop and report it rather than weakening it.

## Hard rules

- Never delete session prose. A missing source file is expected; record
  `source_present = 0` and move on.
- Provenance is structural, never length-based: `human`, `tool-invoked`, or
  `subagent`.
- Never trust a subagent transcript's `sessionId`; use filename stem plus
  `agentId` / `isSidechain`.
- Enumerate skips with a closed-vocabulary reason; never replace them with a
  counter.
- Derive facts and calibration from raw transcripts, never a derived index.
- Store exception class names, never exception messages or transcript data.
- Derive thresholds with `muninn survey`; never hard-code them.

## Conventions

- Python 3.12+, `uv`, and `hatchling`. Runtime dependencies stay minimal;
  ML-shaped dependencies remain optional.
- `corvidae` is the shared raven package. Consume it; never vendor it.
- CalVer is `YYYY.MM.DD` with optional `.MICRO`; `muninn.__version__` must match
  `pyproject.toml`, and tags add `v`.
- Tests use stdlib `unittest` under pytest. Filesystem tests use tempdirs, never
  a real archive.
- Commits use a conventional prefix; releases include the version and an
  explanatory body plus a model co-author trailer.

## Commands

```sh
uv sync
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn index
uv run muninn serve
uv run muninn install-agent
uv run muninn doctor
uv run muninn search "query"
uv run python tools/corpus-survey.py --self-test
```

## Don't

- Do not push without being asked.
- Do not add dependencies without raising it first.
- Do not put Cisco-specific material in this public repository.
- Do not read real user transcript directories in tests; use fixtures and
  tempdirs.

## The agent-facing contract

Muninn is operated largely by agents, so CLI output is an epistemic boundary:
transport only claims the tool can prove. Every import emits a structured receipt
whose source facts are separate from run deltas; never manufacture conclusions
from counters.
