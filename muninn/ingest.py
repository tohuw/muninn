"""Ingest: move transcripts into the archive without ever losing any.

The single most dangerous failure mode this module must avoid: a reconciling
pass that "cleans up" sessions it can no longer see on disk. Because Claude Code
sweeps transcripts after ``cleanupPeriodDays``, a source file's absence is the
normal end state, not a signal to delete. Absence is recorded
(``source_present = 0``); the prose stays.

See .valholl/articles/archive-of-record.md and
.valholl/articles/continuous-ingest-not-periodic.md.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from . import sources
from .store import Store


@dataclass
class IngestResult:
    scanned: int = 0
    ingested: int = 0
    updated: int = 0
    skipped_unchanged: int = 0
    parse_failures: int = 0
    failures_by_category: dict[str, int] = field(default_factory=dict)
    marked_missing: int = 0

    def merge_failures(self, failures: dict[str, int]) -> None:
        for category, count in failures.items():
            self.failures_by_category[category] = (
                self.failures_by_category.get(category, 0) + count)
            self.parse_failures += count


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ingest_path(st: Store, root: Path, source: str,
                chunk_words: int | None = None,
                chunk_stride: int | None = None) -> IngestResult:
    """Ingest every transcript for ``source`` found under ``root``.

    Incremental: a file whose size and mtime are unchanged since the last pass
    is skipped outright. A grown file is re-parsed in full rather than tailed,
    because a session's prose, turn counts and metadata are computed over the
    whole transcript; the stored offset is used to detect growth, not to
    reconstruct state. Correctness first — tailing is an optimization that can
    land once it is proven not to drop turns.
    """
    root = Path(root)
    result = IngestResult()
    parser = sources.PARSERS[source]
    now = _now()

    for path in sources.discover(root, source):
        result.scanned += 1
        try:
            stat = path.stat()
        except OSError:
            result.merge_failures({"stat_failed": 1})
            continue

        prior = st.get_ingest_state(str(path))
        if (prior and prior["size_bytes"] == stat.st_size
                and prior["mtime"] == stat.st_mtime):
            result.skipped_unchanged += 1
            continue

        try:
            parsed, offset = parser(path, 0)
        except OSError:
            result.merge_failures({"read_failed": 1})
            continue
        except Exception:
            # A parser bug or an unanticipated format must not abort the sweep.
            result.merge_failures({"parser_exception": 1})
            continue

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
        if existed:
            result.updated += 1
        else:
            result.ingested += 1

    result.marked_missing = _reconcile_missing(st, source, now)
    st.commit()
    return result


def _reconcile_missing(st: Store, source: str, now: str) -> int:
    """Record vanished sources. **Never deletes.**

    A missing raw file is the expected outcome of the vendor's retention sweep.
    Marking ``source_present = 0`` is what lets `doctor` report how much of the
    archive is now irreplaceable.
    """
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
