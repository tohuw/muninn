"""A filesystem job queue: the only thing the ``SessionEnd`` hook may touch.

.valholl/articles/session-lifecycle-facts.md is unambiguous: ``SessionEnd``
shares a 1.5-second budget across every ``SessionEnd`` hook, does not support
``async: true``, and cannot block session exit. Opening SQLite from inside the
hook risks lock contention with a concurrent indexer pass, which could burn
that whole budget on a single write. So the hook never touches the archive at
all -- it drops one small JSON file into a directory and exits. Everything
that requires the store (parsing, upserting, the ledger) happens later, out of
band, in ``muninn/indexer.py``.

No database here on purpose: a directory of files needs no schema migration,
no lock file, and can't corrupt worse than "one truncated file," which
``drain()`` isolates rather than propagates.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from .paths import QUEUE_DIR

# import_items disposition needs len(job) as a natural key elsewhere; queue
# jobs use a fresh uuid4 filename instead of anything derived from job
# contents, because two hook invocations for the same session (e.g. a
# ``/clear`` immediately followed by exit) must both survive as distinct jobs
# -- the drain side, not the enqueue side, is where duplicate detection
# belongs (the ledger's digest/actor machinery from spec 001).
_SUFFIX = ".json"
_TMP_SUFFIX = ".tmp"
_BAD_DIR = "bad"


def enqueue(job: dict[str, Any], *, queue_dir: Path = QUEUE_DIR) -> Path | None:
    """Atomically add a job. Fast, and must never raise.

    Writes ``<uuid>.tmp`` then ``os.replace()``s it to ``<uuid>.json`` so a
    concurrent ``drain()`` can never observe a partially written file --
    ``os.replace`` is atomic on POSIX and Windows alike as long as both paths
    are on the same filesystem, which they are (both under ``queue_dir``).

    Returns the final path on success, or ``None`` on any failure (queue dir
    unwritable, disk full, permission denied, ...). The caller -- the
    SessionEnd hook -- must never let an exception here reach the user; this
    function absorbs that so the hook's own try/except is a second line of
    defense, not the only one.
    """
    try:
        queue_dir = Path(queue_dir)
        queue_dir.mkdir(parents=True, exist_ok=True)
        job_id = uuid.uuid4().hex
        tmp_path = queue_dir / f"{job_id}{_TMP_SUFFIX}"
        final_path = queue_dir / f"{job_id}{_SUFFIX}"
        payload = json.dumps(job)
        with tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, final_path)
        return final_path
    except OSError:
        return None


def pending_count(*, queue_dir: Path = QUEUE_DIR) -> int:
    """Count of unread job files, excluding ``bad/`` -- used by ``doctor``."""
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return 0
    return sum(1 for p in queue_dir.glob(f"*{_SUFFIX}") if p.is_file())


def bad_count(*, queue_dir: Path = QUEUE_DIR) -> int:
    """Count of jobs quarantined as malformed -- non-zero means investigate."""
    bad_dir = Path(queue_dir) / _BAD_DIR
    if not bad_dir.is_dir():
        return 0
    return sum(1 for p in bad_dir.glob(f"*{_SUFFIX}") if p.is_file())


def oldest_pending_age_s(*, queue_dir: Path = QUEUE_DIR) -> float | None:
    """Age in seconds of the oldest unread job, or ``None`` if the queue is empty.

    ``doctor`` warns past 5 minutes: that means the drain loop is wedged, not
    just "a session recently ended."
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return None
    oldest_mtime: float | None = None
    for p in queue_dir.glob(f"*{_SUFFIX}"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if oldest_mtime is None or mtime < oldest_mtime:
            oldest_mtime = mtime
    if oldest_mtime is None:
        return None
    return max(0.0, time.time() - oldest_mtime)


def drain(*, queue_dir: Path = QUEUE_DIR, limit: int | None = None) -> list[dict[str, Any]]:
    """Read and remove jobs, oldest first by mtime.

    A malformed job file (truncated write, corrupted JSON, non-object payload)
    is moved to ``queue_dir/bad/`` rather than deleted -- it is evidence a
    write went wrong, and ``doctor`` surfaces a non-empty ``bad/`` as a
    warning. Deleting it would make that failure invisible, the exact mistake
    continuous-ingest-not-periodic.md is about for the ingest side generally.
    """
    queue_dir = Path(queue_dir)
    if not queue_dir.is_dir():
        return []
    bad_dir = queue_dir / _BAD_DIR

    entries: list[tuple[float, Path]] = []
    for p in queue_dir.glob(f"*{_SUFFIX}"):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        entries.append((mtime, p))
    entries.sort(key=lambda pair: (pair[0], pair[1].name))

    jobs: list[dict[str, Any]] = []
    for _, path in entries:
        if limit is not None and len(jobs) >= limit:
            break
        try:
            raw = path.read_text(encoding="utf-8")
            job = json.loads(raw)
            if not isinstance(job, dict):
                raise ValueError("job payload is not a JSON object")
        except (OSError, ValueError, UnicodeDecodeError):
            bad_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, bad_dir / path.name)
            except OSError:
                pass  # best effort; a file we can't move, we also can't re-read forever
            continue
        try:
            path.unlink()
        except OSError:
            # Removal failed but we already have the parsed job in hand; a
            # re-drain may see it again and re-import it, which is safe
            # because import is idempotent (spec 001's ledger digest).
            pass
        jobs.append(job)
    return jobs
