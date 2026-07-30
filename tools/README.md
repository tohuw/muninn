# Muninn corpus survey

`corpus-survey.py` measures your local AI-agent transcript corpus and writes an
**anonymous, statistics-only** JSON report. We use it to calibrate Muninn — an
agent-history search/archive console — against real corpora instead of guessing
from one developer's machine.

**Your data comes back to us. Your prose does not.**

## How to run

```sh
python3 corpus-survey.py
```

That's it. No install, no dependencies, Python 3.10 or newer, stdlib only. It
takes a few seconds on a typical corpus (~4 seconds for 4,000 sessions / 776 MB
on the reference machine) and writes
`./muninn-corpus-survey-<utc-timestamp>.json` in your current directory.

**Please open the report and skim it before sending it to us.** It is plain JSON,
about 25 KB, and short enough to read end to end. If anything in it looks
sensitive, don't send it — tell us instead, that's a bug we want to fix.

### Options

| Flag | Meaning |
|---|---|
| `--out PATH` | Where to write the report. Default `./muninn-corpus-survey-<utc-timestamp>.json`. |
| `--claude-dir PATH` | Override the Claude Code home directory (the one containing `projects/`). |
| `--codex-dir PATH` | Override the Codex home directory (the one containing `sessions/`). `$CODEX_HOME` is honored automatically. |
| `--quiet` | Suppress progress output on stderr. |
| `--print-only` | Compute and print the summary; write no file. Good for a look before you commit to anything. |
| `--self-test` | Verify the privacy guarantee and the statistics against a synthetic corpus in a temp directory, then exit. |
| `--help` | Usage plus the full privacy statement. |

### Verify the privacy claim yourself

```sh
python3 corpus-survey.py --self-test
```

This builds a small synthetic corpus in a temp directory containing planted
secret strings — fake prompts, a fake API token, fake paths, a fake branch name,
a fake hostname, a Bedrock ARN with an account id — surveys it, then asserts that
**none** of those strings appear anywhere in the serialized report or the printed
summary. It also asserts that no path-shaped token and no salt-length hex blob
appear, and that every statistic (turn counts, word counts, durations, token
totals, provenance rules, parse-failure categories, percentiles, chunk estimates)
is computed correctly. It cleans up its temp directory. If it prints anything
other than `PASS`, do not send us a report.

## Sources it looks at

Each is optional and skipped cleanly if absent.

- **Claude Code** — `~/.claude/projects/**/*.jsonl`, including subagent
  transcripts under `<session-id>/subagents/agent-*.jsonl`.
  `$CLAUDE_CONFIG_DIR` and, on Windows, `%APPDATA%`/`%LOCALAPPDATA%` are checked.
- **Codex** — `~/.codex/sessions/**/rollout-*.jsonl`. `$CODEX_HOME` is honored.
- **Prose indexes** — whether `~/.claudex/index` and `~/.codexdex/index` exist,
  and how many files each holds. Contents are never read.

## What it collects

Only counts, lengths, durations, coarse time buckets, enum-like classifications,
and salted hashes.

- **Provenance classification** per session — `human`, `tool-invoked`, or
  `subagent` — plus which structural rule fired (subagent path, sidechain flag,
  `entrypoint: sdk-cli`, cwd under a state/cache dir, zero user turns, single
  fast turn). Rule-firing counts tell us which signals actually matter.
- **Distributions** per source and provenance class, each reported as count, sum,
  min, p10, p25, median, p75, p90, p95, p99, max, mean: prose words, user turns,
  assistant turns, duration in seconds, `tool_use` count, `tool_result` count,
  thinking/reasoning block count, raw file bytes, prose bytes.
- **Session counts per month** (`"2026-07"`), per source, per class.
- **Model identifiers** and how many sessions used each, e.g.
  `"claude-sonnet-5": 246`. Anything resembling an ARN, account id, custom
  endpoint, or URL becomes the literal `"custom/redacted"`.
- **Token usage totals** where the transcript records them.
- **Project structure** — the number of distinct projects, the distribution of
  sessions per project, and the top five projects as salted hashes with their
  session share, path *depth*, and whether they sit under a state/cache directory.
- **Working-directory structure** — path depth distribution and how many sessions
  ran under a state or cache directory. Structural facts only.
- **Parse failures** by reason *category* (`json_decode_error`, `empty_file`,
  `unreadable_file`, `no_usable_timestamp`, …) — never a message string.
- **Retention evidence** — session age in 10-day buckets and the oldest age
  observed per source. This is the key durability question: is your history being
  swept?
- **Derived calibration** — the enrichment gate at 85% text coverage (threshold
  words, session count, share of conversations) and estimated chunk counts at a
  400-word target with a 320-word stride.
- **Anomalies** — plain-language warnings about the shape of the corpus, e.g.
  "91% of sessions are tool-invoked", using only numbers and hashes.
- **Run metadata** — schema version, script version, UTC timestamp, Python
  version, and platform as exactly one of `darwin` / `linux` / `win32` / `other`.

## What it does NOT collect

Not present in the report, by construction, and asserted by `--self-test`:

- No message text. No prompts, no replies, no thinking or reasoning content.
- No tool inputs and no tool outputs — only how many there were.
- No file contents and no filenames.
- No paths of any kind. Not your home directory, not your cwd, not a repo path.
- No session titles, session ids, or conversation summaries.
- No git branch names.
- No usernames, hostnames, machine names, or OS release strings.
- No repository or organization names.
- No URLs, no email addresses, no API keys or tokens.
- No error message strings — failures are reduced to category counts.
- No per-session rows. Everything is aggregated, so no single conversation can be
  picked out of the report.
- No anonymization salt. It is random per run and discarded when the run ends.

### How paths are handled

Some questions are genuinely about paths — "how many distinct projects does this
developer work across, and how skewed is the distribution?" We answer those
without emitting a path. Each path is hashed as
`sha256(random-per-run-salt + normalized-path)` and only the first 8 hex
characters are emitted, alongside structural facts like depth. Because the salt is
fresh every run and never written down, the hashes cannot be reversed, cannot be
dictionary-attacked, and cannot be correlated across runs or across people.

## Safety properties

- **Read-only.** The script opens transcript files in `"r"` mode and never
  writes, moves, renames, or deletes anything under `~/.claude`, `~/.codex`, or
  any other source. The only file it writes is the report.
- **Bounded.** Transcripts are streamed line by line; only per-session scalars
  are retained. Peak memory on a 776 MB corpus was 68 MB.
- **Crash-resistant.** Permission errors, unreadable files, malformed JSON, and
  truncated final lines are counted as categorized parse failures and the survey
  continues. One bad file cannot abort the run.
- **Deterministic.** The same corpus yields the same statistics. The only
  run-to-run differences are the anonymization salt, the run timestamp, and
  age-in-days. Files are iterated in sorted order; nothing is sampled.
- **Handles an empty corpus.** If no transcripts are found it says so clearly and
  still produces a valid report — "this developer has no local history" is itself
  a useful calibration data point.
