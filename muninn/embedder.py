"""The background embedding worker: what makes "semantic" automatic.

Normative source: docs/specs/014-automatic-embedding.md. Read
.valholl/articles/embedding-is-not-a-chore.md first — it is why this exists at
all, and it is normative over this file on the *why*.

## What changed, and why a worker exists now

Until spec 014, embeddings were generated only by a human typing ``muninn
embed``. Everything else about retrieval was automatic: the daemon ingests
continuously, FTS5 is written on every import, and ``search`` needs no
preparation. Semantic search alone had a manual prerequisite, and the failure
that produced was not an error — it was a *worse answer*, returned confidently,
for exactly the sessions a person had most recently created. The archive said
semantic search was available; the vectors for last week were simply absent.

This is the same shape as the staleness argument in
.valholl/articles/continuous-ingest-not-periodic.md, one layer up: a step a
human has to remember is not a guarantee, and the version of it that loses is
the one nobody notices losing.

So ``muninn serve`` now owns a worker that drains the same backlog the CLI
drains, and ``muninn embed`` remains — as the foreground, bounded,
``--dry-run``-able path for a backfill someone wants to watch and pay for
deliberately. Neither is a reimplementation of the other: both call
``embed.pending_chunks`` and ``embed.store_vectors``.

## Why a thread, and why it opens its own connection

The worker cannot run inside ``indexer.watch``'s loop. A provider call is a
network round trip for a hosted embedder and a weight-loaded forward pass for a
local one; either one blocks the loop that owns continuous ingest, and
"ingest never waits on anything else" is a durability rule here, not a
preference (continuous-ingest-not-periodic.md, "Never blocks the agent").

``sqlite3`` connections default to ``check_same_thread=True``, so the worker
opens its **own** ``Store`` inside its own thread rather than borrowing the
daemon's. That is not a workaround for the check: two threads sharing one
connection would serialise the ingest loop behind embedding writes, which is the
thing being avoided. WAL mode plus the 30 s ``busy_timeout`` that
``store.open_store`` already sets is what makes two writers to one archive
ordinary rather than exceptional, and ``store_vectors`` is an UPSERT, so a
concurrent ``muninn embed`` costs duplicated work and never a duplicated row.

## Every failure here is non-fatal to the daemon, and the reasons differ

A daemon that dies because an embedding provider had a bad afternoon has traded
a missing convenience for the data loss this whole project exists to prevent.
So nothing in this module propagates, and the three failure classes are handled
differently on purpose:

- **No provider installed** — the default build ships none. ``start()`` returns
  ``False``, logs once, and the daemon carries on. This is the normal state, not
  a warning.
- **``PolicyRefused``** — the model policy chokepoint refused this model. That is
  a *decision*, not an outage: retrying cannot change it, and a loop that retried
  would turn a governance refusal into a hot loop against a metered API. The
  worker stops permanently and records the reason.
- **Anything else** (a network error, a dimension mismatch, a provider bug) —
  transient until proven otherwise: back off exponentially and retry, so a
  laptop that was offline for an hour catches up by itself.

## The stall guard, which is about money

A pass that writes zero vectors while the backlog is non-empty means something
is wrong that retrying will not fix — a provider returning fewer vectors than
texts, or every row failing ``DimensionMismatch``. Without a guard, that is an
unbounded loop that calls a paid API forever and never shrinks the queue. After
:data:`STALL_LIMIT` consecutive stalled passes the worker stops and says so.
Stopping visibly is the cheaper failure; `doctor` reports the backlog it left.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from . import embed, store
from .policy import PolicyRefused

logger = logging.getLogger("muninn.embedder")

#: How long to wait before looking for new work once the backlog is empty. A
#: poll rather than a signal from the ingest loop: the two run in different
#: threads over different connections, and 30 s of latency on "my last session
#: became semantically searchable" is imperceptible next to the coupling a
#: cross-thread wakeup would add. The wait is on an ``Event``, so shutdown does
#: not sit through it.
DEFAULT_IDLE_INTERVAL_S = 30.0

#: Chunks per provider call. Matches ``muninn embed``'s default so the automatic
#: path and the manual one cost the same per chunk.
DEFAULT_BATCH = 64

#: Batches fetched per query. ``pending_chunks`` scans the FTS5 table, so asking
#: for one batch at a time would repeat that scan per provider call; asking for
#: several amortises it without holding a large result set.
WINDOW_BATCHES = 8

#: Consecutive zero-progress passes tolerated before the worker gives up. See
#: the module docstring, "The stall guard".
STALL_LIMIT = 3

#: Backoff bounds for a transient provider failure.
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 300.0

# Why the worker stopped, as a closed vocabulary — the same discipline
# ``daemon.HOLDERS`` follows, because these strings are read back by `doctor`
# and a free-text reason is a value a reader starts parsing.
STOPPED_NOT_STARTED = "not-started"
STOPPED_NO_PROVIDER = "no-provider"
STOPPED_REQUESTED = "requested"
STOPPED_POLICY = "policy-refused"
STOPPED_STALLED = "stalled"
STOPPED_RUNNING = "running"


class BackgroundEmbedder:
    """Drains the embedding backlog in a thread, for as long as the daemon lives.

    Composition, like everything the daemon owns: the work is
    ``embed.pending_chunks`` → ``provider.embed`` → ``embed.store_vectors``, and
    this class contributes only *when* that happens and what to do when it
    fails.

    ``announce`` is the daemon's line printer, so the one thing a person needs to
    know — how big the backlog was when the daemon started — reaches the same
    place every other startup line does. Silence about a backlog is how a
    metered provider surprises someone.
    """

    def __init__(self, db_path: str | Path, *,
                 provider: embed.EmbeddingProvider | None = None,
                 batch: int = DEFAULT_BATCH,
                 idle_interval_s: float = DEFAULT_IDLE_INTERVAL_S,
                 window_batches: int = WINDOW_BATCHES,
                 backoff_start_s: float = BACKOFF_START_S,
                 backoff_max_s: float = BACKOFF_MAX_S,
                 stall_limit: int = STALL_LIMIT,
                 announce: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.batch = max(1, batch)
        self.idle_interval_s = idle_interval_s
        self.window_batches = max(1, window_batches)
        # Instance attributes rather than the module constants read directly, so
        # a test can exercise the retry and stall paths without waiting out a
        # real backoff. The defaults are the constants; nothing in production
        # passes these.
        self.backoff_start_s = backoff_start_s
        self.backoff_max_s = backoff_max_s
        self.stall_limit = stall_limit
        self.announce = announce or (lambda _msg: None)

        # Injected in tests; resolved at start() otherwise, because
        # resolve_provider() consults installed plugins and must not run at
        # import time.
        self.provider = provider

        self.vectors_written = 0
        self.passes = 0
        self.last_error: str | None = None
        self.stopped_reason: str = STOPPED_NOT_STARTED

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Resolve a provider and start the thread. ``False`` if there is none.

        Returning a bool rather than raising is deliberate and mirrors
        ``ravenserve.attach``: the caller is the daemon, "no embedding provider"
        is the default install's normal state, and spec 009's rule that an extra
        must never cost the indexer its ingest applies here unchanged.
        """
        if self._thread is not None:
            return True
        if self.provider is None:
            try:
                self.provider = embed.resolve_provider()
            except embed.EmbeddingUnavailable as exc:
                # Not a warning: a default install has no provider by design.
                logger.info("muninn embedder: not started — %s", exc)
                self.stopped_reason = STOPPED_NO_PROVIDER
                return False

        self.stopped_reason = STOPPED_RUNNING
        self._stop.clear()
        # A daemon thread as a backstop only. The ordinary path is
        # ``stop()`` from the daemon's ``finally``; this flag is what stops a
        # wedged provider call from holding the interpreter open after the
        # ingest loop has already unwound.
        self._thread = threading.Thread(
            target=self._run, name="muninn-embedder", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to finish its batch and join it. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Joining timed out inside a provider call. Say so rather than
                # blocking the daemon's teardown: the writes are committed per
                # batch, so what is lost is at most one batch of work, and the
                # thread is a daemon thread so the interpreter can still exit.
                logger.warning("muninn embedder: still busy after %.0fs; "
                               "leaving it to be reaped at exit", timeout)
        if self.stopped_reason == STOPPED_RUNNING:
            self.stopped_reason = STOPPED_REQUESTED

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        """A flat dict for `doctor` and for tests to assert against."""
        return {
            "provider": getattr(self.provider, "name", None),
            "model": getattr(self.provider, "model", None),
            "running": self.running,
            "reason": self.stopped_reason,
            "vectors": self.vectors_written,
            "passes": self.passes,
            "last_error": self.last_error,
        }

    # ── the loop ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Own connection, own lifetime, no exception left unlogged."""
        try:
            st = store.open_store(self.db_path)
        except Exception as exc:            # noqa: BLE001 - see the class docstring
            logger.warning("muninn embedder: cannot open the archive: %s", exc)
            self.last_error = str(exc)
            self.stopped_reason = STOPPED_STALLED
            return
        try:
            self._loop(st)
        except Exception as exc:            # noqa: BLE001 - a thread that dies
            # silently is the failure mode this whole module is written against.
            logger.exception("muninn embedder: stopping after an unhandled error")
            self.last_error = str(exc)
            self.stopped_reason = STOPPED_STALLED
        finally:
            st.close()

    def _loop(self, st: store.Store) -> None:
        provider = self.provider
        assert provider is not None      # start() guarantees it before threading

        backlog = embed.pending_count(st, provider.model)
        if backlog:
            # The one line that must not be silent: on a fresh archive this is
            # thousands of chunks and, against a hosted provider, a bill.
            self.announce(f"muninn embedder: {backlog:,} chunk(s) pending for "
                          f"{provider.model}; embedding in the background")
        else:
            self.announce(f"muninn embedder: up to date for {provider.model}")

        stalls = 0
        backoff = self.backoff_start_s
        while not self._stop.is_set():
            try:
                written, seen = self._pass(st, provider)
            except PolicyRefused as exc:
                # A refusal is a decision. Retrying it would loop forever, and
                # against a metered provider it would do so expensively.
                logger.warning("muninn embedder: stopping — model policy refused "
                               "%s: %s", provider.model, exc)
                self.last_error = str(exc)
                self.stopped_reason = STOPPED_POLICY
                return
            except Exception as exc:       # noqa: BLE001 - transient until proven
                logger.warning("muninn embedder: pass failed (%s); retrying in %.0fs",
                               exc, backoff)
                self.last_error = str(exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.backoff_max_s)
                continue

            self.passes += 1
            backoff = self.backoff_start_s

            if seen == 0:
                stalls = 0
                if self._stop.wait(self.idle_interval_s):
                    return
                continue

            if written == 0:
                stalls += 1
                if stalls >= self.stall_limit:
                    logger.warning(
                        "muninn embedder: stopping — %d passes made no progress with "
                        "%d chunk(s) still pending; run `muninn embed` to see the error",
                        stalls, embed.pending_count(st, provider.model))
                    self.stopped_reason = STOPPED_STALLED
                    return
                if self._stop.wait(backoff):
                    return
                continue

            stalls = 0
            # Loop straight into the next window rather than sleeping: a
            # backlog should drain as fast as the provider allows, and the idle
            # wait above is what keeps the thread cheap once it is empty.

    def _pass(self, st: store.Store, provider: embed.EmbeddingProvider) -> tuple[int, int]:
        """Embed up to one window of pending chunks. Returns ``(written, seen)``.

        ``seen`` is how many pending rows the query found, and it is reported
        separately from ``written`` so the caller can tell "there is no work"
        from "there is work and none of it succeeded" — the second is the stall
        this module guards against and the first is the steady state.

        Committed per batch, exactly as ``muninn embed`` does: a worker killed
        mid-backlog keeps every vector it has already paid for.
        """
        rows = embed.pending_chunks(
            st, provider.model,
            limit=self.batch * self.window_batches, newest_first=True)
        if not rows:
            return 0, 0

        written = 0
        for start in range(0, len(rows), self.batch):
            if self._stop.is_set():
                break
            window = rows[start:start + self.batch]
            vectors = provider.embed([r["body"] for r in window])
            for row, vector in zip(window, vectors):
                written += embed.store_vectors(
                    st, row["session_id"], provider.model, provider.dim,
                    [list(vector)], start_ordinal=row["ordinal"])
            st.commit()
        self.vectors_written += written
        if written:
            # New rows mean any cached matrix in this process is stale. The
            # cross-process case is already covered by embed._stamp; this is the
            # in-process one, and the daemon is exactly a process that outlives
            # the write.
            embed.clear_cache()
        return written, len(rows)
