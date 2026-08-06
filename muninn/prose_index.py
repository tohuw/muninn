"""Backfill the predecessors' prose indexes, which hold the only copy of a lot.

`claudex` and `codexdex` indexed the same transcripts Muninn does, into flat
prose files. Muninn was designed to supersede both — but **their indexes are
archives too**. `~/.claudex/index` holds thousands of files covering sessions
whose raw transcripts the vendor swept months ago, and that data is not
recoverable from anywhere else. Archiving the repositories is harmless; anyone
concluding that the *indexes* are therefore disposable would destroy the oldest
part of the corpus. See tohuw/muninn#6 and
.valholl/articles/archive-of-record.md.

So the honest migration order is: backfill first, verify nothing was lost, and
only then retire the predecessors. This module is the first step.

## The format, and why it is parsed rather than trusted

Both tools wrote ``# key: value`` headers followed by the prose body. The shapes
differ (claudex has ``kind``/``parent``/``branch``; codexdex has
``source``/``path``/``model``/``title``; claudex's cloud index has ``name`` and
no ``cwd`` at all), so headers are read generically and every field is optional.
A predecessor's format is exactly as unstable as a vendor's — more so, since
nobody is maintaining it — and the same fail-soft rule applies: a missing or
renamed key is an enumerated skip, never an exception.

## Prose-index sessions never overwrite raw-derived ones

``origin`` distinguishes them, and raw wins. A raw transcript yields tool calls,
files touched, models, token counts and per-turn structure; a prose file yields
text and a handful of headers. Overwriting the first with the second would be a
silent downgrade of an already-ingested session, which is why
``SkipReason.SUPERSEDED_BY_RICHER_ORIGIN`` exists and was reserved for exactly
this before any importer needed it. The skip is *recorded per item*, so "why is
this session not from the backfill" is a query rather than an inference.

## ``source_present = 0``, always

A prose-index entry exists because a predecessor archived a transcript, and for
most of this corpus that transcript is long gone. Muninn cannot see the original
from here — claudex did not even record its path — so claiming presence would
overstate what survives and corrupt the one statistic that matters most, the
count of sessions whose only copy is this archive. Recording absence is the
conservative direction and it is self-correcting: if the raw file does still
exist, the next `muninn index` ingests it as ``origin = 'raw'`` and sets presence
back.

``source_path`` is left ``NULL`` for the same reason, which also keeps these rows
out of the local sweep's reconciler — it only considers rows that name a path —
so nothing flaps them back and forth.

## The body is stored verbatim

Including the predecessors' ``[USER <timestamp>]`` markers, which differ from the
``[USER]`` form Muninn's own adapters emit. Normalising them would be a lossy
rewrite of the only surviving copy of that text to make it cosmetically match
text that is not at risk. The prose is the thing being rescued; it is not the
place to tidy.
"""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path

from . import sources
from .digest import digest_items, digest_tree
from .ingest import assert_conservation
from .receipt import (
    Attribution,
    Delta,
    ImportReceipt,
    Outcome,
    Skip,
    SkipReason,
    SourceFacts,
)
from .sources import ParsedSession
from .store import Store

SOURCE_KIND = "prose-index"
ORIGIN = "prose-index"

#: Where the predecessors keep their indexes, and the source each contributes.
#: ``cloud`` is a sub-index rather than a separate tool: claudex archived
#: claude.ai conversations alongside local ones, and they are ``claude-cloud``
#: sessions — the same source string ``exports.parse_claude_export`` writes, so
#: a backfilled conversation and a re-imported vendor export land on one row
#: instead of two.
KNOWN_ROOTS: tuple[tuple[str, str], ...] = (
    (".claudex", "claude"),
    (".codexdex", "codex"),
)

_CLOUD_SOURCE = "claude-cloud"


@dataclass(frozen=True)
class ProseIndexCandidate:
    """One predecessor index directory, and the default source for its files."""

    path: Path
    default_source: str

    @property
    def file_count(self) -> int:
        return len(discover(self.path))


def default_home() -> Path:
    return Path.home()


def find_prose_indexes(home: Path | None = None) -> list[ProseIndexCandidate]:
    """Predecessor indexes present under ``home``, in a stable order.

    Only directories that actually hold prose files are returned. An empty
    ``~/.codexdex`` is a real state on this corpus — codexdex was never run on
    the development machine before Muninn's design work — and reporting it as a
    source with nothing in it would be indistinguishable from a parse failure.
    """
    home = default_home() if home is None else home
    found = []
    for name, source in KNOWN_ROOTS:
        root = home / name
        if root.is_dir() and discover(root):
            found.append(ProseIndexCandidate(root, source))
    return found


def discover(root: Path) -> list[Path]:
    """Prose files under ``root``, sorted for determinism.

    Both ``index/`` and ``cloud/index/`` are walked. The cloud sub-index is easy
    to miss — it is a second directory holding a different session class — and
    missing it would silently drop every claude.ai conversation the predecessor
    archived, which is the older and less replaceable half.
    """
    if not root.is_dir():
        return []
    out: list[Path] = []
    for sub in ("index", "cloud/index"):
        directory = root / sub
        if directory.is_dir():
            out.extend(p for p in sorted(directory.glob("*.txt")) if p.is_file())
    return sorted(out)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_headers(text: str) -> tuple[dict[str, str], str]:
    """Split ``# key: value`` headers from the body.

    The header block ends at the first line that is not a ``#`` comment, so a
    body line beginning with ``#`` (a markdown heading in a transcript, which is
    extremely common) cannot be mistaken for a header. Unrecognised keys are
    kept rather than dropped: this is a format nobody maintains, and a caller
    that wants one key should not silently discard the evidence of others.
    """
    headers: dict[str, str] = {}
    lines = text.split("\n")
    body_starts = 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            body_starts = i
            break
        key, sep, value = line[1:].partition(":")
        if sep:
            headers.setdefault(key.strip().lower(), value.strip())
        body_starts = i + 1
    return headers, "\n".join(lines[body_starts:]).strip("\n")


def _int_after(turns: str, label: str) -> int:
    """``user=3 assistant=242`` -> the number after ``label``, or 0."""
    for token in turns.split():
        name, sep, value = token.partition("=")
        if sep and name == label:
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def parse_prose_file(path: Path, default_source: str) -> ParsedSession | Skip:
    """One prose file into a session, or an enumerated skip.

    Never raises. Every failure is a ``Skip`` carrying the file's stem as its id,
    so "which files did not make it, and why" is answerable from the ledger
    afterwards rather than by re-running and watching. A count could not be
    audited; this can.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Skip(item_id=path.stem, reason=SkipReason.READ_ERROR)

    headers, body = parse_headers(text)
    if not headers:
        return Skip(item_id=path.stem, reason=SkipReason.UNKNOWN_SCHEMA)

    session_id = headers.get("session") or path.stem
    if not session_id:
        return Skip(item_id=path.stem, reason=SkipReason.MISSING_ITEM_ID)
    if not body.strip():
        return Skip(item_id=session_id, reason=SkipReason.NO_CONTENT)

    kind = headers.get("kind", "")
    source = headers.get("source") or (_CLOUD_SOURCE if kind == "cloud" else default_source)
    turns = headers.get("turns", "")

    sess = ParsedSession(
        session_id=session_id,
        source=source,
        text=body,
        parent_id=headers.get("parent"),
        cwd=headers.get("cwd") or None,
        branch=headers.get("branch") or None,
        model=headers.get("model") or None,
        title=headers.get("title") or headers.get("name") or None,
        started_at=_iso_or_none(headers.get("start")),
        ended_at=_iso_or_none(headers.get("end")),
        user_turns=_int_after(turns, "user"),
        assistant_turns=_int_after(turns, "assistant"),
    )
    sess.duration_s = _duration(sess.started_at, sess.ended_at)
    # Provenance stays structural, exactly as for raw transcripts: the same
    # classifier, fed the cwd and turn counts the header preserved. Deciding it
    # from the prose length here would reintroduce the length-based
    # classification that skewed every measurement by ~40x.
    sess.provenance = sources.classify(sess, is_subagent=(kind == "subagent"))
    return sess


def _iso_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _duration(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (b - a).total_seconds()


# ── Import ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def import_prose_index(st: Store, root: Path, *, default_source: str = "claude",
                       actor: str = "cli") -> ImportReceipt:
    """Backfill one predecessor index. Mirrors ``exports.import_export``'s flow.

    ``windowed=False``: a prose index is a complete archive of what its tool ever
    saw, not a rolling window. That said, this importer **never reconciles** —
    it only ever touches sessions the index itself names, so a session absent
    from it is left entirely alone rather than flagged. Absence from a
    predecessor's index proves only that the predecessor did not index it.
    """
    root = Path(root)
    paths = discover(root)
    item_count = len(paths)

    parsed: list[ParsedSession] = []
    skips: list[Skip] = []
    for path in paths:
        result = parse_prose_file(path, default_source)
        (skips if isinstance(result, Skip) else parsed).append(result)

    # Digest over (id, end) pairs, so a re-run of an unchanged index is a
    # DUPLICATE with attribution rather than an "imported, 0 added" line an
    # agent has to interpret. digest_tree covers the bytes; this covers the
    # content, which is what "the same import" means.
    pairs = [(s.session_id, s.ended_at or "") for s in parsed]
    items_digest = digest_items(SOURCE_KIND, pairs)

    started = [s.started_at for s in parsed if s.started_at]
    ended = [s.ended_at for s in parsed if s.ended_at]
    facts = SourceFacts(
        kind=SOURCE_KIND, digest=items_digest, item_count=item_count,
        span_earliest=min(started) if started else None,
        span_latest=max(ended) if ended else None,
        windowed=False,
        file_digest=digest_tree(root, paths) if paths else None,
    )

    prior_completed = st.find_import_by_digest(items_digest)
    ledger_id = st.begin_import(actor=actor, source_kind=SOURCE_KIND,
                                source_ref=str(root), source_digest=items_digest,
                                facts=facts)

    holder = st.acquire_import_lock(ledger_id, actor, os.getpid())
    if holder is not None:
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta())
        return ImportReceipt(
            ledger_id=ledger_id, outcome=Outcome.REJECTED, source=facts, delta=Delta(),
            attribution=Attribution(ledger_id=holder["ledger_id"], actor=holder["actor"],
                                    finished_at=holder["acquired_at"]),
        )

    now = _now()
    added = updated = unchanged = 0
    item_receipts: list[tuple[str, str, str | None]] = []

    for sess in parsed:
        existing = st.get_session(sess.session_id)
        if existing is not None and existing.get("origin") != ORIGIN:
            # A raw-derived session is strictly richer. Recorded as a skip with
            # a reason rather than passed over, so the decision is auditable.
            skips.append(Skip(item_id=sess.session_id,
                              reason=SkipReason.SUPERSEDED_BY_RICHER_ORIGIN))
            continue
        if existing is not None and existing.get("ended_at") == sess.ended_at:
            unchanged += 1
            item_receipts.append((sess.session_id, "unchanged", None))
            continue

        st.upsert_session({
            "session_id": sess.session_id,
            "source": sess.source,
            "provenance": sess.provenance,
            "parent_id": sess.parent_id,
            "cwd": sess.cwd,
            "branch": sess.branch,
            "model": sess.model,
            "title": sess.title,
            "started_at": sess.started_at,
            "ended_at": sess.ended_at,
            "duration_s": sess.duration_s,
            "user_turns": sess.user_turns,
            "assistant_turns": sess.assistant_turns,
            # Zero because the columns are NOT NULL and a prose index recorded
            # no tool activity — which is "we cannot know", not "there was
            # none". The schema has no way to say the first, and inventing one
            # for a backfill would be a wide change for a narrow gain; the
            # `origin` column is what tells a reader how much to trust these.
            "tool_uses": 0,
            "tool_results": 0,
            "words": sess.words,
            "text": sess.text,
            # NULL path and absent source: see the module docstring. These rows
            # are the irreplaceable ones by construction.
            "source_path": None,
            "source_present": 0,
            "origin": ORIGIN,
            "ingested_at": now if existing is None else None,
            "updated_at": now,
        })
        st.replace_chunks(sess.session_id, sess.text)

        if existing is None:
            added += 1
            item_receipts.append((sess.session_id, "added", None))
        else:
            updated += 1
            item_receipts.append((sess.session_id, "updated", None))

    for skip in skips:
        item_receipts.append((skip.item_id, "skipped", skip.reason.value))

    assert_conservation(added, updated, unchanged, len(skips), item_count)
    delta = Delta(added=added, updated=updated, unchanged=unchanged, skipped=len(skips))

    duplicate_of: int | None = None
    if added == 0 and updated == 0:
        if skips and unchanged == 0 and item_count > 0 and prior_completed is None:
            outcome = Outcome.REJECTED
        elif prior_completed is not None:
            outcome = Outcome.DUPLICATE
            duplicate_of = prior_completed["ledger_id"]
        else:
            outcome = Outcome.IMPORTED
    else:
        outcome = Outcome.PARTIAL if skips else Outcome.IMPORTED

    st.record_items(ledger_id, item_receipts)
    st.finish_import(ledger_id, outcome=outcome, delta=delta, duplicate_of=duplicate_of)
    st.release_import_lock(ledger_id)

    attribution: Attribution | None = None
    if outcome is Outcome.DUPLICATE and prior_completed is not None:
        attribution = Attribution(ledger_id=prior_completed["ledger_id"],
                                  actor=prior_completed["actor"],
                                  finished_at=prior_completed["finished_at"])

    st.commit()
    return ImportReceipt(ledger_id=ledger_id, outcome=outcome, source=facts, delta=delta,
                         skips=skips, duplicate_of=duplicate_of, attribution=attribution)
