"""The drain loop, watcher, and sweep — everything the queue hands off to.

Three layers, cheapest first (see docs/specs/003-background-indexer.md and
.valholl/articles/continuous-ingest-not-periodic.md):

1. ``drain_once`` — imports whatever the SessionEnd hook enqueued. Fast and
   eager, but the hook can be missed entirely (crash, SIGKILL, window close;
   see .valholl/articles/session-lifecycle-facts.md).
2. ``watch`` — a long-running loop that also reacts to raw file changes via
   ``watchfiles``, catching sessions whose hook never fired.
3. ``sweep`` — a full reconciling scan (this is exactly ``ingest.ingest_path``
   over every configured root). This is the only layer that *closes* the
   guarantee, because a hook can be missed and the watcher can miss events
   while the daemon itself is down.

Nothing in this module may be imported by ``muninn/hooks/cli.py`` — it pulls
in ``muninn.store`` (hence ``sqlite3``) and ``muninn.ingest`` (hence every
parser), which is exactly the weight the hook must never carry.
"""
from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Callable, Iterator

from . import ingest, queue
from .receipt import ImportReceipt
from .store import Store

logger = logging.getLogger("muninn.indexer")

# Linux inotify has a systemwide watch-descriptor limit (commonly 8192 or
# 65536) that a corpus of thousands of per-session transcript files can hit
# well before disk or CPU become a concern — see
# continuous-ingest-not-periodic.md, "File watching". watchfiles surfaces this
# as an OSError from the underlying notify backend; there is no dedicated
# exception type across platforms, so the fallback below matches on errno.
_INOTIFY_LIMIT_ERRNOS = {24, 28}  # EMFILE (too many open files), ENOSPC (inotify watch limit)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def drain_once(st: Store, *, actor: str = "hook",
               queue_dir: Path = queue.QUEUE_DIR,
               limit: int | None = None) -> list[ImportReceipt]:
    """Drain the queue once; import each job's transcript via spec 001.

    A job whose ``transcript_path`` is missing or whose ``kind`` is not
    recognized still gets acknowledged (removed from the queue) rather than
    retried forever — ``ingest.ingest_file`` records a REJECTED ledger row for
    a vanished file so the attempt stays visible to `doctor`, per
    .valholl/articles/archive-of-record.md's spirit that absence is data, not
    a reason to pretend nothing happened.
    """
    jobs = queue.drain(queue_dir=queue_dir, limit=limit)
    receipts: list[ImportReceipt] = []
    for job in jobs:
        transcript_path = job.get("transcript_path")
        if not transcript_path:
            logger.warning("muninn indexer: job missing transcript_path, dropped: %r", job)
            continue
        # A session's own source (claude vs codex) is not carried by the
        # SessionEnd payload; every hook job originates from Claude Code
        # (Codex has no hook mechanism — see spec guardrails), so "claude" is
        # not a guess, it's the only vendor that can produce this job kind.
        result = ingest.ingest_file(st, Path(transcript_path), source="claude", actor=actor)
        if result.receipt is not None:
            receipts.append(result.receipt)
    return receipts


def sweep(st: Store, roots: dict[str, Path], *, actor: str = "sweep") -> list[ImportReceipt]:
    """Full reconciling scan over every configured root.

    This is what closes the guarantee continuous-ingest-not-periodic.md
    describes: a hook can be missed and the watcher can miss events while the
    daemon is down, but a sweep that runs on every startup (and periodically
    while the watcher is up) eventually catches everything either one missed.
    """
    receipts: list[ImportReceipt] = []
    for source, root in roots.items():
        if not Path(root).is_dir():
            continue
        result = ingest.ingest_path(st, root, source, actor=actor)
        if result.receipt is not None:
            receipts.append(result.receipt)
    st.record_sweep(_now())
    return receipts


def _default_watch(paths: list[str], *, interval_s: float,
                   force_polling: bool) -> Iterator[set]:
    """Thin wrapper over ``watchfiles.watch`` so ``watch()`` below can accept
    an injected replacement in tests without importing watchfiles at all in
    that path — see acceptance test 7, which asserts sweep-before-watch via a
    fake watcher rather than waiting on a real filesystem event.
    """
    import watchfiles

    yield from watchfiles.watch(
        *paths, rust_timeout=int(interval_s * 1000), yield_on_timeout=True,
        raise_interrupt=False, force_polling=force_polling or None,
    )


def watch(st: Store, roots: dict[str, Path], *, interval_s: float = 2.0,
          sweep_interval_s: float = 900.0,
          queue_dir: Path = queue.QUEUE_DIR,
          max_iterations: int | None = None,
          watch_fn: Callable[..., Iterator[set]] | None = None,
          on_receipts: Callable[[list[ImportReceipt]], None] | None = None) -> None:
    """Long-running loop: sweep, then drain the queue and react to file events.

    Must run a sweep on startup BEFORE watching — events during downtime were
    missed and only a sweep recovers them (session-lifecycle-facts.md,
    "Design consequences for the indexer", point 2).

    ``max_iterations`` bounds the loop for tests; production callers leave it
    ``None`` and run forever. ``watch_fn`` lets a test inject a fake event
    source instead of a real ``watchfiles.watch`` generator. ``on_receipts``,
    if given, is called with every batch of import receipts produced by a
    drain or a sweep — this is how ``muninn index --watch`` logs one line per
    import without this module needing to know anything about the CLI's
    presentation.
    """
    import time

    watch_fn = watch_fn or _default_watch

    def _emit(receipts: list[ImportReceipt]) -> list[ImportReceipt]:
        if on_receipts is not None and receipts:
            on_receipts(receipts)
        return receipts

    # The sweep that closes the "missed everything while the daemon was down"
    # gap must complete before the first event wait, not concurrently with
    # it — otherwise a session written in the gap between process start and
    # the first watchfiles poll could be missed by both.
    _emit(sweep(st, roots, actor="sweep"))

    watch_paths = [str(root) for root in roots.values() if Path(root).is_dir()]
    last_sweep = time.monotonic()
    force_polling = False
    iterations = 0
    generator: Iterator[set] | None = None

    while True:
        if max_iterations is not None and iterations >= max_iterations:
            return
        iterations += 1

        # Drain the queue every iteration regardless of whether a file event
        # fired: the hook's job is the primary path and should not wait on a
        # filesystem event that may never arrive for a session with no
        # further writes (e.g. it already ended).
        _emit(drain_once(st, actor="hook", queue_dir=queue_dir))

        if time.monotonic() - last_sweep >= sweep_interval_s:
            _emit(sweep(st, roots, actor="sweep"))
            last_sweep = time.monotonic()

        if not watch_paths:
            time.sleep(interval_s)
            continue

        if generator is None:
            generator = iter(watch_fn(
                watch_paths, interval_s=interval_s, force_polling=force_polling))

        try:
            changes = next(generator, None)
        except OSError as exc:
            if getattr(exc, "errno", None) in _INOTIFY_LIMIT_ERRNOS and not force_polling:
                # Thousands of transcript files is exactly where the inotify
                # watch-descriptor limit bites (continuous-ingest-not-periodic.md,
                # "File watching"). Falling back to polling keeps the watcher
                # alive in a degraded mode rather than crashing the daemon.
                logger.warning(
                    "muninn indexer: inotify limit hit (%s); falling back to polling", exc)
                force_polling = True
                generator = None
                continue
            raise

        if changes:
            _emit(drain_once(st, actor="hook", queue_dir=queue_dir))
            for source, root in roots.items():
                if Path(root).is_dir():
                    result = ingest.ingest_path(st, root, source, actor="watcher")
                    if result.receipt is not None:
                        _emit([result.receipt])
