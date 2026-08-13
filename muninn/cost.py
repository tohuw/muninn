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

**Declared rates** (:data:`RATES`) are external facts. They change without asking,
they differ per account and per platform, and nothing here can derive them — so
every entry carries a ``source`` and a ``confidence``, and a projection built from
a low-confidence rate says so in its output. A cost report that cannot tell you
which of its inputs is a guess is worse than no report, because it will be quoted.

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

from dataclasses import dataclass, field
from typing import Any

#: Token counts per unit of prose, measured on a real 680-session archive of
#: Claude Code and Codex transcripts (2026-08-12). Method: for embedding, sum
#: Titan's own ``inputTextTokenCount`` over 40 randomly sampled real chunks; for
#: enrichment, sum Bedrock ``usage`` over 15 real enrichment calls spanning the
#: word distribution from the gate threshold to the largest session.
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
    #: ``True`` when the marginal cost is expected to be zero because access is
    #: seat- or subscription-based rather than metered. Kept as a flag rather
    #: than a rate of 0.0 so the distinction between "carries no incremental
    #: charge" and "unmeasured" survives into the report. Note it is *not* the
    #: same as costless: seat access draws on a shared token pool.
    seat_licensed: bool = False


#: Declared rates. **Override these for your account** — a negotiated Bedrock
#: rate or an enterprise Codex agreement makes any published figure wrong, and
#: the whole point of the ``source`` field is that a reader can see which number
#: to challenge first.
RATES: dict[str, Rate] = {
    "amazon.titan-embed-text-v2:0": Rate(
        model="amazon.titan-embed-text-v2:0", input=0.02, output=None,
        source="commonly published on-demand figure; NOT verified against the "
               "AWS pricing page or an invoice",
        as_of="2026-08-12", confidence="low"),
    "claude-haiku-4-5": Rate(
        model="claude-haiku-4-5", input=1.00, output=5.00,
        source="Anthropic first-party API rates", as_of="2026-06-24"),
    "claude-sonnet-5": Rate(
        model="claude-sonnet-5", input=3.00, output=15.00,
        source="Anthropic first-party API rates ($2/$10 introductory through "
               "2026-08-31)", as_of="2026-06-24"),
    "claude-opus-5": Rate(
        model="claude-opus-5", input=5.00, output=25.00,
        source="Anthropic first-party API rates", as_of="2026-06-24"),
    "mlx-community/bge-small-en-v1.5-bf16": Rate(
        model="mlx-community/bge-small-en-v1.5-bf16", input=0.0, output=None,
        source="local inference on the machine's own GPU — no per-token charge. "
               "The real costs are one model-weights download and the wall-clock "
               "time of the embed pass",
        as_of="2026-08-12", seat_licensed=True),
    "gpt-5.6-luna": Rate(
        model="gpt-5.6-luna", input=0.0, output=0.0,
        source="Codex CLI access is typically seat- or subscription-based, so "
               "the marginal cost of a call is zero; set a rate here if your "
               "Codex access is token-billed",
        as_of="2026-08-12", confidence="low", seat_licensed=True),
}

#: Cost of one session running through the daemon's ingest path, for
#: completeness. Ingest calls no model.
INGEST_RATE = 0.0

#: Stand-in for a model with no rate entry. Priced at zero but flagged, so an
#: unknown model reads as "unknown" rather than "no charge".
_UNKNOWN = Rate(model="(unknown)", input=0.0, output=0.0,
                source="no rate entry for this model id — add one to RATES",
                as_of="", confidence="low")

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
    usd: float
    unit: str
    per_unit_usd: float
    inputs: dict[str, Any] = field(default_factory=dict)
    confidence: str = "high"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "model": self.model,
            "usd": round(self.usd, 4),
            "unit": self.unit,
            "per_unit_usd": round(self.per_unit_usd, 6),
            "inputs": self.inputs,
            "confidence": self.confidence,
            "note": self.note,
        }


def _tokens_usd(rate: Rate, input_tokens: float, output_tokens: float = 0.0) -> float:
    out_rate = rate.output or 0.0
    return (input_tokens / 1_000_000 * rate.input
            + output_tokens / 1_000_000 * out_rate)


def embed_cost(words: int, *, model: str = "amazon.titan-embed-text-v2:0",
               chunks: int | None = None) -> StageCost:
    """One-time cost to embed ``words`` of prose.

    Chunking **overlaps** (400-word windows on a 320-word stride), so the tokens
    actually embedded exceed the corpus word count by roughly the overlap ratio.
    Ignoring that under-projects by ~25%, which is why ``chunks`` is taken as a
    measured input rather than derived from ``words`` here.
    """
    rate = rate_for(model) or _UNKNOWN
    ratio = TOKEN_RATIOS["embed_tokens_per_word"]
    if chunks is not None and chunks > 0:
        # Words actually presented to the provider, counting overlap.
        from . import store as _store
        effective_words = chunks * _store.DEFAULT_CHUNK_WORDS
        effective_words = min(effective_words, max(words, 1) * 4)   # sanity bound
    else:
        effective_words = words
    tokens = effective_words * ratio
    usd = _tokens_usd(rate, tokens)
    return StageCost(
        stage="embed", model=model, usd=usd, unit="1M words of prose",
        per_unit_usd=(usd / max(words, 1)) * 1_000_000,
        inputs={"words": words, "chunks": chunks,
                "effective_words_with_overlap": int(effective_words),
                "tokens": int(tokens)},
        confidence=rate.confidence,
        note="one-time per chunk; re-embedding is only needed on a model change")


def enrich_cost(words: int, calls: int, *, model: str = "claude-haiku-4-5",
                sessions: int | None = None) -> StageCost:
    """Cost to enrich ``words`` of prose across ``calls`` provider calls.

    ``calls`` exceeds ``sessions`` because a session over 12,000 words is split,
    and each split pays the instruction block again.
    """
    rate = rate_for(model) or _UNKNOWN
    in_tokens = words * TOKEN_RATIOS["enrich_input_tokens_per_word"]
    out_tokens = calls * TOKEN_RATIOS["enrich_output_tokens_per_call"]
    usd = 0.0 if rate.seat_licensed else _tokens_usd(rate, in_tokens, out_tokens)
    denom = sessions if sessions else calls
    return StageCost(
        stage="enrich", model=model, usd=usd, unit="1,000 sessions",
        per_unit_usd=(usd / max(denom, 1)) * 1_000,
        inputs={"words": words, "calls": calls, "sessions": sessions,
                "input_tokens": int(in_tokens), "output_tokens": int(out_tokens)},
        confidence=rate.confidence,
        note=("covered by seat licensing — no incremental charge, though it does "
              "draw on shared capacity" if rate.seat_licensed
              else "one-time per session, until the session changes"))


def search_cost(searches: int, *, semantic_model: str = "amazon.titan-embed-text-v2:0",
                deep: bool = False, rerank_model: str = "claude-haiku-4-5") -> StageCost:
    """Cost of ``searches`` queries.

    Semantic search embeds **the query only** — the corpus vectors were paid for
    once, at embed time — so this is the cheapest thing in the tool by orders of
    magnitude. ``--deep`` adds one text-model call per search and is the only
    query-time path that sends archived prose anywhere.
    """
    embed_rate = rate_for(semantic_model) or _UNKNOWN
    usd = _tokens_usd(embed_rate,
                      searches * TOKEN_RATIOS["query_tokens_per_search"])
    confidence = embed_rate.confidence
    stage = "search --semantic"
    if deep:
        rr = rate_for(rerank_model) or _UNKNOWN
        stage = "search --deep"
        if not rr.seat_licensed:
            usd += _tokens_usd(
                rr,
                searches * TOKEN_RATIOS["rerank_input_tokens_per_search"],
                searches * TOKEN_RATIOS["rerank_output_tokens_per_search"])
        confidence = "low" if "low" in (confidence, rr.confidence) else "high"
    return StageCost(
        stage=stage, model=semantic_model if not deep else rerank_model,
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
            enrich_sessions: int, embed_model: str = "amazon.titan-embed-text-v2:0",
            text_model: str = "claude-haiku-4-5",
            searches_per_month: int = 100,
            deep_share: float = 0.1) -> dict[str, Any]:
    """Every stage, for one archive. Returns a JSON-safe document.

    ``searches_per_month`` and ``deep_share`` are the only guessed inputs and are
    labelled as such in the output — everything else is measured from the corpus.
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
    one_time = sum(s.usd for s in stages if s.stage in ("embed", "enrich"))
    monthly = sum(s.usd for s in stages if s.stage.startswith("search "))
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
    return {
        "rates": {name: {"input_per_1m": r.input, "output_per_1m": r.output,
                         "source": r.source, "as_of": r.as_of,
                         "confidence": r.confidence,
                         "seat_licensed": r.seat_licensed}
                  for name, r in used.items()},
        "token_ratios": TOKEN_RATIOS,
        "stages": [s.to_dict() for s in stages],
        "one_time_usd": round(one_time, 4),
        "recurring_monthly_usd": round(monthly, 4),
        "assumptions": {
            "searches_per_month": searches_per_month,
            "deep_share": deep_share,
        },
        "low_confidence_models": lows,
    }
