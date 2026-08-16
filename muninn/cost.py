"""What each stage costs, projected from measured token ratios and declared rates.

`muninn survey` already measures the shape of a corpus — sessions, words, the
derived enrichment gate, chunk estimates. This module turns that into money, so
"what would a full pass cost" is answerable **before** spending it and without
anyone doing arithmetic in a terminal.

## Two kinds of number, kept apart on purpose

**Measured ratios** (:data:`TOKEN_RATIOS`) come from running the real providers
over this project's real corpus. They are recorded with their method and date, the
way `survey`'s ``chunking`` block records the values in force: naming them is what
makes a later change show up as a difference rather than as an invisible constant.

**Declared rates** are external facts, and **this repository ships none of them.**

That is a reversal, and the reasoning is worth keeping. Prices change without
asking, they differ per account and per platform, and nothing here can derive
them. A number baked into a source file is therefore wrong on a schedule nobody
is watching: it was checked once, by one person, against one vendor's public
page, and it silently ages into a lie that still renders to two decimal places.

Worse, a shipped price asserts something about *the reader's* billing that this
project cannot know. Subscription access, an enterprise agreement, a reseller, a
platform's own margin — all produce different real numbers for an identical
call, and it is not this tool's business to guess which one applies.

So rates live in a ``rates.json`` beside the archive, written by whoever can
actually look them up, and every figure derived from one is labelled as an
understanding of **published API list pricing** rather than as a bill. With no
rates file, stages that reach a model report their **token volumes and no
price** — see :func:`load_rates`. "I do not know what this costs" is a usable
answer; a confident zero is not.

The one exception is inference that runs on the reader's own machine, which has
no per-token price to look up in the first place. Those are recorded as a
property of where the work happens, not as a price (:data:`LOCAL_RATES`).

## What calls no model at all

Ingest, chunking, FTS5 search, `log`, `resume`, `doctor`, `survey` and
`correlate` call no model, so they consume no model capacity at any volume.
`correlate` surprises people — it resolves a provider only to read its model *id*
as a lookup key, then compares vectors already on disk. It is listed at zero
rather than omitted, because "not mentioned" reads as "not measured".

**Nothing here is described as "free", including a seat-licensed model.** Seat or
subscription access carries no *incremental* charge, but it draws on a shared pool
of tokens, and a report that calls that free invites a reader to treat a shared
budget as unlimited. "No incremental charge" and "calls no model" are the two
honest statements, and they are not the same statement.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any

#: Token counts per unit of prose, measured on a real 680-session archive of
#: Claude Code and Codex transcripts (2026-08-12). Method: for embedding, sum the
#: embedding provider's own reported input-token count over 40 randomly sampled
#: real chunks; for enrichment, sum the text provider's reported ``usage`` over
#: 15 real enrichment calls spanning the word distribution from the smallest
#: enriched session to the largest.
#:
#: These are measurements of how transcripts *tokenize*, which is a property of
#: the tokenizer and the prose — not of anyone's pricing. They stay in the repo
#: for exactly that reason, while prices do not.
#:
#: **These are higher than the usual rule of thumb and that matters.** The common
#: ~1.3 tokens/word figure is for English prose; agent transcripts are dense with
#: code, paths, identifiers and JSON, which tokenize far worse. Using 1.3 here
#: would under-project every number in this module by about a third.
TOKEN_RATIOS = {
    # Embedding sees chunk bodies only.
    "embed_tokens_per_word": 1.764,
    # Enrichment's input is the redacted transcript *plus* a ~700-token
    # instruction block per call, which is why short sessions measure higher per
    # word (2.52) than long ones (1.75): the fixed overhead amortises. 2.02 is
    # the aggregate over the sampled span and is the right figure for a corpus
    # projection.
    "enrich_input_tokens_per_word": 2.020,
    # One facet object. Bounded by ``max_tokens=2048`` and measured well under it.
    "enrich_output_tokens_per_call": 1048,
    # A `--deep` rerank sees the query plus candidate snippets and is capped at
    # 512 output tokens by rerank.py.
    "rerank_input_tokens_per_search": 3500,
    "rerank_output_tokens_per_search": 120,
    # One query embedding. A query is a sentence, not a session.
    "query_tokens_per_search": 20,
}


@dataclass(frozen=True)
class Rate:
    """A price, and how much to trust it.

    ``input``/``output`` are USD per 1,000,000 tokens. ``output`` is ``None`` for
    an embedding model, which produces vectors rather than billable tokens.
    """

    model: str
    input: float
    output: float | None
    source: str
    as_of: str
    #: ``high`` — from vendor documentation or an authoritative local table.
    #: ``low`` — a plausible figure nobody here has verified. A projection that
    #: uses one is labelled, and the label travels with the output.
    confidence: str = "high"
    #: ``True`` when the reader has told us their access is seat- or
    #: subscription-based rather than metered, so a call carries no *incremental*
    #: charge. Kept as a flag rather than a rate of 0.0 so "carries no
    #: incremental charge" stays distinguishable from "unmeasured" in the report.
    #: Not the same as costless: seat access draws on a shared token pool.
    #:
    #: **Only ever set from the reader's own rates file.** Nothing shipped here
    #: declares it for a hosted model, because how somebody pays for a model is
    #: a fact about their account that this project has no way to observe.
    seat_licensed: bool = False
    #: ``True`` for inference that runs on this machine. Not a price and not an
    #: assumption about anyone's billing — a statement about where the work
    #: happens. Local inference has no per-token charge to look up; its real
    #: costs are a one-time weights download and wall-clock time.
    local: bool = False


#: Models that run on this machine, so there is no per-token price to look up.
#:
#: These are the embedding backends this repo ships support for. The zero is
#: structural rather than researched: nothing is billed per token because
#: nothing leaves the box. The real costs — a one-time weights download and the
#: wall-clock time of an embed pass — are not per-token and are stated in the
#: note rather than priced.
LOCAL_RATES: dict[str, Rate] = {
    "BAAI/bge-small-en-v1.5": Rate(
        model="BAAI/bge-small-en-v1.5", input=0.0, output=None,
        source="local ONNX inference on this machine — nothing is billed per "
               "token because nothing leaves the box",
        as_of="", local=True),
    "mlx-community/bge-small-en-v1.5-bf16": Rate(
        model="mlx-community/bge-small-en-v1.5-bf16", input=0.0, output=None,
        source="local MLX inference on this machine's own GPU — nothing is "
               "billed per token because nothing leaves the box",
        as_of="", local=True),
}

#: Rates in force. Seeded with the local models and extended by
#: :func:`load_rates` from the reader's ``rates.json``.
#:
#: **No hosted model's price is shipped here, and adding one is a defect.** See
#: this module's docstring: a price in source is checked once and then ages
#: unwatched, and it asserts something about the reader's billing that cannot be
#: observed from inside this process. A price for a platform that this
#: distribution does not even implement a provider for — specs 005, 006 and 008
#: all place those in a separate internal distribution — is doubly out of place.
RATES: dict[str, Rate] = dict(LOCAL_RATES)

#: Cost of one session running through the daemon's ingest path, for
#: completeness. Ingest calls no model.
INGEST_RATE = 0.0

#: How every money figure this module produces must be described, wherever it
#: is rendered. Not decoration: the number is a projection from *published list
#: pricing*, and the reader's invoice is a different thing that this project
#: cannot see.
PRICING_CAVEAT = (
    "figures are an understanding of published API list pricing at the dates "
    "shown, not a quote — subscription, enterprise and reseller billing all "
    "differ, and rates change without notice")

#: A rate older than this is reported as possibly stale. Not enforced — an old
#: rate is still better than none, as long as nobody mistakes it for a current
#: one.
STALE_AFTER_DAYS = 90

#: The reason string used when a stage reaches a model nobody has priced.
UNPRICED = "no rate on file for this model"


def rates_path(db: str | Path) -> Path:
    """``rates.json`` beside the archive, alongside ``calibration.json``.

    Beside the database for the same reason calibration is: it is one machine's
    answer about one account, and a second archive must not silently inherit it.
    """
    return Path(db).expanduser().parent / "rates.json"


def load_rates(db: str | Path) -> dict[str, Rate]:
    """Merge the reader's ``rates.json`` into :data:`RATES`. Returns what loaded.

    The file is a JSON object of ``{model_id: {input, output, source, as_of,
    confidence?, seat_licensed?}}``, where ``input``/``output`` are USD per
    1,000,000 tokens. ``source`` and ``as_of`` are **required**, because a price
    with no provenance and no date is the thing this module exists to stop
    shipping — an agent asked to refresh these must record where it read them and
    when, so the next reader can tell a checked figure from an inherited one.

    A malformed entry is skipped rather than defaulted. Defaulting a price is how
    a typo becomes a confident number.
    """
    path = rates_path(db)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    loaded: dict[str, Rate] = {}
    for model, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            rate = Rate(
                model=str(model),
                input=float(entry["input"]),
                output=None if entry.get("output") is None else float(entry["output"]),
                source=str(entry["source"]),
                as_of=str(entry["as_of"]),
                confidence=str(entry.get("confidence", "high")),
                seat_licensed=bool(entry.get("seat_licensed", False)),
            )
        except (KeyError, TypeError, ValueError):
            continue
        loaded[rate.model] = rate
    RATES.update(loaded)
    return loaded


def stale_rates(today: str) -> list[Rate]:
    """Loaded rates older than :data:`STALE_AFTER_DAYS`, for the reader to refresh.

    ``today`` is passed in rather than read from the clock so a report is
    reproducible and testable. Local models carry no date and are never stale:
    there is nothing to go and re-check.
    """
    try:
        now = _date.fromisoformat(today)
    except ValueError:
        return []
    old = []
    for rate in RATES.values():
        if rate.local or not rate.as_of:
            continue
        try:
            seen = _date.fromisoformat(rate.as_of)
        except ValueError:
            continue
        if (now - seen).days > STALE_AFTER_DAYS:
            old.append(rate)
    return sorted(old, key=lambda r: r.model)

#: Provider prefixes a platform adds to an otherwise-identical model. Stripped
#: before a rate lookup so one rate entry serves every platform that resells the
#: same model.
_PLATFORM_PREFIXES = ("us.", "eu.", "ap.", "global.", "anthropic.", "amazon.")


def rate_for(model: str) -> Rate | None:
    """The rate for a model id, tolerating platform prefixes and version suffixes.

    Necessary because the same model wears different ids per platform, and the
    ids are not even internally consistent: verified against the live Bedrock
    API, ``us.anthropic.claude-sonnet-5`` resolves while
    ``us.anthropic.claude-haiku-4-5`` does not — Haiku requires the dated
    ``us.anthropic.claude-haiku-4-5-20251001-v1:0``. A rate table keyed on exact
    ids would therefore miss whichever form the caller happens to hold, and
    missing a rate silently projects zero, which is the worst possible way to be
    wrong about cost.

    Returns ``None`` for an unknown model rather than guessing. The caller is
    expected to surface that as "unknown rate", not as "no charge".
    """
    if model in RATES:
        return RATES[model]
    needle = model
    for prefix in _PLATFORM_PREFIXES:
        if needle.startswith(prefix):
            needle = needle[len(prefix):]
            break
    # Longest key first so `claude-haiku-4-5` cannot be shadowed by a shorter
    # future key that happens to be a substring of it.
    for key in sorted(RATES, key=len, reverse=True):
        bare = key
        for prefix in _PLATFORM_PREFIXES:
            if bare.startswith(prefix):
                bare = bare[len(prefix):]
                break
        if needle.startswith(bare) or bare in needle:
            return RATES[key]
    return None


def bills_per_token(model: str) -> bool:
    """True if calling ``model`` costs money per token. **Fails closed.**

    This is the predicate an *unattended* caller needs, and it is deliberately
    not "is there a rate": an unknown model returns ``True``, because a price
    nobody has recorded is a price nobody has ruled out. Every other function in
    this module reports an unknown rate as "unverified" and carries on, which is
    right for an estimate a human reads and wrong for a loop that would spend.

    "Does not bill" means one of two things, both recorded on the rate rather than
    inferred here: ``seat_licensed`` (paid for by a seat or subscription, so a call
    carries no incremental charge — it still draws on a shared token pool) or a
    genuinely zero rate (a local model, which consumes nothing shared).

    Naming the mistake this exists to prevent: a provider chain whose primary hop
    is seat-licensed and whose fallback is metered reports whichever hop *would*
    run, so "it did not bill when the daemon started" is not a claim that holds for
    the life of the process. See ``enricher.BackgroundEnricher``.
    """
    rate = rate_for(model)
    if rate is None:
        return True
    if rate.seat_licensed:
        return False
    return bool(rate.input) or bool(rate.output)

#: Enrichment's chunking, **mirrored from ``enrich.CHUNK_WORDS`` /
#: ``enrich.CHUNK_OVERLAP_WORDS`` rather than imported**, because ``enrich``
#: imports ``survey`` and ``survey`` imports this module — importing them here
#: would close the cycle. A deliberate duplicate with a test that fails when the
#: two drift, which is the same trade this repo already makes for
#: ``daemon._restrict`` vs ``raven._restrict``.
ENRICH_CHUNK_WORDS = 12_000
ENRICH_CHUNK_OVERLAP_WORDS = 400


def enrich_calls(words: int) -> int:
    """Provider calls one session of ``words`` costs.

    A session over the chunk size is split, and **each split pays the ~700-token
    instruction block again** — which is why calls, not sessions, is the unit
    that drives output-token cost.
    """
    if words <= 0:
        return 0
    stride = max(ENRICH_CHUNK_WORDS - ENRICH_CHUNK_OVERLAP_WORDS, 1)
    return max(1, -(-words // stride))


@dataclass
class StageCost:
    """One stage's projected cost, with the arithmetic left visible.

    ``inputs`` carries the volumes the estimate came from so a reader can check
    it without re-deriving anything — a cost number with no visible denominator
    is the kind that gets quoted back years later at the wrong scale.
    """

    stage: str
    model: str | None
    #: ``None`` means *unpriced* — this stage reaches a model and nobody has told
    #: us what that model costs. Distinct from ``0.0``, which is a claim. The
    #: previous shape had no way to say this and defaulted an unknown model to
    #: zero, so an unpriced stage rendered as "$0.00" and read as "no charge".
    usd: float | None
    unit: str
    per_unit_usd: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    confidence: str = "high"
    note: str = ""
    #: Why there is no price, when ``usd`` is None. Carried so a report can say
    #: which of "costs nothing" and "we do not know" it means.
    unpriced_reason: str | None = None

    @property
    def priced(self) -> bool:
        return self.usd is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "model": self.model,
            "usd": None if self.usd is None else round(self.usd, 4),
            "unit": self.unit,
            "per_unit_usd": (None if self.per_unit_usd is None
                             else round(self.per_unit_usd, 6)),
            "inputs": self.inputs,
            "confidence": self.confidence,
            "note": self.note,
            "unpriced_reason": self.unpriced_reason,
        }


def _tokens_usd(rate: Rate, input_tokens: float, output_tokens: float = 0.0) -> float:
    out_rate = rate.output or 0.0
    return (input_tokens / 1_000_000 * rate.input
            + output_tokens / 1_000_000 * out_rate)


def embed_cost(words: int, *, model: str = "BAAI/bge-small-en-v1.5",
               chunks: int | None = None) -> StageCost:
    """One-time cost to embed ``words`` of prose.

    Chunking **overlaps** (400-word windows on a 320-word stride), so the tokens
    actually embedded exceed the corpus word count by roughly the overlap ratio.
    Ignoring that under-projects by ~25%, which is why ``chunks`` is taken as a
    measured input rather than derived from ``words`` here.
    """
    rate = rate_for(model)
    ratio = TOKEN_RATIOS["embed_tokens_per_word"]
    if chunks is not None and chunks > 0:
        # Words actually presented to the provider, counting overlap.
        from . import store as _store
        effective_words = chunks * _store.DEFAULT_CHUNK_WORDS
        effective_words = min(effective_words, max(words, 1) * 4)   # sanity bound
    else:
        effective_words = words
    tokens = effective_words * ratio
    inputs = {"words": words, "chunks": chunks,
              "effective_words_with_overlap": int(effective_words),
              "tokens": int(tokens)}
    if rate is None:
        return StageCost(
            stage="embed", model=model, usd=None, unit="1M words of prose",
            per_unit_usd=None, inputs=inputs, confidence="low",
            unpriced_reason=UNPRICED,
            note="one-time per chunk; token volume is measured, the price is not")
    usd = _tokens_usd(rate, tokens)
    return StageCost(
        stage="embed", model=model, usd=usd, unit="1M words of prose",
        per_unit_usd=(usd / max(words, 1)) * 1_000_000,
        inputs=inputs,
        confidence=rate.confidence,
        note=("runs on this machine; the real costs are a one-time weights "
              "download and wall-clock time" if rate.local
              else "one-time per chunk; re-embedding is only needed on a "
                   "model change"))


def enrich_cost(words: int, calls: int, *, model: str = "claude-haiku-4-5",
                sessions: int | None = None) -> StageCost:
    """Cost to enrich ``words`` of prose across ``calls`` provider calls.

    ``calls`` exceeds ``sessions`` because a session over 12,000 words is split,
    and each split pays the instruction block again.
    """
    rate = rate_for(model)
    in_tokens = words * TOKEN_RATIOS["enrich_input_tokens_per_word"]
    out_tokens = calls * TOKEN_RATIOS["enrich_output_tokens_per_call"]
    denom = sessions if sessions else calls
    inputs = {"words": words, "calls": calls, "sessions": sessions,
              "input_tokens": int(in_tokens), "output_tokens": int(out_tokens)}
    if rate is None:
        return StageCost(
            stage="enrich", model=model, usd=None, unit="1,000 sessions",
            per_unit_usd=None, inputs=inputs, confidence="low",
            unpriced_reason=UNPRICED,
            note="one-time per session; token volume is measured, the price is not")
    usd = 0.0 if rate.seat_licensed else _tokens_usd(rate, in_tokens, out_tokens)
    return StageCost(
        stage="enrich", model=model, usd=usd, unit="1,000 sessions",
        per_unit_usd=(usd / max(denom, 1)) * 1_000,
        inputs=inputs,
        confidence=rate.confidence,
        note=("you have declared this access seat-licensed — no incremental "
              "charge, though it does draw on shared capacity"
              if rate.seat_licensed
              else "one-time per session, until the session changes"))


def search_cost(searches: int, *, semantic_model: str = "BAAI/bge-small-en-v1.5",
                deep: bool = False, rerank_model: str = "claude-haiku-4-5") -> StageCost:
    """Cost of ``searches`` queries.

    Semantic search embeds **the query only** — the corpus vectors were paid for
    once, at embed time — so this is the cheapest thing in the tool by orders of
    magnitude. ``--deep`` adds one text-model call per search and is the only
    query-time path that sends archived prose anywhere.
    """
    stage = "search --deep" if deep else "search --semantic"
    model = rerank_model if deep else semantic_model
    needed = [semantic_model, rerank_model] if deep else [semantic_model]
    if any(rate_for(m) is None for m in needed):
        # One unpriced hop makes the total unpriced. Pricing only the hops we
        # happen to know would report a number smaller than the truth, which is
        # the direction that gets someone in trouble.
        return StageCost(
            stage=stage, model=model, usd=None, unit="1,000 searches",
            per_unit_usd=None, inputs={"searches": searches}, confidence="low",
            unpriced_reason=UNPRICED,
            note="recurring per query; corpus vectors are not re-paid for")
    embed_rate = rate_for(semantic_model)
    usd = _tokens_usd(embed_rate,
                      searches * TOKEN_RATIOS["query_tokens_per_search"])
    confidence = embed_rate.confidence
    if deep:
        rr = rate_for(rerank_model)
        if not rr.seat_licensed:
            usd += _tokens_usd(
                rr,
                searches * TOKEN_RATIOS["rerank_input_tokens_per_search"],
                searches * TOKEN_RATIOS["rerank_output_tokens_per_search"])
        confidence = "low" if "low" in (confidence, rr.confidence) else "high"
    return StageCost(
        stage=stage, model=model,
        usd=usd, unit="1,000 searches",
        per_unit_usd=(usd / max(searches, 1)) * 1_000,
        inputs={"searches": searches},
        confidence=confidence,
        note="recurring per query; corpus vectors are not re-paid for")


def unmetered_stages() -> list[StageCost]:
    """The stages that call no model, listed so their absence is not inferred.

    Described as "no model call" rather than "free", and the distinction is not
    pedantry: seat-licensed model access draws on a **shared pool of tokens**, so
    calling anything model-backed "free" invites someone to treat a shared budget
    as unlimited. These stages are the ones where that question does not arise —
    they consume no model capacity at all, at any volume.
    """
    return [
        StageCost(stage=name, model=None, usd=0.0, unit="unlimited",
                  per_unit_usd=0.0, note=note)
        for name, note in (
            ("ingest", "hook, watcher, sweep, ledger — pure Python and SQLite"),
            ("search (lexical)", "SQLite FTS5; 0.1–1.9 ms measured"),
            ("correlate", "compares stored vectors; resolves a provider only to "
                          "read its model id as a lookup key"),
            ("log / show / resume / doctor / survey", "reads the archive"),
        )
    ]


def project(*, words: int, chunks: int, enrich_words: int, enrich_calls: int,
            enrich_sessions: int, embed_model: str = "BAAI/bge-small-en-v1.5",
            text_model: str = "claude-haiku-4-5",
            searches_per_month: int = 100,
            deep_share: float = 0.1, today: str = "") -> dict[str, Any]:
    """Every stage, for one archive. Returns a JSON-safe document.

    ``searches_per_month`` and ``deep_share`` are the only guessed inputs and are
    labelled as such in the output — everything else is measured from the corpus.

    A total is ``None`` when any stage feeding it is unpriced. Summing the priced
    stages and presenting that as the total would understate it by exactly the
    part nobody has checked, which is the wrong direction to be wrong in.
    """
    stages = [
        embed_cost(words, model=embed_model, chunks=chunks),
        enrich_cost(enrich_words, enrich_calls, model=text_model,
                    sessions=enrich_sessions),
        search_cost(searches_per_month, semantic_model=embed_model),
        search_cost(int(searches_per_month * deep_share), deep=True,
                    semantic_model=embed_model, rerank_model=text_model),
        *unmetered_stages(),
    ]
    def _total(selected: list[StageCost]) -> float | None:
        return (None if any(not s.priced for s in selected)
                else sum(s.usd for s in selected))

    one_time = _total([s for s in stages if s.stage in ("embed", "enrich")])
    monthly = _total([s for s in stages if s.stage.startswith("search ")])
    # Report the *rates* that are unverified, not the stages that depend on one.
    # The first version listed every model in a low-confidence stage, so a
    # high-confidence Claude rate was flagged unverified merely because the same
    # stage also priced an embedding — which is how a caveat stops meaning
    # anything.
    used = {}
    for name in (embed_model, text_model):
        found = rate_for(name)
        if found is not None:
            used[found.model] = found
    lows = sorted(r.model for r in used.values() if r.confidence == "low")
    unpriced = sorted({s.model for s in stages if s.model and not s.priced})
    return {
        "rates": {name: {"input_per_1m": r.input, "output_per_1m": r.output,
                         "source": r.source, "as_of": r.as_of,
                         "confidence": r.confidence,
                         "seat_licensed": r.seat_licensed,
                         "local": r.local}
                  for name, r in used.items()},
        "token_ratios": TOKEN_RATIOS,
        "stages": [s.to_dict() for s in stages],
        "one_time_usd": None if one_time is None else round(one_time, 4),
        "recurring_monthly_usd": None if monthly is None else round(monthly, 4),
        "assumptions": {
            "searches_per_month": searches_per_month,
            "deep_share": deep_share,
        },
        "low_confidence_models": lows,
        #: Models a stage reached that nobody has priced. Non-empty means the
        #: totals above are None and the reader needs a rates.json.
        "unpriced_models": unpriced,
        "stale_rates": [r.model for r in stale_rates(today)] if today else [],
        "caveat": PRICING_CAVEAT,
    }
