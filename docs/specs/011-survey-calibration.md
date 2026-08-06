# Spec 011 — `muninn survey` and `calibration.json`

**Status: implemented.** Depends on 001 (ledger), 003 (indexer, for lag).
Unblocks 005 (enrichment), whose gate reads this file.

Wiki articles to read first: [`derived-calibration`](../../.valholl/articles/derived-calibration.md),
[`provenance-classification`](../../.valholl/articles/provenance-classification.md),
[`corpus-measurements`](../../.valholl/articles/corpus-measurements.md),
[`continuous-ingest-not-periodic`](../../.valholl/articles/continuous-ingest-not-periodic.md).

## The problem

The README described `muninn survey` and `calibration.json` as the mechanism
behind Muninn's "derived, never hard-coded" thresholds. Neither existed:
`muninn survey` was an `invalid choice` error, and `grep -rn calibration muninn/`
found two comments and no code. Tracked as
[#2](https://github.com/tohuw/muninn/issues/2).

That is worse than a missing subcommand. The derived-threshold idea is
load-bearing, not decorative: a proposed "enrich sessions ≥ 300 words" gate
selected **37% of Claude sessions but 91% of Codex ones** on the same corpus —
one constant meaning two completely different policies depending on which agent
someone favours. Spec 005 cannot be implemented as written, because its gate
reads a file nothing wrote.

## What it does

`muninn survey` measures the archive and writes `calibration.json` beside it.

```
$ muninn survey
archive  ~/.local/state/muninn/muninn.db
         3,115 sessions · 41,203 chunks

[claude] 115 conversations · 588,554 words
  human            115 sessions    588,554 words
  subagent           0 sessions          0 words
  tool-invoked   3,000 sessions     84,112 words  (excluded from every statistic)
  enrich gate  >= 3,166 words -> 36 sessions (31.3% of conversations, 85.3% of text)

anomalies
  [!] claude: 96% of sessions are tool-invoked (3,000 of 3,115). They are excluded
      from every statistic here; pooling them would distort each one.

wrote  ~/.local/state/muninn/calibration.json
```

Flags: `--dry-run` (derive and print, write nothing), `--json` (the document and
nothing else on stdout, for agents), `--out`, `--source`.

## Decisions worth knowing before reading the code

### It surveys the archive, and the wiki says never calibrate from a derived artifact

Both are true, because that rule is about **staleness, not indirection**. The
prototype that calibrated from the claudex/codexdex prose indexes undercounted
conversations by 15–27% — not because a prose index parses differently, but
because it was seven days stale while 149 newer transcripts sat unindexed. The
gate it derived was 41% too high.

Muninn's archive is ingested *from* the raw transcripts, holds sessions whose raw
files have since been swept, and is already provenance-classified and
deduplicated. It is a better input than a re-walk, and a far faster one. What it
can still be is **behind** — the same failure by another route. So index lag is
measured on every survey, recorded *inside* the artifact, and raised as an
anomaly. Staleness travels attached to the number it invalidates rather than
filed somewhere else.

### Coverage is the intent; the word threshold is the output

The gate is "the smallest set of longest conversations whose words cover 85% of
the source's conversation text". The 85% is held fixed across sources; the word
count that achieves it is free to differ, and did — 4,046 vs 2,480 words on the
measured corpus, a 1.6x spread landing on near-identical coverage. Inverting
that (fixing the words, letting coverage float) is exactly the failure this spec
exists to remove.

Print order follows from this: coverage first, threshold second. A reader shown
"4,046 words" first will read it as the number that matters and compare it
against someone else's.

### Tool-invoked sessions contribute to no statistic

Pooling them once made a corpus look 40x larger and its median session 16x
shorter. They are counted **in their own class** and excluded from every
distribution and from the gate. Counting them is not an exception to the rule —
their volume is the evidence the anomaly rule depends on, and hiding it would
remove the signal that a corpus is contaminated.

The gate is derived over `human` **and** `subagent` together. That is not
pooling either: it is the set enrichment would actually run on, which is the set
`muninn search` covers by default, and subagent transcripts hold real work —
mistrusting them once cost 251 transcripts and 725,706 words.

### Drift is measured against what the gate *did*, never against its target

The first draft got this wrong and the mistake is instructive. The gate is the
*smallest* set reaching the target, so it always overshoots: a lone 5,000-word
conversation covers 100% of an 85% target. Comparing achieved coverage against
the target therefore reports a correct, freshly written calibration as drifted —
worst on exactly the small corpora where a survey is most tentative.

So `doctor` compares today's behaviour against the behaviour recorded at
derivation time, on **two** axes that fail independently:

| axis | catches |
|---|---|
| coverage % | the gate no longer covers the text it was derived to cover |
| share of conversations % | the gate still covers ~99% of words but now selects 97% of conversations instead of 60% — the enrichment cost bound is gone |

Plus: corpus grown ≥2x, a source appearing with no thresholds, a source's share
of conversations moving >10 points, and an archive that has *shrunk* (which the
never-delete rule makes impossible, so it means a different archive is being
measured against these thresholds).

**Query-latency regression**, the fourth drift signal the wiki article names, is
not measured. It needs a benchmark harness rather than a query, and saying so
here is better than a silently missing check.

### `doctor` distinguishes three states, not two

Never surveyed / surveyed and current / surveyed and drifted. A section that only
speaks up when something is wrong leaves a reader unable to tell "fine" from "not
checked", so "current" is stated as a positive answer. Drift is reported *with
its reasons* — "re-run survey" alone is an instruction, not a finding.

### The artifact lives beside the archive

Not in a fixed state directory: it describes *that* corpus, and a second archive
(a test one, a colleague's export) must not silently read thresholds derived from
the first. The archive path is recorded inside the file, so a copied calibration
is detectable rather than merely wrong. Written atomically, sorted, 0600, with a
trailing newline — it is meant to be read, diffed and committed, and an artifact
whose key order shifts between runs cannot show a human what changed.

### What is honestly still hard-coded

**Chunk width and stride.** `store.DEFAULT_CHUNK_WORDS`/`DEFAULT_CHUNK_STRIDE`
remain constants; calibration records the values *in force* and marks them
`"derived": false`. Recording rather than deriving is deliberate — wiring the
store to re-chunk from a file would silently rebuild an existing index — but it
is a hard-coded threshold in a codebase whose rule is that there are none, so it
is named here rather than left for someone to discover.

## Acceptance criteria

1. `muninn survey` on an empty archive exits 0 and says so, dividing by nothing.
2. Two differently-shaped sources derive different thresholds.
3. Both land on the coverage target; the gate is the *smallest* set that does.
4. Tool-invoked sessions move neither the gate nor any distribution.
5. Tool-invoked sessions are still counted in their own class.
6. A second run over an unchanged archive is byte-identical apart from
   `surveyed_at` — including anomaly ordering.
7. `calibration.json` is 0600, atomic, sorted; a foreign `schema` reads as never
   surveyed.
8. `--dry-run` writes nothing; `--json` puts the document and nothing else on
   stdout.
9. `doctor` reports never-surveyed, current, and drifted as three distinct states.
10. A freshly written calibration never reads as drifted.
11. Index lag appears as an anomaly rather than silently biasing the gate.
12. Anomalies surface a tool-invoked majority, a source with no human sessions, a
    thin corpus, and sessions whose only copy is the archive.

## Verification

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn survey --dry-run          # against the real archive
uv run muninn doctor                    # calibration section, three states
```

**A caveat on the real-corpus rule.** The development machine's archive was empty
at implementation time, so the real-corpus run exercised only the empty-archive
path. The derivation was instead validated against a synthetic two-source corpus
shaped like the measured one, which reproduced the article's finding — a **1.64x
spread between derived gates, both landing at 85.3% coverage**, while a fixed
300-word constant selected 89% and 96% of the two sources. That is a stronger
check than a fixture and a weaker one than real data, and this spec is not done
being verified until `muninn survey` has run against a populated archive.

## Guardrails

- **Do not pool provenance classes.** Every statistic is scoped, and the 40x
  error is what happens when one is not.
- **Do not derive from a stale archive without saying so.** Index lag belongs in
  the artifact, not in a separate report.
- **Do not compare drift against the target.** Compare against what the gate did
  when derived. See above.
- **Do not make `tools/corpus-survey.py` importable.** It is a standalone,
  stdlib-only, distributable script for collecting anonymised statistics from
  *other people's* corpora, and it must never grow a dependency on this package.
  This was a port, not a move.
- **Do not have the store read `calibration.json` yet.** Re-chunking an existing
  archive from a file is spec 005/006 territory and would silently rebuild an
  index.
