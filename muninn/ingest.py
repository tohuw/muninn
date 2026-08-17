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
import enum
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import sources
from .digest import digest_file, digest_tree
from .receipt import Attribution, Delta, ImportReceipt, Outcome, Skip, SkipReason, SourceFacts
from .store import Store

if TYPE_CHECKING:  # annotations only — muninn.plugins is the socket, not a dependency
    from . import plugins

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


def _upsert_parsed(st: Store, parsed: sources.ParsedSession, path: Path, source: str,
                   stat: os.stat_result, offset: int, now: str,
                   chunk_words: int | None, chunk_stride: int | None) -> bool:
    """Write one parsed transcript's session row, chunks, files, and tools.

    Shared by ``ingest_path`` (the directory sweep) and ``ingest_file`` (the
    single-transcript path the hook drain and watcher use) so the two entry
    points can never drift on what "a session was ingested" means — see
    .valholl/articles/session-lifecycle-facts.md, "A hook may only enqueue,
    never index": once the indexer decides to import a job's transcript, it
    must apply exactly the same upsert the sweep would have applied to the
    same file, or the two code paths would produce archives with different
    shapes depending on which layer happened to catch a given session first.

    Returns whether the session already existed (for added-vs-updated tallying).
    """
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
        st.record_parse_failure(source, category, count,
                                session_id=parsed.session_id)

    st.save_ingest_state(str(path), parsed.session_id, stat.st_size,
                         stat.st_mtime, offset, now)
    return existed


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

    # Live transcript trees are append-only, so there is no (item_id ->
    # updated_at) identity to detect a duplicate *export* by — see digest.py.
    # What the digest CAN do is recognize "this exact directory snapshot was
    # already fully imported", which is exactly the actor-A/actor-B scenario
    # this ledger exists to fix. This lookup MUST happen after the lock is
    # acquired, not before: a 4-way concurrent-import stress test showed that
    # checking it earlier lets two racing imports both observe "no prior
    # completed row" (because neither had finished yet), serialize on the
    # lock in turn, and then the SECOND one to actually run reports
    # `imported` with added=0/updated=0/unchanged=30 -- the exact "0 written,
    # 61 cached" ambiguity deterministic-imports.md exists to eliminate, just
    # produced by a race instead of a re-run. Once this import holds the
    # lock, no other import can complete underneath it (every caller
    # releases the lock only after finish_import()), so the read here is
    # guaranteed current for the entire body below.
    prior_completed = st.find_import_by_digest(source_digest)

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
        existed = _upsert_parsed(st, parsed, path, source, stat, offset, now,
                                 chunk_words, chunk_stride)
        result.merge_failures(parsed.failures)

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


def ingest_file(st: Store, path: Path, source: str,
                chunk_words: int | None = None,
                chunk_stride: int | None = None,
                *, actor: str = "hook") -> IngestResult:
    """Ingest exactly one transcript file. The hook-drain and watcher entry point.

    ``ingest_path`` digests a whole directory snapshot because a live
    transcript tree has no stable per-item identity to digest by (see
    digest.py). A single file does not have that problem — ``digest_file``'s
    content hash IS a stable identity — so a job for one already-fully-parsed
    file (the common case: the hook fires once per session, the watcher
    reacts once per write) is reported as ``duplicate of import #N`` rather
    than a second, redundant ``imported`` row, exactly the invariant 3
    guarantee ``ingest_path`` gives the sweep. This is also what makes
    acceptance test 9 (drain_once run twice against the same unchanged job)
    resolve to ``Outcome.DUPLICATE`` instead of two identical ``imported``
    rows that would misreport "nothing new" the way the claudex incident did.

    Every upsert goes through ``_upsert_parsed`` — the exact function
    ``ingest_path`` uses — so a session picked up by the hook path and one
    picked up by the sweep can never diverge in what fields get written.

    This function never reconciles missing sources (that is a whole-tree
    operation, not a single-file one) and never marks anything missing; the
    sweep is what closes that guarantee, per
    .valholl/articles/continuous-ingest-not-periodic.md.
    """
    path = Path(path)
    result = IngestResult()
    now = _now()
    source_kind = _SOURCE_KIND.get(source, source)
    item_id = str(path)

    try:
        stat = path.stat()
    except OSError as exc:
        # The transcript named by the job is gone by the time we got to it —
        # a real race, not a hypothetical: SessionEnd enqueues, and before
        # the drain runs, /clear or a rename could replace the file, or (per
        # session-lifecycle-facts.md) the vendor's own startup sweep could
        # beat us to it. Record the attempt so it is visible to `doctor`
        # rather than silently vanishing, but there is nothing to import.
        source_digest = f"file-missing:{item_id}"
        ledger_id = st.begin_import(
            actor=actor, source_kind=source_kind, source_ref=item_id,
            source_digest=source_digest,
            facts=SourceFacts(kind=source_kind, digest=source_digest, item_count=0, windowed=False),
        )
        skip = Skip(item_id=item_id, reason=SkipReason.READ_ERROR)
        st.record_items(ledger_id, [(item_id, "skipped", SkipReason.READ_ERROR.value)])
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta(skipped=1),
                         error=type(exc).__name__)
        result.merge_failures({"stat_failed": 1})
        result.receipt = ImportReceipt(
            ledger_id=ledger_id, outcome=Outcome.REJECTED,
            source=SourceFacts(kind=source_kind, digest=source_digest, item_count=0, windowed=False),
            delta=Delta(skipped=1), skips=[skip], error=type(exc).__name__,
        )
        st.commit()
        return result

    source_digest = digest_file(path)
    item_count = 1

    ledger_id = st.begin_import(
        actor=actor, source_kind=source_kind, source_ref=item_id,
        source_digest=source_digest,
        facts=SourceFacts(kind=source_kind, digest=source_digest, item_count=item_count, windowed=False),
    )

    holder = st.acquire_import_lock(ledger_id, actor, os.getpid())
    if holder is not None:
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta())
        result.receipt = ImportReceipt(
            ledger_id=ledger_id, outcome=Outcome.REJECTED,
            source=SourceFacts(kind=source_kind, digest=source_digest, item_count=item_count,
                               windowed=False),
            delta=Delta(),
            attribution=Attribution(ledger_id=holder["ledger_id"], actor=holder["actor"],
                                    finished_at=holder["acquired_at"]),
        )
        return result

    # Same actor-A/actor-B identity check ingest_path performs, scoped to one
    # file, and for the same reason moved to AFTER lock acquisition: read
    # while serialized, or two racing hook-drain/watcher calls for the same
    # unchanged transcript can both see "no prior completed import" and both
    # report `imported` — see the matching comment in ingest_path for the
    # concurrent-import stress test that caught this.
    prior_completed = st.find_import_by_digest(source_digest)

    added = updated = unchanged = 0
    skips: list[Skip] = []
    item_receipts: list[tuple[str, str, str | None]] = []
    error_class: str | None = None
    span_earliest: str | None = None
    span_latest: str | None = None

    prior = st.get_ingest_state(str(path))
    if prior and prior["size_bytes"] == stat.st_size and prior["mtime"] == stat.st_mtime:
        result.skipped_unchanged += 1
        unchanged += 1
        item_receipts.append((item_id, "unchanged", None))
    else:
        parser = sources.PARSERS[source]
        try:
            parsed, offset = parser(path, 0)
        except OSError as exc:
            result.merge_failures({"read_failed": 1})
            skips.append(Skip(item_id=item_id, reason=SkipReason.READ_ERROR))
            item_receipts.append((item_id, "skipped", SkipReason.READ_ERROR.value))
            error_class = type(exc).__name__
        except Exception as exc:
            # A parser bug must not raise out of the drain loop and abort
            # every other queued job in the same batch — same discipline as
            # ingest_path, see unstable-jsonl-format.md.
            result.merge_failures({"parser_exception": 1})
            skips.append(Skip(item_id=item_id, reason=SkipReason.UNKNOWN_SCHEMA))
            item_receipts.append((item_id, "skipped", SkipReason.UNKNOWN_SCHEMA.value))
            error_class = type(exc).__name__
        else:
            if not parsed.session_id:
                skips.append(Skip(item_id=item_id, reason=SkipReason.MISSING_ITEM_ID))
                item_receipts.append((item_id, "skipped", SkipReason.MISSING_ITEM_ID.value))
            else:
                existed = _upsert_parsed(st, parsed, path, source, stat, offset, now,
                                         chunk_words, chunk_stride)
                result.merge_failures(parsed.failures)
                span_earliest = parsed.started_at
                span_latest = parsed.ended_at
                if existed:
                    result.updated += 1
                    updated += 1
                    item_receipts.append((item_id, "updated", None))
                else:
                    result.ingested += 1
                    added += 1
                    item_receipts.append((item_id, "added", None))

    assert_conservation(added, updated, unchanged, len(skips), item_count)
    delta = Delta(added=added, updated=updated, unchanged=unchanged, skipped=len(skips))

    duplicate_of: int | None = None
    if added == 0 and updated == 0:
        if skips:
            outcome = Outcome.REJECTED
        elif prior_completed is not None:
            outcome = Outcome.DUPLICATE
            duplicate_of = prior_completed["ledger_id"]
        else:
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
                           span_earliest=span_earliest, span_latest=span_latest, windowed=False),
        delta=delta,
        skips=skips,
        duplicate_of=duplicate_of,
        attribution=attribution,
        error=error_class,
    )
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


# ── Contributed history: absence, decided by core (tohuw/muninn#1) ───────────

class ReconcileOutcome(str, enum.Enum):
    """Why a reconciliation pass did what it did. Closed, like every other enum here.

    A caller branches on this instead of inferring from a count, for the reason
    ``receipt.Outcome`` exists: "0 marked" is produced by a healthy remote that
    lost nothing, by a source that declined to enumerate, and by a windowed
    source that is not allowed to — three facts that need three different
    responses and are indistinguishable as an integer.
    """

    RECONCILED = "reconciled"          # the source enumerated; the diff was applied
    ABSTAINED = "abstained"            # None, no method, or a raise: nothing flagged
    REFUSED_WINDOWED = "refused-windowed"  # absence from a window proves nothing


@dataclass(frozen=True)
class ReconcileResult:
    """What one pass over a contributed source concluded.

    ``marked`` is the session ids themselves, not a tally. "Enumerate, don't
    count" applies with more force here than anywhere: every silent skip in the
    predecessor tools was a data-loss path nobody noticed, and this is the one
    operation in the codebase that changes a row's meaning on the strength of a
    third party's say-so.
    """

    outcome: ReconcileOutcome
    vouched: int = 0
    marked: tuple[str, ...] = ()


def reconcile_history_source(st: Store, source: "plugins.HistorySource",
                             context: "plugins.SourceContext") -> ReconcileResult:
    """Ask a contributed source what it still sees; record the rest as gone.

    The archive-safety half of ``HistorySource``. The plugin says *what it
    sees*; this function decides *what absence means* — and it means
    ``source_present = 0``, never a delete, because the archived prose may be
    the only surviving copy (.valholl/articles/archive-of-record.md). Keeping
    that decision here rather than in the protocol is the entire reason
    ``reconcile()`` returns keys instead of taking a ``Store``: a plugin author
    cannot get the rule wrong if they are never handed the means to.

    Three refusals, each a different kind of "not enough information":

    - **Windowed source** — invariant 6, the same one ``_reconcile_missing``
      enforces for export importers. A source that only ever sees 30 days cannot
      distinguish "deleted upstream" from "older than my window".
    - **``None``, or no ``reconcile`` at all** — the source declined. An older
      plugin written before this method existed lands here, which is why adding
      it needed no ``API_VERSION`` bump.
    - **A raised exception** — treated as ``None``. An unreachable remote must
      cost a pass, never a mass reclassification of everything it contributed.
      The exception is not stored or rendered, per the no-exception-messages
      rule; the outcome enum carries everything a caller may act on.

    Scoped to ``context.namespace_prefix()``, so one source can only ever speak
    for the id space it was given. The prefix is escaped for ``LIKE`` because
    ``source`` is a plugin-supplied string and ``_`` is a single-character
    wildcard there — an unescaped ``plugin:a_c.x:`` would also match
    ``plugin:abc.x:``, which is one plugin flagging another's sessions.
    """
    if getattr(source, "windowed", False):
        return ReconcileResult(ReconcileOutcome.REFUSED_WINDOWED)

    reconcile = getattr(source, "reconcile", None)
    if reconcile is None:
        return ReconcileResult(ReconcileOutcome.ABSTAINED)
    try:
        vouched_ids = reconcile(context)
        if vouched_ids is None:
            return ReconcileResult(ReconcileOutcome.ABSTAINED)
        vouched = {context.namespaced_id(str(key)) for key in vouched_ids}
    except Exception:  # noqa: BLE001 - a third-party client; see docstring
        return ReconcileResult(ReconcileOutcome.ABSTAINED)

    prefix = context.namespace_prefix()
    rows = st.conn.execute(
        "SELECT session_id FROM sessions "
        r"WHERE session_id LIKE ? ESCAPE '\' AND source_present = 1",
        (_like_prefix(prefix),),
    ).fetchall()
    marked = tuple(row["session_id"] for row in rows if row["session_id"] not in vouched)
    for session_id in marked:
        st.mark_source_missing(session_id)
    if marked:
        st.commit()
    return ReconcileResult(ReconcileOutcome.RECONCILED, vouched=len(vouched), marked=marked)


def _like_prefix(prefix: str) -> str:
    r"""``prefix`` as a ``LIKE`` pattern with ``\`` as the escape character."""
    for char in ("\\", "%", "_"):
        prefix = prefix.replace(char, "\\" + char)
    return prefix + "%"


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
