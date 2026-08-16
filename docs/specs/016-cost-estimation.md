# Spec 016 — Cost estimation in `survey`

**Status: implemented.** Depends on 011 (`survey` and the derived gate), 005
(enrichment), 006 (embeddings).

**Read first:**
[`derived-calibration`](../../.valholl/articles/derived-calibration.md). This spec
follows its rule and then names the one place it cannot: volumes are measured,
**rates are declared**, and the two are never blended into a single number without
a label.

## Why

`survey` already measures everything a cost estimate needs — sessions, words, the
derived enrichment gate, chunk counts — and stops one step short of the question
those measurements imply: *what would a full pass cost?* Until now that question
was answered by someone doing arithmetic in a terminal, which means it was
answered once, by one person, and never again.

Two things made the missing answer worse than merely inconvenient. Muninn's
expensive operations are **one-time per session** and **easy to launch by
accident** — a corpus pass is hundreds of model calls behind a single command. And
the tool now spans providers whose prices differ by more than an order of
magnitude, so "what does enrichment cost" has no answer that is not
account-specific.

## Scope

**In:** a cost model (`muninn/cost.py`) with measured token ratios; a `cost`
section in the calibration document and in `survey`'s printed output; per-unit
metrics; every stage listed, including free ones.

**Out:** live rate fetching (a network call in a local read-only command);
per-account billing integration; changing any operation's actual cost.

**Amended (v2026.08.16.9): no rates ship.** See "Prices are not ours to
declare" below. The original spec shipped an attributed rate table; that is
reversed, and the attribution requirement moved onto the reader's own file.

## The two kinds of number

| | Source | Changes when |
|---|---|---|
| **Token ratios** (`TOKEN_RATIOS`) | Measured with real providers over the real corpus, method and date recorded | The corpus's character changes, or a tokenizer does |
| **Rates** (`RATES`) | **Absent by default.** Loaded from the reader's `rates.json`, each entry requiring `source` and `as_of` | A vendor changes a price, or the reader's account negotiates one |

Measured on a 680-session archive of Claude Code and Codex transcripts:

- **embedding: 1.764 tokens/word** — the embedding provider's own reported
  input-token count over 40 random real chunks.
- **enrichment: 2.020 input tokens/word, 1,048 output tokens/call** — the text
  provider's reported `usage` over 15 real calls spanning the gate threshold to
  the largest session.

These are properties of how transcripts tokenize, not of anyone's pricing, which
is why they stay in the repo while prices do not.

**Both are far above the familiar ~1.3 tokens/word.** That figure describes
English prose; agent transcripts are dense with code, paths, identifiers and JSON.
Using 1.3 would under-project every number here by roughly a third, so a test
asserts the ratios stay above 1.5 — the likeliest future regression is someone
"correcting" them toward the number they remember.

Short sessions measure *worse* per word (2.52) than long ones (1.75), because each
call pays the ~700-token instruction block. The aggregate is the right figure for
a corpus projection, and `calls` rather than `sessions` is what drives output cost.

## Prices are not ours to declare

**Amended v2026.08.16.9.** The original design shipped a rate table with a
`source` and a `confidence` per entry, on the reasoning that an attributed
number beats an unattributed one. That is true and it was not enough.

A price in a source file is checked once, by one person, against one vendor's
page, on one date — and then renders to two decimal places on somebody else's
machine for as long as the file exists. Nothing watches it. The `as_of` field
records when it was true without doing anything when it stops being true.

It is also a claim about *the reader's* account that this process cannot see.
Subscription, enterprise agreement, reseller and metered API access produce
different real numbers for an identical call, and picking one is guessing about
somebody's contract.

The original table carried a third problem specific to this repo: it priced an
`amazon.titan-embed-text-v2:0` that specs 005, 006 and 008 all place **outside**
this distribution. The repo forbade the provider and shipped its price.

So:

- `RATES` ships **only** models that run on the reader's own machine, where the
  zero is structural — nothing is billed per token because nothing leaves the
  box. That is a statement about where inference happens, not a price.
- Everything else is loaded from `rates.json` beside the archive, each entry
  requiring `source` and `as_of`. A malformed entry is skipped, never defaulted:
  defaulting a price is how a typo becomes a confident number.
- A rate older than `STALE_AFTER_DAYS` (90) is named back to the reader as worth
  re-checking. Refreshing them is an agent's job — it can read a pricing page,
  which this process deliberately cannot.
- `seat_licensed` may only come from the reader's file. Nothing shipped declares
  how somebody pays for a hosted model.

### Unpriced is a value, not a zero

`StageCost.usd` is `float | None`, and `None` means *nobody has priced this*. The
previous shape had no way to say that: an unknown model resolved to a zero-priced
stand-in flagged only by `confidence: low`, so an unpriced stage rendered
`$0.00` and read as "no charge" — the exact failure the module's own docstring
warned about two screens above the code that did it.

A total containing an unpriced stage is itself `None`. Summing the priced stages
would understate the total by precisely the part nobody has checked, which is the
wrong direction to be wrong in.

### Every money figure says what kind of number it is

`PRICING_CAVEAT` travels in the payload and is printed above the table: these are
an understanding of **published list pricing** at the dates shown, not a quote.

### Model ids still normalise

`rate_for()` normalises platform prefixes and version suffixes, because the same
model wears different ids per platform and **the ids are not internally
consistent** — one platform resolves `…claude-sonnet-5` bare while requiring a
dated `…claude-haiku-4-5-20251001-v1:0`. One `rates.json` entry therefore serves
every platform reselling the same model, and a lookup cannot miss merely because
the caller holds a different spelling.

An unknown model resolves to `None` and is reported as unpriced, never as free.

## Free stages are printed

Ingest, lexical search, `correlate`, `log`, `show`, `resume`, `doctor` and
`survey` are listed at `$0.00` with the reason. A table containing only the priced
operations reads as *these are the operations*, and the most useful fact about
Muninn's economics is how few of them cost anything. `correlate` in particular
surprises people: it resolves a provider only to read its model id as a lookup
key.

## It prices the provider that would actually run

`survey` asks the resolved provider for its model rather than assuming the
built-in default, because an estimate that prices Haiku while the installed
distribution enriches through something else is wrong in whichever direction is
cheaper — silently. Any failure falls back to the built-in default: a cost
estimate must not give `survey` a way to fail, and `available()` is never called
(resolution is local by contract; a provider probe is not).

## Acceptance criteria

1. `cost.ENRICH_CHUNK_WORDS`/`_OVERLAP_WORDS` match `enrich`'s (mirrored, not
   imported — `enrich` imports `survey` imports `cost`).
2. `enrich_calls()` agrees with `enrich.plan`'s own call estimate.
3. `rate_for` resolves platform prefixes and dated suffixes; unknown → `None`.
4. An unknown model is reported `low` confidence, not free.
5. Embedding cost accounts for chunk overlap (~25% more tokens than words).
6. A seat-licensed model costs `$0` and says why.
7. Output cost scales with calls, not sessions.
8. Semantic search costs under a cent per thousand queries.
9. `--deep` costs more than `--semantic`.
10. Free stages are listed, all at zero.
11. Only genuinely unverified rates are flagged.
12. Every rate carries a source and a confidence.
13. Token ratios stay above the prose rule of thumb.
14. `survey` keeps every field it previously produced.
15. A broken provider does not break `survey`.
16. The estimate names the resolved provider's model.
17. The cost section is JSON-safe.

## Definition of done

- `uv run pytest` green with and without `[semantic]`; `ruff` clean.
- `muninn survey --dry-run` prints a cost section on a real archive, and the
  embed figure independently reproduces a hand calculation.
- The printed report marks every figure that depends on an unverified rate.

## Guardrails

- **Do not** fetch rates over the network from `survey`. It is a local read-only
  command and must stay one.
- **Do not** drop a rate's `source`/`confidence` to tidy the table. An unlabelled
  estimate is the failure mode this spec exists to prevent.
- **Do not** replace a measured ratio with a remembered one.
- **Do not** let a missed rate lookup return zero.
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
