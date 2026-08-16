"""Measure the present corpus and derive thresholds from it.

Muninn hard-codes no corpus thresholds. `muninn survey` measures what is
actually here and writes an inspectable ``calibration.json``; everything
downstream reads that file rather than a constant somebody picked.

See .valholl/articles/derived-calibration.md for why, in one number: a proposed
"enrich sessions >= 300 words" gate selected **37% of Claude sessions but 91% of
Codex ones** on the same machine. The same constant meant two completely
different policies depending on which agent the user favoured. Derived gates on
that corpus landed at 4,046 and 2,480 words — a 1.6x spread — while both hit
~85% text coverage, which is the thing actually worth holding fixed.

## Why this surveys the archive, when the wiki says never calibrate from a derived artifact

Because the rule is about *staleness*, not about indirection. An earlier
prototype calibrated from the claudex/codexdex prose indexes and undercounted
conversations by 15-27% — not because a prose index parses differently, but
because it was seven days stale while 149 newer transcripts sat unindexed. A
gate derived from it was systematically too strict.

Muninn's archive is not that. It is ingested *from* the raw transcripts, holds
sessions whose raw files have since been swept, and is provenance-classified and
deduplicated already — a better input than a re-walk of the trees, and a much
faster one. What it can still be is **behind**, which is the same failure by a
different route. So index lag is measured on every survey, recorded in the
artifact, and raised as an anomaly rather than left to bias the numbers
silently. Calibrating from something stale is the mistake; calibrating from
something derived is not.

## What is scoped to a provenance class, and what is not

Every distribution here is per source *and* per provenance class. Pooling them
once made a corpus look 40x larger and its median session 16x shorter (92% of
that machine's Claude "sessions" were programmatic ``claude -p`` calls made by
another tool). See .valholl/articles/provenance-classification.md.

The **enrichment gate** is derived over ``human`` and ``subagent`` sessions
together and never over ``tool-invoked`` ones. That is not a pooling exception:
it is the set enrichment would actually run on, which is the same set `muninn
search` covers by default, and subagent transcripts hold real work. Tool-invoked
prose is a reproducible byproduct of some other tool's call volume and is
prunable, so letting it move the gate would tune Muninn to bug residue.

## What is honestly not derived yet

Chunk width and stride. ``store.DEFAULT_CHUNK_WORDS``/``DEFAULT_CHUNK_STRIDE``
are still constants, and calibration records the values in force rather than
deriving new ones — recorded so the gap is visible and so a later change to them
shows up as drift, instead of being a silent hard-coded threshold that this
module's existence implies is gone. Query-latency regression, the fourth drift
signal named in the wiki article, needs a benchmark harness and is not measured
here either.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import cost as cost_model
from . import ingest, store
from .store import Store

CALIBRATION_SCHEMA = "muninn.calibration/1"

#: The share of a source's *words* the enrichment gate aims to cover. Held
#: fixed across sources on purpose: coverage is the intent, and the word
#: threshold that achieves it is the thing allowed to differ per corpus. That
#: inversion is the whole design — see the 1.6x spread in the module docstring.
COVERAGE_TARGET_PCT = 85.0

PERCENTILES: tuple[int, ...] = (10, 25, 50, 75, 90, 95, 99)

#: Classes the enrichment gate is derived over. See the module docstring.
ENRICHABLE = ("human", "subagent")

#: A source whose share of conversations moved by more than this many
#: percentage points since the survey is a different corpus than the one that
#: was calibrated.
MIX_SHIFT_PCT = 10.0

#: How far the stored gate's behaviour may move before re-surveying is worth
#: recommending. Both are measured against **what the gate did when it was
#: derived**, never against the target.
#:
#: That distinction is easy to get wrong and was, in the first draft. The gate
#: is the *smallest* set reaching the target, so it always overshoots — a lone
#: 5,000-word conversation covers 100% of an 85% target. Comparing achieved
#: coverage against the target therefore reports a correct, freshly written
#: calibration as drifted, and does it worst on exactly the small corpora where
#: a survey is most tentative.
COVERAGE_DRIFT_PCT = 5.0

#: Share-of-conversations drift, which is the more sensitive of the two and
#: catches what coverage cannot. A gate that selected 60% of conversations and
#: now selects 97% still covers ~99% of the words — coverage barely moves — but
#: the enrichment cost it was derived to bound has changed completely.
SELECTION_DRIFT_PCT = 10.0

#: Corpus growth that invalidates a calibration outright.
GROWTH_FACTOR = 2.0


def calibration_path(db: str | Path) -> Path:
    """``calibration.json`` beside the archive it describes.

    Beside the database rather than in a fixed state directory because it
    describes *that* corpus: a second archive (a test one, a colleague's export)
    must not silently read thresholds derived from the first. The archive path is
    also recorded inside the file, so a copied calibration is detectable rather
    than merely wrong.
    """
    return Path(db).expanduser().parent / "calibration.json"


# ── Numbers ───────────────────────────────────────────────────────────────────

def percentile(ordered: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile of an already-sorted sequence.

    Nearest-rank rather than interpolated so the result is always a value the
    corpus actually contains, and so two runs over an unchanged archive agree to
    the bit. An interpolating percentile would make ``calibration.json`` differ
    between platforms with different float formatting, which would turn the
    idempotence guarantee into a platform quirk.
    """
    if not ordered:
        return None
    rank = math.ceil(pct / 100.0 * len(ordered))
    return ordered[min(max(rank - 1, 0), len(ordered) - 1)]


def _number(value: float | None) -> float | int | None:
    if value is None or isinstance(value, int):
        return value
    return int(value) if float(value).is_integer() else round(float(value), 3)


def summarize(values: Iterable[float]) -> dict[str, Any]:
    """Distribution summary: count, sum, min, p10..p99, max, mean.

    An empty input yields the same *keys* with ``None`` values rather than an
    empty dict. A consumer that has to branch on key presence will eventually
    forget to, and the empty case is the normal one on a fresh archive.
    """
    ordered = sorted(values)
    out: dict[str, Any] = {"count": len(ordered), "sum": _number(sum(ordered)) if ordered else 0}
    out["min"] = _number(ordered[0]) if ordered else None
    for pct in PERCENTILES:
        key = "median" if pct == 50 else f"p{pct}"
        out[key] = _number(percentile(ordered, pct)) if ordered else None
    out["max"] = _number(ordered[-1]) if ordered else None
    out["mean"] = _number(sum(ordered) / len(ordered)) if ordered else None
    return out


def derive_enrichment_gate(word_counts: Sequence[int],
                           target_coverage_pct: float = COVERAGE_TARGET_PCT) -> dict[str, Any]:
    """The smallest set of longest sessions whose words cover the target share.

    Expressed as a coverage intent, not as a length. "Enrich the sessions that
    hold 85% of what I have written" survives a corpus that changes shape;
    "enrich sessions over 300 words" does not, and became two different policies
    across two sources on one machine.

    Returns zeros rather than dividing on an empty or wordless corpus — a fresh
    archive is a normal thing to survey, and the answer is "no gate yet".
    """
    descending = sorted(word_counts, reverse=True)
    total = sum(descending)
    base = {
        "target_coverage_pct": target_coverage_pct,
        "threshold_words": 0,
        "sessions": 0,
        "conversations_total": len(descending),
        "coverage_pct": 0.0,
        "share_of_conversations_pct": 0.0,
    }
    if not descending or total == 0:
        return base
    running = 0
    for position, words in enumerate(descending, start=1):
        running += words
        if running / total * 100.0 >= target_coverage_pct:
            return {**base, "threshold_words": words, "sessions": position,
                    "coverage_pct": round(running / total * 100.0, 2),
                    "share_of_conversations_pct": round(position / len(descending) * 100.0, 2)}
    return {**base, "threshold_words": descending[-1], "sessions": len(descending),
            "coverage_pct": 100.0, "share_of_conversations_pct": 100.0}


def gate_effect(word_counts: Sequence[int], threshold: int) -> tuple[float, float]:
    """``(coverage_pct, share_of_conversations_pct)`` of applying ``threshold`` now.

    The drift measurement: what does the stored gate *do* to today's corpus?
    That is a question comparing two word thresholds cannot answer, because the
    threshold is an output, not the intent — an unchanged number can mean a
    completely changed policy.

    Both halves are returned because they fail independently. A gate that
    selected 60% of conversations and now selects 97% has barely moved on
    coverage (99% either way) and has entirely lost the cost bound it was
    derived to give.
    """
    total_words = sum(word_counts)
    if not word_counts or total_words <= 0:
        return 0.0, 0.0
    selected = [w for w in word_counts if w >= threshold]
    return (round(sum(selected) / total_words * 100.0, 2),
            round(len(selected) / len(word_counts) * 100.0, 2))


# ── The survey ────────────────────────────────────────────────────────────────

def survey(st: Store, *, db: str | Path | None = None,
           roots: dict[str, Path] | None = None,
           coverage_target_pct: float = COVERAGE_TARGET_PCT) -> dict[str, Any]:
    """Measure the archive and return a calibration document.

    Pure with respect to the archive: it reads and writes nothing there.
    ``surveyed_at`` is the only field that varies between two runs over an
    unchanged corpus, which is what makes the idempotence guarantee testable
    rather than aspirational.
    """
    rows = st.conn.execute(
        "SELECT source, provenance, session_id, words, user_turns, assistant_turns, "
        "       cwd, source_present "
        "FROM sessions ORDER BY session_id"
    ).fetchall()

    per_class: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        per_class.setdefault((row["source"], row["provenance"]), []).append(dict(row))

    sources: dict[str, Any] = {}
    for source in sorted({row["source"] for row in rows}):
        classes = {}
        for provenance in ("human", "subagent", "tool-invoked"):
            members = per_class.get((source, provenance), [])
            classes[provenance] = {
                "sessions": len(members),
                "words": sum(m["words"] or 0 for m in members),
                "words_distribution": summarize(m["words"] or 0 for m in members),
                "turns_distribution": summarize(
                    (m["user_turns"] or 0) + (m["assistant_turns"] or 0) for m in members),
            }
        enrichable = [m for p in ENRICHABLE for m in per_class.get((source, p), [])]
        words = [m["words"] or 0 for m in enrichable]
        gate = derive_enrichment_gate(words, coverage_target_pct)
        sources[source] = {
            "provenance": classes,
            "conversations": len(enrichable),
            "conversation_words": sum(words),
            "enrichment_gate": gate,
            "estimated_chunks_above_gate": sum(
                estimate_chunks(w) for w in words if w >= gate["threshold_words"]),
            # Recorded, not derived. See the module docstring: naming the values
            # in force is what makes a later change to them show up as drift
            # instead of as an invisible constant.
            "chunking": {"words": store.DEFAULT_CHUNK_WORDS,
                         "stride": store.DEFAULT_CHUNK_STRIDE,
                         "derived": False},
            "irreplaceable_sessions": sum(
                1 for m in enrichable if not m["source_present"]),
        }

    doc: dict[str, Any] = {
        "schema": CALIBRATION_SCHEMA,
        "surveyed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "archive": {
            "path": str(Path(db).expanduser()) if db is not None else str(st.path),
            "sessions": st.count_sessions(),
            "chunks": st.count_chunks(),
        },
        "coverage_target_pct": coverage_target_pct,
        "sources": sources,
    }
    doc["index_lag"] = _index_lag(st, roots)
    # Cost is additive: everything above is unchanged, and this answers the one
    # question the rest of the survey implies but never states — what a full pass
    # over this corpus would cost. Only the model-side stages have a price; the
    # free ones are listed rather than omitted, because "not mentioned" reads as
    # "not measured". See muninn/cost.py for which inputs are measured and which
    # are declared rates you should override for your account.
    doc["cost"] = _cost_section(doc, st)
    doc["anomalies"] = anomalies(doc)
    return doc


def _cost_section(doc: dict[str, Any], st: Store) -> dict[str, Any]:
    """Project every stage's cost from what the survey already measured.

    Above-gate words and call counts come from the same per-source gate the rest
    of this document derives, so the estimate moves with the corpus rather than
    being pinned to a number someone measured once.

    Prices come from ``rates.json`` beside the archive, if one is there. This
    repo ships none, so with no such file the model-side stages report their
    measured token volumes and no price at all — see ``muninn/cost.py`` for why
    a shipped price is worse than an absent one.
    """
    cost_model.load_rates(st.path)
    above_words = 0
    above_sessions = 0
    above_calls = 0
    for source, block in doc["sources"].items():
        threshold = block["enrichment_gate"]["threshold_words"]
        rows = st.conn.execute(
            "SELECT words FROM sessions WHERE source = ? AND provenance != ? "
            "AND words >= ?", (source, "tool-invoked", threshold)).fetchall()
        for row in rows:
            words = row["words"] or 0
            above_words += words
            above_sessions += 1
            above_calls += cost_model.enrich_calls(words)

    return cost_model.project(
        words=sum(b["conversation_words"] for b in doc["sources"].values()),
        chunks=doc["archive"]["chunks"],
        enrich_words=above_words,
        enrich_calls=above_calls,
        enrich_sessions=above_sessions,
        embed_model=_embed_model_or_default(),
        text_model=_text_model_or_default(),
    )


def _text_model_or_default() -> str:
    """The model enrichment would *actually* use here, if it can be determined.

    An estimate that prices the built-in default while the installed
    distribution enriches through something else is worse than no estimate: it is
    wrong in the direction of whichever model happens to be cheaper, silently.
    So the resolved provider is asked, and its answer is used.

    Every failure falls back to the built-in default rather than propagating —
    `survey` reads the archive and must not acquire a way to fail because a
    plugin is misconfigured. That is also why nothing here calls ``available()``:
    resolution is local by contract, and a provider probe is not.
    """
    from . import providers

    try:
        return getattr(providers.resolve_provider(), "model", providers.DEFAULT_MODEL)
    except Exception:      # noqa: BLE001 - a cost estimate must not break survey
        return providers.DEFAULT_MODEL


def _embed_model_or_default() -> str:
    """The embedding model in use, preferring one already present in the archive.

    Reads the *installed* provider rather than the archive's existing vectors,
    because the question this answers is "what would a pass cost now", and a
    stale model in ``chunk_vectors`` is exactly the case where those differ.
    """
    from . import embed

    try:
        return embed.resolve_provider().model
    except Exception:      # noqa: BLE001 - see above
        # The public build's default embedder, which is local and free.
        return "mlx-community/bge-small-en-v1.5-bf16"


def estimate_chunks(words: int, target: int | None = None, stride: int | None = None) -> int:
    """Overlapping windows of ``target`` words advancing by ``stride``."""
    target = store.DEFAULT_CHUNK_WORDS if target is None else target
    stride = store.DEFAULT_CHUNK_STRIDE if stride is None else stride
    if words <= 0:
        return 0
    if words <= target:
        return 1
    return 1 + math.ceil((words - target) / stride)


def _index_lag(st: Store, roots: dict[str, Path] | None) -> dict[str, Any]:
    """Per-source unindexed-file counts, or why they could not be measured.

    Lag is part of the calibration document rather than a separate report
    because it is the one thing that makes the rest of the document wrong. A
    reader holding a gate derived while 149 transcripts sat unindexed needs that
    fact attached to the gate, not filed elsewhere.
    """
    if roots is None:
        from .paths import default_roots

        roots = default_roots()
    try:
        measured = ingest.index_lag(st, roots)
    except OSError:
        # A root that cannot be walked is a fact to record, never a survey that
        # fails: the archive's own numbers are unaffected by it.
        return {"measured": False, "reason": "a transcript root could not be read"}
    return {
        "measured": True,
        "sources": {source: int(info["unindexed_or_grown_files"] or 0)
                    for source, info in sorted(measured.items())},
        "last_sweep_at": st.last_sweep_at(),
    }


def anomalies(doc: dict[str, Any]) -> list[str]:
    """What is strange about this corpus, in plain language.

    A design whose first act is to report what is strange about the data catches
    errors careful reasoning did not: the survey prototype flagged, unprompted,
    that 92% of one corpus was tool-invoked and concentrated in a single
    directory — the precise contamination that had already produced a 40x error
    in a hand analysis nobody had doubted.

    Ordered deterministically (by source, then by rule) so two runs over an
    unchanged archive produce byte-identical output.
    """
    out: list[str] = []
    if not doc["sources"]:
        return ["The archive holds no sessions; nothing could be derived from it."]

    for source, report in sorted(doc["sources"].items()):
        classes = report["provenance"]
        total = sum(c["sessions"] for c in classes.values())
        if total == 0:
            continue
        tool = classes["tool-invoked"]["sessions"]
        if tool and tool / total * 100.0 >= 25.0:
            out.append(f"{source}: {tool / total * 100.0:.0f}% of sessions are tool-invoked "
                       f"({tool:,} of {total:,}). They are excluded from every statistic "
                       f"here; pooling them would distort each one.")
        if classes["human"]["sessions"] == 0:
            out.append(f"{source}: zero sessions classified as human out of {total:,}; the "
                       f"provenance heuristics may be mis-tuned for this corpus.")
        if report["conversations"] and report["conversations"] < 30:
            out.append(f"{source}: only {report['conversations']:,} conversation(s) to derive "
                       f"from; the threshold is provisional.")
        if report["irreplaceable_sessions"]:
            out.append(f"{source}: {report['irreplaceable_sessions']:,} conversation(s) whose "
                       f"original transcript no longer exists on disk. The archive is the "
                       f"only copy.")

    lag = doc.get("index_lag", {})
    if not lag.get("measured", False):
        out.append("Index lag could not be measured, so staleness cannot be ruled out.")
    else:
        behind = {s: n for s, n in lag.get("sources", {}).items() if n}
        if behind:
            named = ", ".join(f"{s} {n:,}" for s, n in sorted(behind.items()))
            # The failure the wiki article records: a gate derived from a stale
            # index was 41% too high, because 149 newer transcripts were missing.
            out.append(f"The archive is behind its sources ({named} file(s) not yet indexed), "
                       f"so these thresholds are derived from an incomplete corpus. Run "
                       f"`muninn index` and survey again.")
        elif lag.get("last_sweep_at") is None:
            out.append("No reconciling sweep has ever completed, so index lag is being "
                       "reported against an archive nothing has verified.")
    return out


# ── The artifact ──────────────────────────────────────────────────────────────

def write_calibration(doc: dict[str, Any], path: Path) -> None:
    """Write ``calibration.json`` atomically, sorted, at 0600.

    Sorted keys and a trailing newline because the file is meant to be read,
    diffed and committed: an artifact whose key order shifts between runs cannot
    show a human what actually changed. 0600 because it names project
    directories and per-source volumes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".calibration-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_calibration(path: Path) -> dict[str, Any] | None:
    """The stored calibration, or ``None`` if absent, unreadable or foreign.

    A document whose ``schema`` is not one this build understands reads as
    absent rather than as an error. "Never surveyed" and "surveyed by a version
    that wrote a shape I cannot interpret" both mean the same thing to a caller:
    there is nothing here to trust.
    """
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != CALIBRATION_SCHEMA:
        return None
    return doc


# ── Drift ─────────────────────────────────────────────────────────────────────

def drift(st: Store, doc: dict[str, Any]) -> list[str]:
    """Reasons the stored calibration no longer describes this archive.

    Empty means "still current", which `doctor` reports as distinct from "never
    surveyed" — those need different actions and a health report that conflates
    them is telling the reader nothing.

    Query-latency regression, the fourth signal
    .valholl/articles/derived-calibration.md names, is not measured: it needs a
    benchmark harness rather than a query against the archive. Said here rather
    than left as a silently missing check.
    """
    reasons: list[str] = []
    was = doc.get("archive", {}).get("sessions", 0) or 0
    now = st.count_sessions()
    if was and now >= was * GROWTH_FACTOR:
        reasons.append(f"the corpus has grown from {was:,} to {now:,} sessions "
                       f"({now / was:.1f}x) since it was surveyed")
    if was and now < was:
        # Not a normal direction. The archive never deletes prose, so a shrink
        # means a different (or replaced) archive is being measured against
        # someone else's thresholds.
        reasons.append(f"the archive holds fewer sessions ({now:,}) than the calibration "
                       f"describes ({was:,}); this may be a different archive")

    rows = st.conn.execute(
        "SELECT source, provenance, words FROM sessions "
        "WHERE provenance IN (?, ?) ORDER BY session_id", ENRICHABLE).fetchall()
    live: dict[str, list[int]] = {}
    for row in rows:
        live.setdefault(row["source"], []).append(row["words"] or 0)

    surveyed = doc.get("sources", {})
    for source in sorted(set(live) | set(surveyed)):
        if source not in surveyed:
            reasons.append(f"{source!r} has appeared since the survey and has no thresholds")
            continue
        words = live.get(source, [])
        if not words:
            continue
        gate = surveyed[source].get("enrichment_gate", {})
        threshold = gate.get("threshold_words") or 0
        if threshold:
            # Compared against what the gate did when it was derived, never
            # against the target — see COVERAGE_DRIFT_PCT for why that
            # distinction is the whole check.
            was_coverage = gate.get("coverage_pct", 0.0) or 0.0
            was_share = gate.get("share_of_conversations_pct", 0.0) or 0.0
            coverage, share = gate_effect(words, threshold)
            if abs(coverage - was_coverage) > COVERAGE_DRIFT_PCT:
                reasons.append(
                    f"{source}: the stored gate (>= {threshold:,} words) now covers "
                    f"{coverage:.0f}% of conversation text, against {was_coverage:.0f}% "
                    f"when it was derived")
            if abs(share - was_share) > SELECTION_DRIFT_PCT:
                reasons.append(
                    f"{source}: the stored gate now selects {share:.0f}% of "
                    f"conversations, against {was_share:.0f}% when it was derived")

    total_now = sum(len(v) for v in live.values())
    total_was = sum(s.get("conversations", 0) for s in surveyed.values())
    for source in sorted(set(live) | set(surveyed)):
        share_now = (len(live.get(source, [])) / total_now * 100.0) if total_now else 0.0
        share_was = ((surveyed.get(source, {}).get("conversations", 0) / total_was * 100.0)
                     if total_was else 0.0)
        if abs(share_now - share_was) > MIX_SHIFT_PCT:
            reasons.append(f"{source}: share of conversations moved from {share_was:.0f}% to "
                           f"{share_now:.0f}% of the corpus")
    return reasons
