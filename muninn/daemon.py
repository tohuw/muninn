"""The daemon: one process that owns continuous ingest and the raven surface.

Normative source: docs/specs/010-daemon.md. Read
.valholl/articles/continuous-ingest-not-periodic.md first — it is why an ingest
loop has to exist at all, and it is normative over this file on the *why*.

## What changed, and why a daemon exists now

Before this module, Muninn had no long-running process of its own. Spec 009
bolted the raven descriptor and ``/api/menu`` onto ``muninn index --watch``
because that was the only thing that ran for any length of time, and recorded the
consequence honestly: **Muninn vanished from the shared menubar whenever nobody
happened to be running the watcher.** ``fastapi`` and ``uvicorn`` were declared
dependencies that no module imported, and every entry point was a one-shot CLI
exit. That gap was left as an explicit owner decision in spec 009, "The lifecycle
question". The owner decided: *Muninn needs a daemon to be grabbing sessions.*

So the division of labour is now:

- ``muninn serve`` — **the service.** Sweeps, drains the queue, watches for file
  changes, embeds new chunks in the background (spec 014), publishes the raven
  descriptor, serves ``/api/menu``, writes a state file an external supervisor
  can read, and tears all of it down on a signal.
- ``muninn index --watch`` — **the foreground/debug path.** The same ingest loop
  and nothing else: no port, no descriptor, no state file, no embedding. It is
  how someone watches ingest happen without installing a service, and it is
  deliberately not a second publisher of the descriptor.

Naming the mistake a reader might make: this module does **not** reimplement the
ingest loop. ``indexer.watch()`` is the engine and stays the engine; the daemon
owns *running* it, which is a different job. In particular ``watch()``'s startup
sweep is not optional and not duplicated here — events that occurred while the
daemon was down were missed by every watcher, and only a sweep recovers them
(.valholl/articles/session-lifecycle-facts.md, "The sweep is not optional").

## Why the teardown is signal-driven rather than trusted to ``finally``

Python's default ``SIGTERM`` disposition terminates the process **without
unwinding the stack**, so a ``finally:`` never runs. This is not theoretical: it
bit Huginn as issue #43, where an ordinary "stop" from the menu bar left
``daemon.json``, the auth token, and the raven descriptor all orphaned — the
descriptor naming a port nothing was listening on. ``SIGHUP`` is handled for the
same reason and is easier to forget: a daemon started from a terminal that then
closes gets ``SIGHUP``, and losing the teardown there orphans exactly the same
files. ``SIGINT`` already raises ``KeyboardInterrupt``, which unwinds, so it is
deliberately left alone — claiming it would swap a working path for an untested
one.

## Why there is a single-instance lock

Two ingest loops against one archive is not a data-corruption bug — spec 001's
``import_lock`` serialises individual imports and is tested for it
(``tests/test_indexer.py::ConcurrentImportTest``). It is a *lifecycle* bug, and
these are the two failures worth naming:

1. Both loops drain the same queue directory and both sweep the same roots, so
   every transcript is parsed twice and each loop's receipts describe work the
   other already did.
2. Both publish the same descriptor path. The last writer wins, and then the
   **loser's** teardown deletes the winner's descriptor — so a perfectly healthy
   daemon silently drops out of the menubar. Huginn hit precisely this shape
   (its issue #40) when two daemons could both pass a connect-probe during a
   restart.

So ``serve`` and ``index --watch`` both take one advisory whole-file lock, and
whichever starts second refuses with the holder named.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import embedder, enricher, indexer, paths, raven, ravenserve, store
from .receipt import ImportReceipt

logger = logging.getLogger("muninn.daemon")

#: Written into the lock file so ``doctor`` can name what is holding it. A
#: **closed vocabulary**, per CLAUDE.md's "enumerate, don't count" discipline:
#: anything else read back out of the file is reported as ``unknown`` rather
#: than echoed, because a label read from a file must never be free text a
#: reader assumes is trustworthy.
HOLDER_SERVE = "serve"
HOLDER_WATCH = "index --watch"
HOLDERS = (HOLDER_SERVE, HOLDER_WATCH)

#: Longest prefix of the lock file that is ever read. It holds one short line;
#: a longer file means something else wrote there, and the bound is what stops
#: that turning into an unbounded read on the doctor path.
_MAX_LOCK_READ = 256

#: Returned by ``muninn serve`` when another ingest loop already holds the lock.
#: 1 rather than 0: a supervisor that treats "already running" as success will
#: happily report a daemon it did not start.
EXIT_ALREADY_RUNNING = 1


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only mode. A no-op on Windows, which uses ACLs.

    A deliberate duplicate of ``raven._restrict``, not an import of it. Reaching
    across for a private name would couple the daemon's permission discipline to
    the raven protocol module, and importing ``muninn.paths`` into ``raven.py``
    to share one from there would put ``paths.STATE_DIR`` in front of every
    reader of the module whose central warning is *do not publish the descriptor
    to ``paths.STATE_DIR``* (see ``raven.state_dir``). ``store.py`` already
    chmods inline for the same reason; five lines is the cheaper of the two
    couplings.
    """
    if sys.platform == "win32":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


def state_path() -> Path:
    """Where the daemon records itself for other processes to find.

    ``paths.STATE_DIR`` is read at call time rather than captured at import, so
    a test can point ``muninn.paths.STATE_DIR`` at a tempdir. This is **Muninn's
    own** state directory, not the shared ravens directory — the descriptor goes
    in the latter (``raven.descriptor_path()``) and the two must not be confused;
    see raven.state_dir()'s docstring for what happens when they are.
    """
    return paths.STATE_DIR / "daemon.json"


def lock_path() -> Path:
    """The advisory lock file naming the one ingest loop allowed to run."""
    return paths.STATE_DIR / "daemon.lock"


# ── The state file ────────────────────────────────────────────────────────────

def write_state(port: int | None, *, path: Path | None = None,
                started: float | None = None, db_path: str | Path | None = None) -> Path:
    """Record pid/port/started/python/repo/db for a supervisor to read. 0600.

    **Field names mirror Huginn's ``daemon.json``** (``huginn/daemon.py``'s
    ``_write_daemon_state``) wherever they mean the same thing, so the two ravens
    are operationally similar and one script can read either: ``pid``, ``port``,
    ``started``, ``python``, ``repo``. ``db`` is Muninn's own addition, because
    the archive path is overridable per invocation (``--db``) and a supervisor
    that wants to know which archive a running daemon is feeding cannot otherwise
    tell.

    ``started`` is **epoch seconds, not an ISO string**, which is the opposite of
    how the rest of Muninn stores timestamps (``store.record_sweep``, the
    ledger). That is deliberate on both counts: it matches Huginn's field of the
    same name *and* the ``started`` in Muninn's own raven descriptor, and those
    two are the readers that exist. Rendering it for humans is ``doctor``'s job.

    ``port`` may be ``None``, and a reader that assumes otherwise is the mistake
    worth naming. Huginn's port is never ``None`` because its bind is mandatory
    and the daemon dies without it; Muninn's raven is best-effort by design
    (``ravenserve.attach`` returns ``None`` rather than costing the indexer its
    ingest), so "the daemon is running and there is no menu port" is a legitimate
    state that must still be discoverable.

    Written atomically with the mode set *before* the replace, the same ordering
    and for the same reason as ``raven.publish``: creating the final file and
    chmodding afterwards leaves a window in which it is world-readable. Not
    shared with that function because its payload and filename are the
    descriptor's, and a generic atomic-write helper does not belong in
    ``paths.py``, whose whole job is to name locations without performing I/O.
    Huginn's equivalent uses a plain ``write_text`` and chmods after, which has
    that window; this is the stricter of the two on purpose.
    """
    target = state_path() if path is None else Path(path)
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    # The archive itself is 0600 (store.py) but the directory holding it was
    # never restricted. This file names a loopback port that answers
    # unauthenticated requests, and ``python``/``repo`` are paths a tray app may
    # relaunch from — integrity matters even where confidentiality does not
    # (the reasoning Huginn recorded for its own 0600, finding M5 of the security
    # review of its issue #41 — not of #41's own scope, which was model policy).
    _restrict(directory, 0o700)

    payload = json.dumps({
        "pid": os.getpid(),
        "port": port,
        "started": time.time() if started is None else started,
        "python": sys.executable,
        # Same expression Huginn uses, and the same caveat: from a checkout this
        # is the repo root, from an installed wheel it is site-packages. It
        # answers "where do I relaunch this from", which is the question a tray
        # app asked by hardcoding one developer's path (Huginn issue #37).
        "repo": str(Path(__file__).resolve().parent.parent),
        "db": str(Path(paths.DB_PATH if db_path is None else db_path).expanduser()),
    }, indent=2, sort_keys=True) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".daemon.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def read_state(path: Path | None = None) -> dict[str, Any] | None:
    """Parse the state file, or ``None`` if it is absent or unreadable.

    "Absent" and "unreadable" are collapsed here on purpose — every caller wants
    "can I find a daemon", and neither answer is yes. ``doctor`` distinguishes
    them itself by checking existence first, because a file that is present and
    unparseable is a different problem from no daemon at all.
    """
    target = state_path() if path is None else Path(path)
    try:
        parsed = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def remove_state(path: Path | None = None) -> bool:
    """Remove the state file, but only if it names *this* process.

    The ownership check is Huginn's precedent and it guards a real sequence: a
    process that failed to take the single-instance lock, or one whose lock could
    not be taken at all on some platform, must never delete the live daemon's
    file on its way out. Returns whether anything was removed, which is what
    makes the guard testable rather than merely asserted.
    """
    target = state_path() if path is None else Path(path)
    state = read_state(target)
    if state is None or state.get("pid") != os.getpid():
        return False
    try:
        target.unlink(missing_ok=True)
    except OSError:
        return False
    return True


# ── The single-instance lock ──────────────────────────────────────────────────

#: Where the Windows lock sentinel byte lives. Any offset past the holder line
#: works; this one is far enough out that the line can never reach it.
_LOCK_SENTINEL_OFFSET = 1 << 30


def _try_lock(fd: int) -> bool | None:
    """Take an exclusive non-blocking lock. ``None`` = no primitive available.

    **``fcntl.flock``, never ``fcntl.lockf``**, and the difference is the whole
    reason this is a named function. POSIX record locks (``lockf``) are owned by
    the *process*, and closing **any** descriptor for the file releases every
    lock the process held on it — so :meth:`SingleInstance.probe`, which opens and
    closes its own descriptor, would silently release the real lock if it ever ran
    inside the daemon process (which it does: ``doctor`` can be run against a
    daemon, and a future in-process status check would too). ``flock`` locks belong
    to the open file description, so the probe can only ever affect its own.
    ``tests/test_daemon.py::test_probing_does_not_release_the_real_lock`` fails if
    this is ever swapped.

    A platform with neither primitive returns ``None`` and the caller **fails
    open** (allows the daemon to start). That direction is chosen deliberately:
    two ingest loops waste work and can clobber a descriptor, while a daemon
    that refuses to run at all loses transcripts, which is the failure this
    project exists to prevent.
    """
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    try:
        import msvcrt
    except ImportError:
        return None
    try:
        # A sentinel byte far past the holder line — NOT byte 0. Windows
        # byte-range locks are *mandatory*, not advisory: locking a byte the
        # file actually contains makes that byte unreadable to everyone else,
        # so locking byte 0 made `_read_lock_holder` fail with PermissionError
        # and every probe reported a running daemon as `(None, "unknown")`.
        # `doctor` then said "held by an unrecorded pid" about a healthy
        # daemon, and `muninn index --watch` could never be named as the
        # holder. Locking past EOF is supported and does not grow the file.
        os.lseek(fd, _LOCK_SENTINEL_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    finally:
        # acquire() writes the holder line through this same descriptor, so the
        # offset must not be left out at the sentinel.
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass
    return True


def _unlock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:
        pass
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return
    try:
        import msvcrt

        os.lseek(fd, _LOCK_SENTINEL_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except (ImportError, OSError):
        pass
    finally:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            pass


class SingleInstance:
    """The one ingest loop allowed to run, as an advisory whole-file lock.

    Use as a context manager, or call :meth:`acquire` and check the result. The
    lock is held for the lifetime of the open descriptor, so the kernel releases
    it even on ``SIGKILL`` — which is the property a pid file does not have and
    the reason this is a lock rather than a pid comparison.

    After acquiring, the holder's pid and a label from :data:`HOLDERS` are
    written into the file. That is not how the lock works; it is how ``doctor``
    can say *which* loop is running, which matters because
    ``muninn index --watch`` writes no state file and would otherwise be an
    anonymous refusal.
    """

    def __init__(self, path: Path | None = None, *, holder: str = HOLDER_SERVE) -> None:
        self.path = lock_path() if path is None else Path(path)
        self.holder = holder if holder in HOLDERS else HOLDER_SERVE
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True if this process now holds the lock (or locking is unsupported)."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _restrict(self.path.parent, 0o700)
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            # An unwritable state directory is already fatal to ingest for other
            # reasons (the archive lives there), so this is not the place to
            # decide the process cannot run. Fail open and let the store report.
            logger.warning("muninn daemon: cannot open the single-instance lock; not enforcing it")
            return True
        locked = _try_lock(fd)
        if locked is None:
            os.close(fd)
            logger.warning("muninn daemon: no file-locking primitive on this platform; "
                           "the single-instance guard is not enforced")
            return True
        if not locked:
            os.close(fd)
            return False
        self._fd = fd
        try:
            os.truncate(fd, 0)
            os.write(fd, f"{os.getpid()} {self.holder}\n".encode())
            os.fsync(fd)
        except OSError:
            # The label is a diagnostic, not the lock. Losing it costs doctor a
            # word, and must not cost the daemon its start.
            pass
        return True

    def release(self) -> None:
        """Release and close. Idempotent.

        The file is left behind rather than unlinked, which is the correct
        direction and easy to get wrong: unlinking it races a second process
        that has already opened the same path and locked it — that process would
        keep a lock on an unlinked inode while a third opens a fresh file and
        locks that, and then two daemons both believe they are the only one.
        """
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        _unlock(fd)
        try:
            os.close(fd)
        except OSError:
            pass

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    @classmethod
    def probe(cls, path: Path | None = None) -> tuple[bool | None, int | None, str]:
        """Answer "is it held, by whom" without taking it.

        Returns ``(held, pid, holder_label)``. ``held`` is ``None`` when the
        answer is genuinely unknown — no locking primitive, or a lock file this
        user cannot open. ``doctor`` prints that as unknown rather than as free,
        because reporting an unenforced guard as "nothing is running" is exactly
        the invisible-staleness failure this project keeps re-learning.
        """
        target = lock_path() if path is None else Path(path)
        if not target.exists():
            return False, None, "none"
        pid, label = _read_lock_holder(target)
        try:
            fd = os.open(target, os.O_RDWR)
        except OSError:
            return None, pid, label
        try:
            locked = _try_lock(fd)
            if locked is None:
                return None, pid, label
            if locked:
                _unlock(fd)
                return False, pid, label
            return True, pid, label
        finally:
            # Safe only because _try_lock uses flock and not lockf — see there.
            os.close(fd)


def _read_lock_holder(path: Path) -> tuple[int | None, str]:
    """Best-effort ``(pid, label)`` from the lock file's one short line.

    Bounded read, digits-only pid, and the label validated against
    :data:`HOLDERS` rather than echoed. The file is Muninn's own 0600 file, so
    this is not a trust boundary so much as a refusal to build one: a value read
    from a file and printed verbatim is how a diagnostic line becomes an output
    channel for whatever wrote there.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_LOCK_READ)
    except OSError:
        return None, "unknown"
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None, "unknown"
    first, _, rest = text.partition(" ")
    pid = int(first) if first.isdigit() else None
    label = rest.strip()
    return pid, label if label in HOLDERS else "unknown"


# ── Termination signals ───────────────────────────────────────────────────────

#: Set by the handler installed below, so a second signal arriving *during* the
#: teardown cannot abort it. Process-global because signal dispositions are; a
#: module-level flag is the honest shape rather than a wart to hide.
_terminating = False


def install_termination_handlers() -> tuple[str, ...]:
    """Make SIGTERM and SIGHUP unwind the stack. Returns the names installed.

    The failure this prevents, stated plainly because it does not look like a
    missing handler: Python's **default** ``SIGTERM`` disposition terminates the
    process without unwinding, so no ``finally:`` runs, and the raven descriptor
    survives naming a dead port while the state file survives naming a dead pid.
    A service manager's ordinary "stop" is therefore indistinguishable from a
    crash. Huginn shipped exactly this bug (its issue #43) and its Quit menu item
    was the trigger, so this was the *common* path, not an edge case.

    Raising ``SystemExit`` from the handler is what turns the signal into a
    normal unwind, so the daemon's ``finally`` gets to withdraw the descriptor,
    remove the state file, and release the lock.

    ``SIGINT`` is deliberately **not** installed: it already raises
    ``KeyboardInterrupt``, which unwinds. Replacing a working path with an
    untested one is the trade being declined.

    ## What ``watchfiles`` does with the same signal, and why it is not enough

    Measured directly, because the interaction is confusing enough that a reader
    could reasonably conclude this handler is redundant. ``watchfiles.watch`` is
    called with ``raise_interrupt=False``, and its Rust core notices *any*
    terminating signal — so on ``SIGTERM`` it logs the misleading line
    ``KeyboardInterrupt caught, stopping watch`` and makes its generator **return
    normally**. Observed order in a probe with both in play::

        PYTHON HANDLER RAN
        KeyboardInterrupt caught, stopping watch
        GENERATOR RETURNED without SystemExit

    Two things follow, and both matter:

    1. That log line comes from ``watchfiles`` and is **wrong about the signal** —
       nothing raised ``KeyboardInterrupt``. Do not read it as evidence that
       ``SIGINT`` arrived, and do not go looking in this repo for the code that
       printed it.
    2. The generator returning is *sufficient* only while ``watch()`` is blocked
       on it. It is **not** sufficient during the startup sweep, which is the
       longest and most important window there is: measured at ~45 seconds against
       the real 614-session corpus, entirely inside ``indexer.sweep()`` with no
       watchfiles generator alive to notice anything. A ``SIGTERM`` there would hit
       Python's default disposition and orphan both files. This handler is what
       covers it, and ``tests/test_daemon.py::test_sigterm_during_the_startup_sweep_still_tears_down``
       is the test that would fail if it were removed on the theory that
       ``watchfiles`` had it handled.

    The return value exists so a test can assert *which* signals were claimed —
    the number of handlers is not the guarantee, the identity of them is.
    Best-effort per signal: ``signal.signal`` only works on the main thread and
    a Windows build has no ``SIGHUP`` at all. A signal that cannot be claimed is
    logged, never fatal — the descriptor is merely left stale in that case, which
    the host already renders as "Not running" from its pid check.

    **The flag is cleared here**, and that is load-bearing rather than tidiness.
    ``_terminating`` means "a teardown is in progress, ignore further signals", and
    it is process-global. A menu-driven Restart stops the loop *with a SIGTERM*, so
    the flag is set by the time the next run begins — and without this reset the
    restarted daemon ignored every SIGTERM for the rest of its life, which is an
    unstoppable service that a supervisor can only SIGKILL. Installing handlers
    happens strictly before the teardown window opens, so there is no signal this
    reset could swallow.
    """
    global _terminating
    _terminating = False

    installed: list[str] = []
    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_terminating_signal)
        except (OSError, ValueError):
            logger.warning("muninn daemon: could not install a %s handler; "
                           "a stop may leave the descriptor behind", name)
            continue
        installed.append(name)
    return tuple(installed)


def _on_terminating_signal(signum: int, _frame: object) -> None:
    global _terminating
    try:
        name = signal.Signals(signum).name
    except ValueError:      # pragma: no cover - a signal number Python does not name
        name = str(signum)
    if _terminating:
        # A supervisor that escalates TERM, TERM, KILL must not have its second
        # TERM land in the middle of the teardown and orphan the very files the
        # first one is busy removing. SIGKILL still works, and the teardown is
        # milliseconds.
        logger.warning("muninn daemon: %s again while shutting down; ignoring", name)
        return
    _terminating = True
    logger.info("muninn daemon: %s received, shutting down", name)
    raise SystemExit(0)


def _reset_termination_state() -> None:
    """Test hook: forget that a signal was already handled.

    Needed because the flag is process-global and a suite raises real signals
    more than once in one interpreter.
    """
    global _terminating
    _terminating = False


# ── The daemon ────────────────────────────────────────────────────────────────

class AlreadyRunning(RuntimeError):
    """Raised when another ingest loop holds the single-instance lock.

    Carries the holder's pid and label so the CLI can name it. An "already
    running" message that cannot say *what* is running sends the reader to
    ``ps``, which is the sort of output CLAUDE.md's agent-facing contract calls
    an epistemic boundary failure: an agent relaying it can only guess.
    """

    def __init__(self, pid: int | None, holder: str) -> None:
        self.pid = pid
        self.holder = holder
        where = f"pid {pid}" if pid is not None else "an unknown pid"
        super().__init__(f"another muninn ingest loop is already running ({where}, {holder})")


class Daemon:
    """Owns the lifecycle: lock, ingest loop, raven surface, state file, teardown.

    Everything here is composition. The ingest loop is ``indexer.watch`` and the
    HTTP surface is ``ravenserve``; neither is reimplemented, and a change to
    either must not need a change here.
    """

    def __init__(self, db_path: str | Path, roots: dict[str, Path], *,
                 menubar: bool = True,
                 embed: bool = True,
                 enrich: bool = True,
                 enrich_metered: bool = False,
                 publish_state: bool = True,
                 holder: str = HOLDER_SERVE,
                 state_file: Path | None = None,
                 lock_file: Path | None = None,
                 announce: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.roots = roots
        self.menubar = menubar
        # Automatic embedding (spec 014). Default on, but gated on a provider
        # actually being installed — ``BackgroundEmbedder.start()`` returns False
        # on the default build, which ships none. So "on by default" costs a
        # default install nothing, and turns on exactly for the user who already
        # opted in by installing the [semantic] extra or a plugin.
        #
        # ``muninn index --watch`` passes False: it is the foreground/debug path
        # and publishes nothing, and a debug watcher that quietly starts spending
        # money against a hosted embedder would be a surprise in the one mode
        # someone runs to observe behaviour.
        self.embed = embed
        # Separate from ``embed`` on purpose: they are different amounts of money.
        # A user who wants automatic embedding (cents for a corpus) has not thereby
        # asked for automatic enrichment (the one expensive stage), so the flags do
        # not collapse into one.
        self.enrich = enrich
        self.enrich_metered = enrich_metered
        # ``muninn index --watch`` sets this False. A state file is a claim that
        # something can be supervised at that pid and port; a foreground watcher
        # publishes no port and expects no supervisor, and writing one anyway
        # would make `doctor` report a running daemon that a service manager
        # knows nothing about.
        self.publish_state = publish_state
        self.holder = holder if holder in HOLDERS else HOLDER_SERVE
        self.state_file = state_file
        self.lock_file = lock_file
        self.announce = announce or (lambda _msg: None)
        self.port: int | None = None
        self.embedder: embedder.BackgroundEmbedder | None = None
        self.enricher: enricher.BackgroundEnricher | None = None
        #: Set by :meth:`request_stop`. Read by the supervising loop in cli.py
        #: *after* ``run`` returns, which is why it outlives the ingest loop and
        #: why a restart gets a fresh ``Daemon`` rather than reusing this one.
        self.restart_requested = False
        #: True only between the start of the ingest loop and its unwind. A stop
        #: is refused outside that window rather than faked: the two callers that
        #: could hit it are a test-constructed daemon and a race with teardown,
        #: and "there is no loop to stop" is a fact to report.
        self._running = False

    def run(self, **watch_kwargs: Any) -> int:
        """Run until a signal or an exhausted event source. Returns an exit code.

        ``watch_kwargs`` are passed straight to ``indexer.watch`` so a test can
        inject a fake event source and bound the loop, exactly as spec 003's
        tests already do — the daemon adds no second set of knobs for the same
        thing.

        The ordering below is the part that is easy to get wrong, so each step
        says why it is where it is.
        """
        # 1. Signals first, before anything exists to be orphaned. Installing
        #    the handler *after* publishing would leave a window in which the
        #    ordinary stop signal does exactly the damage the handler exists to
        #    prevent — a window that is small, reachable (supervisors stop
        #    daemons during startup all the time), and completely silent.
        installed = install_termination_handlers()
        logger.debug("muninn daemon: termination handlers installed for %s", installed or "nothing")

        # 2. The lock, before the store is opened and before a single transcript
        #    is parsed. Refusing early is the point: the work a second loop does
        #    before discovering it is second is work done twice.
        lock = SingleInstance(self.lock_file, holder=self.holder)
        if not lock.acquire():
            _held, pid, holder = SingleInstance.probe(self.lock_file)
            raise AlreadyRunning(pid, holder)

        st = store.open_store(self.db_path)
        service: ravenserve.RavenService | None = None
        try:
            # 3. Bind and publish before the state file, so the port the state
            #    file reports is one that is already listening. attach()
            #    returns None rather than raising, by spec 009's rule that a
            #    menubar section must never cost the indexer its ingest.
            if self.menubar:
                # The action handler is bound to *this* daemon instance, which is
                # why a restart constructs a new one: a handler closed over a torn
                # down daemon would accept a click and stop nothing.
                service = ravenserve.attach(
                    self.db_path,
                    action_handler=lambda action_id: raven.perform_action(self, action_id),
                )
                if service is not None:
                    self.port = service.port
                    self.announce(f"muninn raven serving http://127.0.0.1:{service.port}/api/menu")
                else:
                    self.announce("muninn raven: not published (see `muninn doctor`)")

            # 4. The embedding worker, before the state file and before the
            #    ingest loop. Before the state file for the same reason the
            #    descriptor is: everything the daemon advertises should already
            #    be true when it becomes discoverable. Before the ingest loop
            #    because ``indexer.watch()`` never returns, so anything started
            #    after it is never started at all.
            #
            #    It gets its own connection and its own thread — see
            #    embedder.py's docstring for why borrowing either would put a
            #    provider round trip in front of ingest.
            if self.embed:
                worker = embedder.BackgroundEmbedder(self.db_path, announce=self.announce)
                if worker.start():
                    self.embedder = worker

            # The enricher after the embedder, and both before the ingest loop for
            # the same reason. Order between the two barely matters — they are
            # independent threads over independent backlogs — but embedding first
            # is the useful one: a session becomes semantically searchable in
            # seconds, and enrichment of the same session takes a model call.
            if self.enrich:
                facets = enricher.BackgroundEnricher(
                    self.db_path, allow_metered=self.enrich_metered,
                    announce=self.announce)
                if facets.start():
                    self.enricher = facets

            # 5. The state file last, so "the daemon is discoverable" implies
            #    everything it advertises is already true.
            if self.publish_state:
                written = write_state(self.port, path=self.state_file, db_path=self.db_path)
                self.announce(f"muninn daemon pid {os.getpid()} · state {written}")
            self.announce(f"muninn indexer watching {', '.join(str(p) for p in self.roots.values())}")

            self._running = True
            indexer.watch(st, self.roots, on_receipts=self._log_receipts, **watch_kwargs)
        except (KeyboardInterrupt, SystemExit):
            # SystemExit is ours: the termination handler raises it so this
            # unwind happens at all. Catching it here rather than letting it
            # propagate means `serve` returns an exit code like every other
            # subcommand instead of leaving argparse's caller to interpret one.
            pass
        finally:
            self._running = False
            # Teardown order, and each position is load-bearing:
            #   descriptor first — stop advertising a port before it dies;
            #   embedder next    — it holds its own connection to the archive,
            #                      and it must stop writing before the lock is
            #                      released to a successor daemon that will
            #                      immediately start its own worker;
            #   state file next  — a supervisor restarting us immediately must
            #                      not see a stale "running" state file while
            #                      the lock is already free, which is exactly
            #                      what `doctor` would read as a live daemon;
            #   store, then lock — the lock released last is what makes the
            #                      whole teardown atomic from outside.
            if service is not None:
                service.stop()
            # The enricher before the embedder, so the slowest worker gets its
            # timeout first rather than after the other has already spent one: both
            # hold their own connection to the archive and both must stop writing
            # before the lock is released to a successor daemon.
            if self.enricher is not None:
                self.enricher.stop()
            if self.embedder is not None:
                self.embedder.stop()
            if self.publish_state:
                remove_state(self.state_file)
            st.close()
            lock.release()
        return 0

    def request_stop(self, *, restart: bool = False) -> bool:
        """Accept a menu-driven Quit/Restart. True if it will happen.

        This only *records the intent*; :meth:`deliver_stop_signal` is what stops
        the loop, and the split is deliberate. The caller is an in-flight HTTP
        request from the menu-bar host, and the reply has to be written before the
        process starts unwinding — see ``raven.perform_action`` for why a quit that
        drops the connection reads to the host as a quit that failed.

        Returns False when there is no ingest loop to stop — a daemon constructed
        in a test, or one already tearing down — so the caller reports that rather
        than appearing to succeed.
        """
        if not self._running:
            return False
        self.restart_requested = bool(restart)
        self.announce(f"muninn: menu asked for {'restart' if restart else 'quit'}")
        return True

    def deliver_stop_signal(self) -> None:
        """Signal our own pid so the main thread unwinds. Called after the reply.

        ``SIGTERM`` rather than a bespoke shutdown flag, because the handler
        installed at the top of :meth:`run` already turns it into ``SystemExit``
        on the main thread, and that is the only thing that makes
        ``indexer.watch`` return and the ``finally`` above run. Adding a second
        shutdown path would mean two teardowns to keep correct, and the one that
        gets exercised less is the one that orphans the descriptor.

        Measured interaction worth naming: ``watchfiles``' Rust core notices any
        terminating signal and returns its generator normally, so the Python
        handler runs *and* the watch loop ends — see
        :func:`install_termination_handlers` for the observed ordering.

        Failure is logged, never raised: this runs on a request thread whose
        response is already on the wire, so an exception here has nowhere useful
        to go and would only be logged as a request failure with a misleading
        shape.

        ## Windows has no signal to deliver here

        ``os.kill`` on Windows is not a signal for anything but the two console
        CTRL events: for every other value it calls ``TerminateProcess``. So
        ``os.kill(os.getpid(), SIGTERM)`` did not ask this process to stop, it
        killed it outright -- no handler, no unwind, and none of the ``finally``
        above. The descriptor survived naming a dead pid and the state file with
        it, which is precisely the failure ``install_termination_handlers``
        exists to prevent, arriving by the one route that bypasses it.

        Worse for Restart specifically: there was no process left to come back,
        so the menu's Restart row was a Quit that also left litter. A host then
        showed "Not running (its recorded process is gone)" over a raven the
        user had just asked to restart.

        ``_thread.interrupt_main()`` is the Windows equivalent of what SIGTERM
        buys on POSIX: it raises ``KeyboardInterrupt`` on the main thread, which
        :meth:`run` already catches alongside ``SystemExit``. SIGINT is left
        uninstalled on purpose (see :func:`install_termination_handlers`), so
        nothing intercepts it. It lands at the next bytecode boundary -- during
        the startup sweep that is immediate, and inside ``watchfiles`` it is at
        most one ``rust_timeout`` tick, because the watcher is called with
        ``yield_on_timeout=True`` and so returns to Python every couple of
        seconds regardless of file activity.
        """
        if os.name == "nt":
            import _thread

            try:
                _thread.interrupt_main()
            except Exception:
                logger.warning("muninn: could not interrupt the main thread to stop")
            return
        try:
            os.kill(os.getpid(), signal.SIGTERM)
        except OSError:
            logger.warning("muninn: could not signal own process to stop")

    def _log_receipts(self, receipts: list[ImportReceipt]) -> None:
        for r in receipts:
            self.announce(f"import #{r.ledger_id} {r.outcome.value} "
                          f"added={r.delta.added} updated={r.delta.updated} "
                          f"skipped={r.delta.skipped}")
