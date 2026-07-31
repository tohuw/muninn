"""Ingest: move transcripts into the archive without ever losing any.

The single most dangerous failure mode this module must avoid: a reconciling
pass that "cleans up" sessions it can no longer see on disk. Because Claude Code
sweeps transcripts after ``cleanupPeriodDays``, a source file's absence is the
normal end state, not a signal to delete. Absence is recorded
(``source_present = 0``); the prose stays.

See .valholl/articles/archive-of-record.md and
.valholl/articles/continuous-ingest-not-periodic.md.

A second, newer failure mode this module must avoid: reporting a re-import as
a store-relative counter an agent has to interpret ("0 written, 61 cached")
instead of a self-evident claim ("duplicate of import #14"). Every call now
opens an append-only ``import_ledger`` row and returns a structured
``ImportReceipt`` alongside the existing ``IngestResult``, so "nothing new in
this run" can never be misread as "nothing new in this source". See
.valholl/articles/deterministic-imports.md and
.valholl/articles/import-ledger-schema.md.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import sources
from .digest import digest_tree
from .receipt import Attribution, Delta, ImportReceipt, Outcome, Skip, SkipReason, SourceFacts
from .store import Store

# Ledger source_kind vocabulary is broader than the "claude"/"codex" identifiers
# used elsewhere in this module (which predate the ledger and are also the
# `sources.PARSERS` keys). Translate here rather than widen either vocabulary.
_SOURCE_KIND = {"claude": "claude-transcripts", "codex": "codex-rollouts"}


class ConservationError(RuntimeError):
    """``added + updated + unchanged + skipped != item_count`` for one import.

    This identity is an assertion, not a hope (see import-ledger-schema.md,
    "Conservation: the arithmetic must close"). claudex's own predecessor had
    exactly this hole: a falsy ``uuid`` was ``continue``d before any counter
    incremented, so the item vanished from the totals entirely — a silent
    data-loss path the printed summary line happened to obscure by summing
    correctly *that time*. A mismatch here must abort loudly, not get logged
    and ignored.
    """


def assert_conservation(added: int, updated: int, unchanged: int, skipped: int,
                        item_count: int) -> None:
    """Raise :class:`ConservationError` unless the four dispositions sum to the
    source's item count exactly. Called once per import, right before the
    ledger row is closed out — a violation here means the ledger row is left
    with ``finished_at IS NULL``, so it surfaces as an incomplete import
    (invariant 9) rather than a completed one with fabricated numbers.
    """
    total = added + updated + unchanged + skipped
    if total != item_count:
        raise ConservationError(
            f"added({added}) + updated({updated}) + unchanged({unchanged}) + "
            f"skipped({skipped}) = {total}, but source item_count = {item_count}"
        )


@dataclass
class IngestResult:
    scanned: int = 0
    ingested: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    parse_failures: int = 0
    failures_by_category: dict[str, int] = field(default_factory=dict)
    marked_missing: int = 0
    # The ledger's structured verdict for this same call. Kept as a separate,
    # optional attribute (rather than folding ledger fields into this
    # dataclass) so this pre-existing shape — and every caller of it, including
    # tests/test_losslessness.py — keeps working completely unchanged.
    receipt: ImportReceipt | None = None

    def merge_failures(self, failures: dict[str, int]) -> None:
        for category, count in failures.items():
            self.failures_by_category[category] = (
                self.failures_by_category.get(category, 0) + count)
            self.parse_failures += count


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ingest_path(st: Store, root: Path, source: str,
                chunk_words: int | None = None,
                chunk_stride: int | None = None,
                *, actor: str = "cli") -> IngestResult:
    """Ingest every transcript for ``source`` found under ``root``.

    Incremental: a file whose size and mtime are unchanged since the last pass
    is skipped outright. A grown file is re-parsed in full rather than tailed,
    because a session's prose, turn counts and metadata are computed over the
    whole transcript; the stored offset is used to detect growth, not to
    reconstruct state. Correctness first — tailing is an optimization that can
    land once it is proven not to drop turns.

    Every call is one ledger row. ``actor`` identifies who is calling —
    ``"cli"``, ``"hook"``, ``"watcher"``, or ``"agent:<name>"`` — and is what
    lets a duplicate import name *who* ran the original, not just *when*.
    """
    root = Path(root)
    result = IngestResult()
    parser = sources.PARSERS[source]
    now = _now()
    source_kind = _SOURCE_KIND.get(source, source)

    # Discover once, up front: the tree digest and the item_count it feeds into
    # SourceFacts must describe the same snapshot the loop below actually
    # walks, not a second, possibly different, directory listing.
    paths = list(sources.discover(root, source))
    source_digest = digest_tree(root, paths)
    item_count = len(paths)

    # Live transcript trees are append-only, so there is no (item_id ->
    # updated_at) identity to detect a duplicate *export* by — see digest.py.
    # What the digest CAN do is recognize "this exact directory snapshot was
    # already fully imported", which is exactly the actor-A/actor-B scenario
    # this ledger exists to fix. Look this up before creating our own row, so
    # we never match against ourselves.
    prior_completed = st.find_import_by_digest(source_digest)

    ledger_id = st.begin_import(
        actor=actor, source_kind=source_kind, source_ref=str(root),
        source_digest=source_digest,
        facts=SourceFacts(kind=source_kind, digest=source_digest,
                          item_count=item_count, windowed=False),
    )

    holder = st.acquire_import_lock(ledger_id, actor, os.getpid())
    if holder is not None:
        # Invariant 7: a second concurrent import must serialize, not race.
        # The loser gets told who holds the lock and since when — never a
        # counter that could be misread as "nothing to do here".
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta())
        result.receipt = ImportReceipt(
            ledger_id=ledger_id, outcome=Outcome.REJECTED,
            source=SourceFacts(kind=source_kind, digest=source_digest,
                               item_count=item_count, windowed=False),
            delta=Delta(),
            attribution=Attribution(ledger_id=holder["ledger_id"], actor=holder["actor"],
                                    finished_at=holder["acquired_at"]),
        )
        return result

    added = updated = unchanged = 0
    skips: list[Skip] = []
    item_receipts: list[tuple[str, str, str | None]] = []
    error_class: str | None = None
    span_earliest: str | None = None
    span_latest: str | None = None
    # A real corpus can carry the same session id under two different
    # discovered files — e.g. a project directory renamed or symlinked, so the
    # encoded-cwd path changes but Claude Code's own sessionId does not; the
    # same shape hits subagent transcripts too. paths is sorted (discover()
    # guarantees it), so "first occurrence wins" is deterministic: the first
    # file for a session id is upserted, every later one is a named
    # duplicate-item-in-source skip rather than a second upsert that would
    # either collide on the import_items primary key or silently overwrite
    # the first file's ledger receipt.
    seen_session_ids: set[str] = set()

    for path in paths:
        result.scanned += 1
        # Before parsing, the only identity available for a ledger entry is
        # the FILE's own path — never the session id, which is not yet known
        # (and for a stat/read failure, may never be). relpath is guaranteed
        # unique within one scan (sources.discover() never yields the same
        # path twice), so using it here for pre-parse failures can never
        # collide on the (ledger_id, item_id) primary key.
        try:
            path_item_id = path.relative_to(root).as_posix()
        except ValueError:
            path_item_id = str(path)

        try:
            stat = path.stat()
        except OSError as exc:
            result.merge_failures({"stat_failed": 1})
            skips.append(Skip(item_id=path_item_id, reason=SkipReason.READ_ERROR))
            item_receipts.append((path_item_id, "skipped", SkipReason.READ_ERROR.value))
            error_class = error_class or type(exc).__name__
            continue

        prior = st.get_ingest_state(str(path))
        if (prior and prior["size_bytes"] == stat.st_size
                and prior["mtime"] == stat.st_mtime):
            result.skipped_unchanged += 1
            unchanged += 1
            item_receipts.append((path_item_id, "unchanged", None))
            continue

        try:
            parsed, offset = parser(path, 0)
        except OSError as exc:
            result.merge_failures({"read_failed": 1})
            skips.append(Skip(item_id=path_item_id, reason=SkipReason.READ_ERROR))
            item_receipts.append((path_item_id, "skipped", SkipReason.READ_ERROR.value))
            error_class = error_class or type(exc).__name__
            continue
        except Exception as exc:
            # A parser bug or an unanticipated format must not abort the sweep
            # (unstable-jsonl-format.md). It is, however, real evidence that
            # something about this file did not match the parser's
            # assumptions, so it is recorded as a schema-shaped skip rather
            # than silently merged into a generic counter.
            result.merge_failures({"parser_exception": 1})
            skips.append(Skip(item_id=path_item_id, reason=SkipReason.UNKNOWN_SCHEMA))
            item_receipts.append((path_item_id, "skipped", SkipReason.UNKNOWN_SCHEMA.value))
            error_class = error_class or type(exc).__name__
            continue

        if not parsed.session_id:
            # Invariant 11: a missing/empty item id is a named skip, never a
            # coerced placeholder. codexdex defaulted this to the literal
            # string "unknown", so every id-less conversation overwrote the
            # previous one on disk — the exact silent-loss shape this ledger
            # exists to make impossible. No session row is created for it.
            skips.append(Skip(item_id=path_item_id, reason=SkipReason.MISSING_ITEM_ID))
            item_receipts.append((path_item_id, "skipped", SkipReason.MISSING_ITEM_ID.value))
            continue

        if parsed.session_id in seen_session_ids:
            # The same session id was already handled by an earlier file in
            # THIS scan — real corpora hit this when a project directory is
            # renamed or symlinked (Claude Code encodes the cwd into the
            # directory name, so the old and new directories both carry a
            # copy of the same transcript), and subagent transcripts under
            # two parents can collide the same way. sources.discover() yields
            # paths in sorted order, so "first path wins" is deterministic; a
            # second upsert here would be redundant work at best and, since
            # upsert_session's losslessness merge treats each call as a fresh
            # write of source_path, would make session.source_path flap
            # between the two paths depending on iteration order. Recording
            # the enumerated skip is what lets `which files, and why?` be a
            # query instead of forensics. Keyed by path (not session id) so
            # this row cannot collide with the winning file's own item_id.
            skips.append(Skip(item_id=path_item_id, reason=SkipReason.DUPLICATE_ITEM_IN_SOURCE))
            item_receipts.append(
                (path_item_id, "skipped", SkipReason.DUPLICATE_ITEM_IN_SOURCE.value))
            # Cache this path's state anyway so a re-run recognizes it as
            # unchanged (via the size/mtime shortcut above) rather than
            # re-parsing and re-detecting the same duplicate every pass. The
            # ingest_state row is keyed by path, so this cannot collide with
            # the winning file's own state either.
            st.save_ingest_state(str(path), parsed.session_id, stat.st_size,
                                 stat.st_mtime, offset, now)
            continue

        seen_session_ids.add(parsed.session_id)
        existed = st.get_session(parsed.session_id) is not None
        st.upsert_session({
            "session_id": parsed.session_id,
            "source": parsed.source,
            "provenance": parsed.provenance,
            "parent_id": parsed.parent_id,
            "cwd": parsed.cwd,
            "branch": parsed.branch,
            "model": parsed.model,
            "title": parsed.title,
            "started_at": parsed.started_at,
            "ended_at": parsed.ended_at,
            "duration_s": parsed.duration_s,
            "user_turns": parsed.user_turns,
            "assistant_turns": parsed.assistant_turns,
            "tool_uses": parsed.tool_uses,
            "tool_results": parsed.tool_results,
            "words": parsed.words,
            "tokens": parsed.tokens,
            "text": parsed.text,
            "source_path": str(path),
            "source_present": 1,
            "origin": "raw",
            "ingested_at": now if not existed else None,
            "updated_at": now,
        })
        st.replace_chunks(
            parsed.session_id, parsed.text,
            chunk_words or 400, chunk_stride or 320)
        st.set_files(parsed.session_id, parsed.files)
        st.set_tools(parsed.session_id, parsed.tools)

        for category, count in parsed.failures.items():
            st.record_parse_failure(source, category, count)
        result.merge_failures(parsed.failures)

        st.save_ingest_state(str(path), parsed.session_id, stat.st_size,
                             stat.st_mtime, offset, now)

        if parsed.started_at:
            span_earliest = (min(span_earliest, parsed.started_at)
                             if span_earliest else parsed.started_at)
        if parsed.ended_at:
            span_latest = (max(span_latest, parsed.ended_at)
                           if span_latest else parsed.ended_at)

        if existed:
            result.updated += 1
            updated += 1
            item_receipts.append((path_item_id, "updated", None))
        else:
            result.ingested += 1
            added += 1
            item_receipts.append((path_item_id, "added", None))

    # Invariant 10: the arithmetic must close. A violation leaves this ledger
    # row unfinished (finished_at stays NULL) rather than finished with
    # fabricated counts — see invariant 9, "a crashed import is visible".
    assert_conservation(added, updated, unchanged, len(skips), item_count)

    delta = Delta(added=added, updated=updated, unchanged=unchanged, skipped=len(skips))

    duplicate_of: int | None = None
    if added == 0 and updated == 0:
        if skips and unchanged == 0 and item_count > 0:
            # Every discovered file failed outright: nothing was imported, and
            # unlike the duplicate case there is no prior successful import to
            # attribute this to. That is a rejection, not an empty success.
            outcome = Outcome.REJECTED
        elif prior_completed is not None:
            # Invariant 3: duplicate detection is by identity. This is the
            # exact actor-A/actor-B scenario the ledger exists to fix — the
            # digest matches a completed row, and nothing in *this* run
            # changed the archive, so the claim is "already imported",
            # never "imported, with zero counts" (which reads as empty).
            outcome = Outcome.DUPLICATE
            duplicate_of = prior_completed["ledger_id"]
        else:
            # Genuinely nothing to change (e.g. an empty source, or a source
            # whose content the store already held before the ledger existed)
            # and no prior ledger row to attribute it to.
            outcome = Outcome.IMPORTED
    else:
        outcome = Outcome.PARTIAL if skips else Outcome.IMPORTED

    st.record_items(ledger_id, item_receipts)
    st.finish_import(ledger_id, outcome=outcome, delta=delta,
                     duplicate_of=duplicate_of, error=error_class)
    st.release_import_lock(ledger_id)

    attribution: Attribution | None = None
    if outcome is Outcome.DUPLICATE and prior_completed is not None:
        attribution = Attribution(
            ledger_id=prior_completed["ledger_id"],
            actor=prior_completed["actor"],
            finished_at=prior_completed["finished_at"],
        )

    result.receipt = ImportReceipt(
        ledger_id=ledger_id,
        outcome=outcome,
        source=SourceFacts(kind=source_kind, digest=source_digest, item_count=item_count,
                           span_earliest=span_earliest, span_latest=span_latest,
                           windowed=False),
        delta=delta,
        skips=skips,
        duplicate_of=duplicate_of,
        attribution=attribution,
        error=error_class,
    )

    result.marked_missing = _reconcile_missing(st, source, now, windowed=False)
    st.commit()
    return result


def _reconcile_missing(st: Store, source: str, now: str, *, windowed: bool = False) -> int:
    """Record vanished sources. **Never deletes.**

    A missing raw file is the expected outcome of the vendor's retention sweep.
    Marking ``source_present = 0`` is what lets `doctor` report how much of the
    archive is now irreplaceable.

    Invariant 6: windowed sources cannot support deletion claims. A claude.ai
    or ChatGPT export may cover only a recent window; a session's absence from
    one proves nothing about whether it still exists upstream. Live transcript
    trees (the only source kind this module handles) are never windowed, but
    the guard is enforced here — not left to caller discipline — because
    export importers (spec 002) will call into ingest machinery next and must
    inherit this refusal automatically rather than by remembering to check.
    """
    if windowed:
        return 0
    rows = st.conn.execute(
        "SELECT session_id, source_path FROM sessions "
        "WHERE source = ? AND source_present = 1 AND source_path IS NOT NULL",
        (source,),
    ).fetchall()
    marked = 0
    for row in rows:
        if not Path(row["source_path"]).exists():
            st.mark_source_missing(row["session_id"])
            marked += 1
    return marked


def index_lag(st: Store, roots: dict[str, Path]) -> dict[str, dict[str, object]]:
    """Compare newest source artifact against newest ingested row, per source.

    Staleness must be *visible*: a sibling tool's cron indexer sat 7 days stale
    with 148 unindexed transcripts and nothing surfaced it.
    """
    out: dict[str, dict[str, object]] = {}
    for source, root in roots.items():
        newest_src = 0.0
        unindexed = 0
        for path in sources.discover(Path(root), source):
            try:
                stat = path.stat()
            except OSError:
                continue
            newest_src = max(newest_src, stat.st_mtime)
            prior = st.get_ingest_state(str(path))
            if not prior or prior["size_bytes"] != stat.st_size:
                unindexed += 1
        row = st.conn.execute(
            "SELECT MAX(updated_at) AS newest FROM sessions WHERE source = ?",
            (source,)).fetchone()
        out[source] = {
            "newest_source_mtime": newest_src or None,
            "newest_ingested_at": row["newest"] if row else None,
            "unindexed_or_grown_files": unindexed,
        }
    return out
