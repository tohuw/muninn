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

**In:** a cost model (`muninn/cost.py`) with measured token ratios and declared,
attributed rates; a `cost` section in the calibration document and in `survey`'s
printed output; per-unit metrics; every stage listed, including free ones.

**Out:** live rate fetching (a network call in a local read-only command);
per-account billing integration; changing any operation's actual cost.

## The two kinds of number

| | Source | Changes when |
|---|---|---|
| **Token ratios** (`TOKEN_RATIOS`) | Measured with real providers over the real corpus, method and date recorded | The corpus's character changes, or a tokenizer does |
| **Rates** (`RATES`) | Declared, each with `source`, `as_of`, `confidence` | A vendor changes a price, or your account negotiates one |

Measured on a 680-session archive of Claude Code and Codex transcripts:

- **embedding: 1.764 tokens/word** — Titan's own `inputTextTokenCount` over 40
  random real chunks.
- **enrichment: 2.020 input tokens/word, 1,048 output tokens/call** — Bedrock
  `usage` over 15 real calls spanning the gate threshold to the largest session.

**Both are far above the familiar ~1.3 tokens/word.** That figure describes
English prose; agent transcripts are dense with code, paths, identifiers and JSON.
Using 1.3 would under-project every number here by roughly a third, so a test
asserts the ratios stay above 1.5 — the likeliest future regression is someone
"correcting" them toward the number they remember.

Short sessions measure *worse* per word (2.52) than long ones (1.75), because each
call pays the ~700-token instruction block. The aggregate is the right figure for
a corpus projection, and `calls` rather than `sessions` is what drives output cost.

## Rates carry their own trustworthiness

Every rate has a `confidence`. A projection that depends on a `low` one is marked
`~` in the printed output and lists the responsible model. This is not decoration:
the Titan embedding rate here is a commonly published figure that **has not been
verified** against an invoice or the AWS pricing page (fetching that page returned
no usable Anthropic or Titan rows). Saying so is the difference between an
estimate and a number that gets quoted.

Two rates are `seat_licensed` rather than zero: local MLX embedding and Codex.
Their marginal cost genuinely is nothing, but "free because there is no meter" and
"zero because nobody priced it" must not render identically.

`rate_for()` normalises platform prefixes and version suffixes, because the same
model wears different ids per platform and **the ids are not internally
consistent** — verified live: `us.anthropic.claude-sonnet-5` resolves while
`us.anthropic.claude-haiku-4-5` does not, since Haiku requires the dated
`us.anthropic.claude-haiku-4-5-20251001-v1:0`. A table keyed on exact ids would
miss whichever form the caller holds, and a missed lookup projects **zero**, which
is the worst available way to be wrong about money.

An unknown model resolves to `None` and is reported as unverified, never as free.

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
