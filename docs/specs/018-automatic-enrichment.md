# 018 — Automatic enrichment

**Status:** implemented.
**Read first:** 014-automatic-embedding.md — this is its sibling and reuses its
shape; 016-cost-estimation.md, which is the prerequisite; 011-survey-calibration.md
for the gate; 005-enrichment.md for the work itself.

## Why

Spec 014 made embedding automatic and left enrichment manual. The resulting
failure is quieter than a missing vector, which is why it survived four specs: a
facet filter over an un-enriched corpus returns **nothing**, and nothing is a
perfectly ordinary answer. `--outcome fixed` cannot distinguish "no session was
fixed" from "no session has facets", so the filters that make the archive queryable
by meaning silently do not work, and no command says so.

Measured on the author's own archive before this shipped: **681 sessions, 2
enriched.** Embedding was at 100%. Nobody had done anything wrong — enrichment
simply required a human to remember, which is the same argument
`continuous-ingest-not-periodic.md` makes one layer up and
`embedding-is-not-a-chore.md` makes for vectors.

Enrichment waited for a reason, and the reason is the guard below. Embedding a
whole corpus is cents, so automating it needed no cost conversation. Enrichment is
the one expensive thing Muninn does, and "automatic" therefore had to mean
*automatic and answerable*: spec 016 had to exist first, so that a daemon starting
an unasked pass is a pass whose cost was already knowable.

## What ships

A `BackgroundEnricher` in `muninn/enricher.py`, owned by `muninn serve`, sibling to
`BackgroundEmbedder` and deliberately the same shape: own thread, own `Store`
connection, every failure non-fatal to the daemon, a stall guard that stops rather
than spends. `muninn enrich` remains as the foreground, `--dry-run`-able path.

Flags on `serve`: `--no-enrich` declines it; `--enrich-metered` allows it to spend.
`index --watch` never enriches, for the same reason it never embeds — a debug
ingest loop must not start making model calls because someone wanted to watch a
sweep.

`doctor` grows an `enrichment` section: facet coverage, the pending count and its
call estimate, the skip tally, and whether an unattended pass is currently
*allowed*. That last line is the point of the section — a refused enricher is
invisible in every other output.

## The metered guard

**A daemon must not silently convert a stage that carries no charge into one that
bills.** This is the whole substance of the spec.

The Cisco distribution's text provider is a chain: Codex Luna first (seat access,
no incremental charge), Bedrock Haiku as a fallback, resolved **per call**, with
`model` reporting the hop that would run. So a laptop that loses its Codex CLI — an
uninstall, an expired login, a `PATH` change — starts answering "Bedrock Haiku"
from the same property. An unattended loop would carry on and begin billing, and
nobody would find out until an invoice.

Three rules follow:

1. **Checked before every pass, not once at startup.** The risk *is* the model
   changing mid-process; a startup check is precisely the wrong shape.
2. **The provider is authoritative, not the model id.** This nearly shipped
   backwards. A model id cannot answer "does this bill": `claude -p` on a Claude
   Code subscription and the identical model on Bedrock are the same id at
   opposite ends of the question. Deciding from the rate table alone made the
   worker refuse on every default public install — which looks like a working
   guard and is the feature not shipping. So `TextProvider` gains an optional
   `metered: bool | None`; `None` means "no opinion", and only then does
   `cost.bills_per_token` decide.
3. **The fallback fails closed.** An unknown model counts as metered. That is the
   opposite of how the rest of `cost` handles an unknown rate, on purpose: an
   estimate a human reads should say "unverified" and carry on, and a loop that
   would spend should stop.

The opt-in is a **command-line flag, not an environment variable**. A switch that
enables unattended billing belongs in the command that starts the daemon, where it
is visible to whoever starts it, not in a shell profile someone else wrote.

When the worker refuses it **names the model it declined**. "Enrichment stopped"
without the model sends the reader to the wrong question.

## Ordering: cheapest-first, and this spec does not touch it

`embedder` asks for newest-first, because a vector's value is highest for the
session just finished. This worker takes `enrich.plan`'s own ordering — **shortest
first** — and does not override it. That ordering already encodes a decision (see
`plan`'s docstring): cost tracks length, the run is resumable, and longest-first
spent fifteen minutes on one 622,232-word session before committing a row, which is
indistinguishable from a hang. Re-deciding it here would be this spec relitigating
another's finding for no new reason.

## Calibration is polled, not required

`plan` returns an empty plan for an un-surveyed archive rather than defaulting a
threshold — spec 011's rule, and not negotiable here. So the worker treats "no
calibration" as it treats an empty backlog: announce once, wait, re-read. Refusing
to *start* would mean a user who runs `muninn survey` an hour later also has to
restart the daemon, to recover from a state that fixes itself.

## Batch size is a cost-safety parameter

Eight sessions per pass, which looks arbitrarily small until you see why:
re-planning between passes is what re-runs the metered guard. A large batch would
postpone that check behind hours of work — exactly the window in which a provider
falls back to a metered hop. `enrich_sessions` already commits per session, so a
small batch costs nothing in durability.

## The stall guard

A session whose response cannot be parsed is recorded as a failure and stays
un-enriched, so it is planned again next pass. Without a guard that is an unbounded
loop against a paid API over the same failing session. After `STALL_LIMIT` passes
that enrich nothing while candidates exist, the worker stops and says so; `doctor`
reports the backlog it left.

## Language: nothing model-backed is called "free"

Applies to `survey`, `doctor`, `cost`, `corpus-survey.py` and the agent skill.
Seat or subscription access carries no *incremental* charge, but it draws on a
**shared pool of tokens**, and a report that calls that "free" invites a reader to
treat a shared budget as unlimited. Two honest statements, which are not the same
statement:

- **"no incremental charge"** — a seat-licensed or local model. `$0.00` is the
  right figure; the *word* is what misleads.
- **"calls no model"** — ingest, lexical search, `log`, `show`, `resume`,
  `correlate`, `doctor`, `survey`. These consume no model capacity at any volume.

`cost.free_stages` is renamed `cost.unmetered_stages`, and the report key
`free_operations` becomes `operations_calling_no_model`, so the vocabulary cannot
drift back through a copied identifier.

## Acceptance criteria

1. The worker starts only when a text provider resolves, reports itself available,
   and would not bill without `--enrich-metered`; each refusal sets a distinct
   `stopped_reason`.
2. The metered check runs **before every pass**, so a provider that changes hop
   mid-run stops the worker rather than spending.
3. A provider's declared `metered` outranks the rate table; `None` falls back to
   `cost.bills_per_token`, which treats an unknown model as metered.
4. `--enrich-metered` permits an otherwise-refused model, and the refusal names the
   model when it happens.
5. `PolicyRefused` stops the worker permanently — it is a statement about the run's
   configuration, not one session's.
6. A transient failure backs off exponentially and retries; an un-calibrated
   archive waits and re-reads rather than dying.
7. `STALL_LIMIT` passes that enrich nothing while candidates exist stops the worker.
8. `index --watch` never enriches; `serve --no-enrich` does not either.
9. Teardown stops the enricher before the embedder and before the lock is released,
   and a busy worker does not block the daemon's teardown past its timeout.
10. `doctor` reports facet coverage, the pending count, and whether an unattended
    pass is allowed — naming the model when it is not.
11. No output in `survey`, `doctor`, `cost` or `corpus-survey.py` describes a
    model-backed operation as "free".

## Out of scope

- **Enriching below the gate.** The gate is spec 011's and this worker only reads
  it.
- **Automatic `survey`.** Calibration remains something a human runs; this worker
  waits for it rather than deriving thresholds itself.
- **A rate for `claude -p` under API-key authentication.** `metered = False` on the
  Claude CLI provider is wrong for that one case, in the user's favour, and
  `survey` prices the model either way. Fixing it properly means asking the CLI how
  it is authenticated, which is I/O that `available()` is forbidden to do.
