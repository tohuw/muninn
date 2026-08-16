"""The daemon contract: state file, single instance, and shutdown (spec 010).

## Why so much of this file runs a real subprocess

The unit tests here would all pass with the daemon's most important call site
deleted. That is not a hypothetical: Huginn's first signal tests did exactly
that — they exercised its handler helper directly, so every one of them stayed
green with the call to it removed from ``run()``, which was the actual bug
(``huginn/tests/test_daemon_signals.py`` says so in its own docstring).

So the tests that matter here start a real ``muninn serve`` in a real
subprocess, in a real temporary HOME, and signal it. What they assert is the
*observable* contract an external supervisor depends on:

- after startup, a descriptor and a state file exist, at the right paths and
  modes, naming a port that answers;
- after ``SIGTERM``, both are gone and the process exited 0;
- after ``SIGHUP``, likewise, because a daemon whose terminal closes gets that
  one and orphans the same files;
- after ``SIGKILL``, both remain — which is correct, and is what the host's
  liveness check exists for.

:class:`WiringTest` closes the remaining gap: it asserts that ``Daemon.run``
installs the handlers *before* it starts the ingest loop. A live subprocess
cannot see ordering, and the ordering is the thing — a handler installed after
publishing leaves a window in which the ordinary stop signal does exactly the
damage the handler exists to prevent.

## The environment every test here must fake

``HOME``, ``XDG_STATE_HOME``, ``CODEX_HOME`` and ``RAVENS_STATE_DIR`` are all
redirected. A test that missed one of them would read the developer's real
``~/.claude``, ingest their real transcripts, or publish a descriptor naming a
dead port into their live menubar. ``muninn.paths`` computes ``STATE_DIR`` at
*import* time, so the subprocess tests set the environment before the child
starts rather than patching a module attribute — patching after import would not
move ``paths.DB_PATH``.
"""
from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from muninn import daemon, raven

POSIX_ONLY = unittest.skipIf(sys.platform == "win32",
                             "POSIX signals and mode bits; the tray owns lifecycle on Windows "
                             "(see WINDOWS.md)")

#: How long a child gets to publish its state file, and to tear it down. Real
#: sweeps of an empty corpus take milliseconds; this is generous so a loaded CI
#: machine is not a flaky failure, and every wait polls rather than sleeping the
#: whole budget.
STARTUP_TIMEOUT_S = 30.0
SHUTDOWN_TIMEOUT_S = 20.0


def _wait_for(predicate, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


# ── Unit-level: the state file ────────────────────────────────────────────────

class StateFileTest(unittest.TestCase):
    """The schema a supervisor reads, and the mode it is written at."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-daemon-"))
        self.path = self.tmp / "state" / "daemon.json"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fields_mirror_huginns_daemon_json(self) -> None:
        # The five shared names are the contract with Huginn's own daemon.json,
        # so one script can read either raven. Asserted as a set so an added
        # field (like Muninn's own "db") is fine and a *renamed* one is not.
        daemon.write_state(4321, path=self.path, db_path=self.tmp / "muninn.db")
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertLessEqual({"pid", "port", "started", "python", "repo"}, set(state))
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["port"], 4321)
        self.assertEqual(state["python"], sys.executable)
        self.assertEqual(state["db"], str(self.tmp / "muninn.db"))

    def test_started_is_epoch_seconds_not_an_iso_string(self) -> None:
        # Matches Huginn's field of the same name and the raven descriptor's,
        # which are the two readers that exist. An ISO string here would be
        # consistent with the rest of Muninn's storage and wrong for both of them.
        daemon.write_state(1, path=self.path)
        started = json.loads(self.path.read_text(encoding="utf-8"))["started"]
        self.assertIsInstance(started, float)
        self.assertLess(abs(started - time.time()), 120)

    def test_repo_points_at_something_importable(self) -> None:
        # A tray app relaunches from "python" + "repo"; a repo path that does not
        # contain the package is how Huginn's menubar came to hardcode one
        # developer's checkout (its issue #37).
        daemon.write_state(1, path=self.path)
        repo = Path(json.loads(self.path.read_text(encoding="utf-8"))["repo"])
        self.assertTrue((repo / "muninn" / "daemon.py").is_file(), repo)

    def test_a_port_of_none_is_a_legitimate_state(self) -> None:
        # ravenserve.attach() returns None rather than costing the daemon its
        # ingest (spec 009 #9), so "running, no menu port" must be recordable. A
        # reader that assumes an int here is the mistake; the field is present
        # and null rather than absent, so the distinction is explicit.
        daemon.write_state(None, path=self.path)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("port", state)
        self.assertIsNone(state["port"])

    @POSIX_ONLY
    def test_mode_is_owner_only_even_under_a_permissive_umask(self) -> None:
        # The mode is set on the temp file *before* the replace, so there is no
        # window in which the final path is world-readable. A umask of 0 is what
        # makes that ordering observable rather than merely claimed.
        prior = os.umask(0)
        try:
            daemon.write_state(1, path=self.path)
        finally:
            os.umask(prior)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)

    def test_write_leaves_no_temp_file_behind(self) -> None:
        daemon.write_state(1, path=self.path)
        strays = [p.name for p in self.path.parent.iterdir() if p.name != self.path.name]
        self.assertEqual(strays, [])

    def test_read_state_returns_none_for_absent_and_for_garbage(self) -> None:
        self.assertIsNone(daemon.read_state(self.path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(daemon.read_state(self.path))
        # A JSON scalar parses but is not a state file. Returning it would hand
        # every caller a .get() on a str.
        self.path.write_text('"nope"', encoding="utf-8")
        self.assertIsNone(daemon.read_state(self.path))

    def test_remove_state_refuses_to_delete_another_processs_file(self) -> None:
        # The guard that stops a process which lost the lock from deleting the
        # live daemon's state file on its way out (Huginn's issue #40 shape).
        daemon.write_state(1, path=self.path)
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["pid"] = os.getpid() + 999_999
        self.path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(daemon.remove_state(self.path))
        self.assertTrue(self.path.exists())

    def test_remove_state_deletes_our_own(self) -> None:
        daemon.write_state(1, path=self.path)
        self.assertTrue(daemon.remove_state(self.path))
        self.assertFalse(self.path.exists())
        # Idempotent: a second teardown pass must not raise.
        self.assertFalse(daemon.remove_state(self.path))


# ── Unit-level: the single-instance lock ──────────────────────────────────────

class SingleInstanceTest(unittest.TestCase):
    """One ingest loop, whichever command started it."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-lock-"))
        self.path = self.tmp / "state" / "daemon.lock"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_second_holder_is_refused(self) -> None:
        first = daemon.SingleInstance(self.path, holder=daemon.HOLDER_SERVE)
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        second = daemon.SingleInstance(self.path, holder=daemon.HOLDER_WATCH)
        self.assertFalse(second.acquire())

    def test_release_lets_the_next_one_in(self) -> None:
        first = daemon.SingleInstance(self.path)
        self.assertTrue(first.acquire())
        first.release()
        second = daemon.SingleInstance(self.path)
        self.assertTrue(second.acquire())
        second.release()

    def test_acquire_is_idempotent_for_the_holder(self) -> None:
        lock = daemon.SingleInstance(self.path)
        self.addCleanup(lock.release)
        self.assertTrue(lock.acquire())
        self.assertTrue(lock.acquire())

    def test_release_is_idempotent(self) -> None:
        lock = daemon.SingleInstance(self.path)
        lock.acquire()
        lock.release()
        lock.release()

    def test_the_lock_file_survives_release(self) -> None:
        # Unlinking it races a process that already opened the same path: that
        # one would hold a lock on an unlinked inode while a third locks a fresh
        # file, and then two daemons each believe they are alone.
        lock = daemon.SingleInstance(self.path)
        lock.acquire()
        lock.release()
        self.assertTrue(self.path.exists())

    def test_probe_names_the_holder_without_taking_the_lock(self) -> None:
        lock = daemon.SingleInstance(self.path, holder=daemon.HOLDER_WATCH)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        held, pid, holder = daemon.SingleInstance.probe(self.path)
        self.assertTrue(held)
        self.assertEqual(pid, os.getpid())
        self.assertEqual(holder, daemon.HOLDER_WATCH)

    def test_probing_does_not_release_the_real_lock(self) -> None:
        """The reason ``_try_lock`` uses ``flock`` and never ``lockf``.

        POSIX record locks are owned by the *process*, and closing any descriptor
        for the file drops every lock the process holds on it — so a probe running
        inside the daemon would silently unlock the daemon. ``flock`` locks belong
        to the open file description, so the probe can only affect its own. This
        test fails loudly if that is ever swapped.
        """
        lock = daemon.SingleInstance(self.path)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        for _ in range(3):
            daemon.SingleInstance.probe(self.path)
        other = daemon.SingleInstance(self.path)
        self.assertFalse(other.acquire(), "the probe released the lock it only meant to read")

    def test_probe_reports_free_when_nothing_holds_it(self) -> None:
        # Both shapes of "free": no lock file at all, and a lock file left behind
        # by a released holder. release() deliberately does not unlink it (see
        # test_the_lock_file_survives_release), so the second case is the common
        # one in practice and would be the easier of the two to get wrong.
        self.assertFalse(daemon.SingleInstance.probe(self.path)[0])
        lock = daemon.SingleInstance(self.path)
        lock.acquire()
        lock.release()
        self.assertFalse(daemon.SingleInstance.probe(self.path)[0],
                         "a released lock must read as free, not as held")

    def test_a_holder_label_is_never_echoed_verbatim(self) -> None:
        # The label reaches a `doctor` line. A closed vocabulary means whatever
        # else is in the file reports as "unknown" rather than becoming output.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("4242 \x1b[31mPWNED\x1b[0m\n", encoding="utf-8")
        _held, pid, holder = daemon.SingleInstance.probe(self.path)
        self.assertEqual(pid, 4242)
        self.assertEqual(holder, "unknown")

    def test_an_oversized_lock_file_is_read_boundedly(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("9" * 100_000, encoding="utf-8")
        _held, pid, holder = daemon.SingleInstance.probe(self.path)
        # Truncated digits still parse as *a* number; the point is that the read
        # was bounded and the label did not become 100 KB of output.
        self.assertIsInstance(pid, int)
        self.assertEqual(holder, "unknown")

    def test_a_bad_holder_label_falls_back_rather_than_being_written(self) -> None:
        lock = daemon.SingleInstance(self.path, holder="not-a-known-holder")
        self.assertEqual(lock.holder, daemon.HOLDER_SERVE)


# ── Unit-level: the signal handlers ───────────────────────────────────────────

@POSIX_ONLY
class TerminationHandlerTest(unittest.TestCase):
    """SIGTERM and SIGHUP must unwind; SIGINT must be left alone."""

    def setUp(self) -> None:
        self.prior = {
            name: signal.getsignal(getattr(signal, name))
            for name in ("SIGTERM", "SIGHUP", "SIGINT")
            if getattr(signal, name, None) is not None
        }
        daemon._reset_termination_state()

    def tearDown(self) -> None:
        for name, handler in self.prior.items():
            signal.signal(getattr(signal, name), handler)
        daemon._reset_termination_state()

    def test_installing_clears_the_in_teardown_flag(self) -> None:
        """A restarted daemon must still be stoppable.

        ``_terminating`` is process-global and says "a teardown is in progress,
        ignore further signals". A menu-driven Restart stops the loop *with* a
        SIGTERM, so the flag is set when the next run starts — and while it was
        sticky, the restarted daemon ignored every SIGTERM for the rest of its
        life. That is a service only SIGKILL can stop, and nothing about it is
        visible short of restarting and then trying to stop.
        """
        daemon.install_termination_handlers()
        with self.assertRaises(SystemExit):
            daemon._on_terminating_signal(signal.SIGTERM, None)
        # Second signal ignored: that part is deliberate, and is what makes the
        # flag sticky within one teardown.
        daemon._on_terminating_signal(signal.SIGTERM, None)

        daemon.install_termination_handlers()        # the restarted run
        with self.assertRaises(SystemExit):
            daemon._on_terminating_signal(signal.SIGTERM, None)

    def test_it_claims_sigterm_and_sighup_and_says_which(self) -> None:
        # The identity of the signals is the guarantee, not the count — which is
        # why install_termination_handlers returns names at all.
        installed = daemon.install_termination_handlers()
        self.assertIn("SIGTERM", installed)
        self.assertIn("SIGHUP", installed)

    def test_sigterm_raises_systemexit_so_a_finally_runs(self) -> None:
        """The whole point: the default disposition does NOT unwind.

        Written as "did the finally run", not "was a handler installed",
        because a handler that fails to unwind is the exact bug (Huginn #43) and
        it looks identical from the outside to a correct one.
        """
        daemon.install_termination_handlers()
        cleaned: list[str] = []
        with self.assertRaises(SystemExit):
            try:
                signal.raise_signal(signal.SIGTERM)
            finally:
                cleaned.append("teardown ran")
        self.assertEqual(cleaned, ["teardown ran"])

    def test_sighup_also_unwinds(self) -> None:
        daemon.install_termination_handlers()
        cleaned: list[str] = []
        with self.assertRaises(SystemExit):
            try:
                signal.raise_signal(signal.SIGHUP)
            finally:
                cleaned.append("teardown ran")
        self.assertEqual(cleaned, ["teardown ran"])

    def test_sigint_is_deliberately_not_claimed(self) -> None:
        # It already raises KeyboardInterrupt, which unwinds. Claiming it would
        # swap a working path for an untested one.
        before = signal.getsignal(signal.SIGINT)
        daemon.install_termination_handlers()
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_a_second_signal_during_teardown_is_ignored(self) -> None:
        # A supervisor that escalates TERM, TERM, KILL must not have its second
        # TERM abort the teardown that is busy removing the files.
        daemon.install_termination_handlers()
        with self.assertRaises(SystemExit):
            signal.raise_signal(signal.SIGTERM)
        # Still inside the notional teardown: a second one must return, not raise.
        signal.raise_signal(signal.SIGTERM)


class StopSignalDeliveryTest(unittest.TestCase):
    """How the menu's Quit and Restart reach the main thread, per platform.

    Both branches are exercised on every OS by faking ``os.name``: the whole
    point is that the Windows path was wrong and nobody running POSIX could see
    it.
    """

    def _daemon(self):
        service = daemon.Daemon(":memory:", {})
        service._running = True
        return service

    def test_windows_interrupts_the_main_thread_and_never_calls_os_kill(self) -> None:
        """os.kill on Windows is TerminateProcess, not a signal.

        It killed the process outright: no handler, no unwind, so the descriptor
        and the state file both survived naming a dead pid -- the exact failure
        install_termination_handlers exists to prevent, arriving by the one route
        that bypasses it. A Restart could not come back, because nothing was left
        to come back.
        """
        import _thread
        from unittest.mock import patch

        service = self._daemon()
        with patch.object(daemon.os, "name", "nt"), \
             patch.object(daemon.os, "kill") as killed, \
             patch.object(_thread, "interrupt_main") as interrupted:
            service.deliver_stop_signal()
        interrupted.assert_called_once_with()
        killed.assert_not_called()

    def test_posix_still_sends_itself_sigterm(self) -> None:
        from unittest.mock import patch

        service = self._daemon()
        with patch.object(daemon.os, "name", "posix"), \
             patch.object(daemon.os, "kill") as killed:
            service.deliver_stop_signal()
        killed.assert_called_once_with(os.getpid(), signal.SIGTERM)

    def test_a_failure_to_deliver_is_logged_not_raised(self) -> None:
        """This runs on a request thread whose response is already on the wire."""
        import _thread
        from unittest.mock import patch

        service = self._daemon()
        with patch.object(daemon.os, "name", "nt"), \
             patch.object(_thread, "interrupt_main", side_effect=RuntimeError("no")):
            service.deliver_stop_signal()   # must not raise


@POSIX_ONLY
class WiringTest(unittest.TestCase):
    """The handler existing is not the guarantee — ``run()`` installing it is.

    Huginn's cautionary precedent, quoted because it is the reason this class
    exists: its first signal tests "all pass with the call site in ``run()``
    deleted, because they exercise the helper directly." So this asserts the
    wiring by two independent means — a source-order check, and a live daemon
    whose handler must actually be in place while the ingest loop runs.
    """

    def test_run_installs_handlers_before_the_ingest_loop(self) -> None:
        import inspect

        source = inspect.getsource(daemon.Daemon.run)
        install_at = source.find("install_termination_handlers(")
        watch_at = source.find("indexer.watch(")
        self.assertNotEqual(install_at, -1,
                            "Daemon.run must install the termination handlers")
        self.assertNotEqual(watch_at, -1, "Daemon.run must still run indexer.watch()")
        self.assertLess(install_at, watch_at,
                        "handlers must be installed before the loop that they interrupt")

    def test_run_installs_them_before_publishing_anything(self) -> None:
        # A handler installed after the descriptor exists leaves a window in
        # which the ordinary stop signal orphans it. Small, reachable, silent.
        import inspect

        source = inspect.getsource(daemon.Daemon.run)
        install_at = source.find("install_termination_handlers(")
        attach_at = source.find("ravenserve.attach(")
        state_at = source.find("write_state(")
        self.assertLess(install_at, attach_at)
        self.assertLess(install_at, state_at)

    def test_the_handler_is_actually_in_place_inside_a_running_daemon(self) -> None:
        """Source order is necessary but not sufficient; observe it installed.

        This is what fails if ``install_termination_handlers`` is ever called but
        cannot take effect — the case a source-order assertion cannot see. The
        ingest loop is bounded with a fake event source (spec 003's own test
        seam) so nothing here waits on a real filesystem event.
        """
        import shutil

        tmp = Path(tempfile.mkdtemp(prefix="muninn-wiring-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        prior = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, prior)
        self.addCleanup(daemon._reset_termination_state)
        # A real root, because indexer.watch() only consults watch_fn when one
        # configured root exists on disk. With none it sleeps, the probe never
        # runs, and the assertion below would pass on an empty list.
        root = tmp / "claude"
        root.mkdir()

        observed: list[object] = []

        def fake_watch(_paths, **_kw):
            observed.append(signal.getsignal(signal.SIGTERM))
            return iter(())

        service = daemon.Daemon(
            tmp / "muninn.db", {"claude": root},
            menubar=False,
            state_file=tmp / "daemon.json",
            lock_file=tmp / "daemon.lock",
        )
        service.run(watch_fn=fake_watch, max_iterations=1)
        self.assertEqual(len(observed), 1, "the ingest loop never ran")
        self.assertIsNot(observed[0], signal.SIG_DFL,
                         "SIGTERM was still at its default while the loop ran — a stop "
                         "would not have unwound, and nothing else here would notice")


# ── Unit-level: what the daemon owns, and what index --watch does not ─────────

class OwnershipTest(unittest.TestCase):
    """The split between ``serve`` and ``index --watch``."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-own-"))
        self.state = self.tmp / "daemon.json"
        self.lock = self.tmp / "daemon.lock"
        # A real (empty) root. ``indexer.watch`` only calls ``watch_fn`` when at
        # least one configured root is an existing directory — with none it
        # sleeps instead, and a probe that never fires would make every
        # assertion below trivially pass on an empty list.
        self.root = self.tmp / "claude"
        self.root.mkdir()
        self.roots = {"claude": self.root}

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _daemon(self, **kwargs) -> daemon.Daemon:
        kwargs.setdefault("menubar", False)
        return daemon.Daemon(self.tmp / "muninn.db", self.roots,
                             state_file=self.state, lock_file=self.lock, **kwargs)

    def _run_probing(self, probe, **kwargs) -> list:
        """Run one bounded iteration, calling ``probe()`` from inside the loop.

        Asserting from *inside* is the only way to see the state that exists
        while the daemon runs; checking after ``run()`` returns can only ever
        observe the teardown.
        """
        seen: list = []

        def watching(_paths, **_kw):
            seen.append(probe())
            return iter(())

        self._daemon(**kwargs).run(watch_fn=watching, max_iterations=1)
        self.assertEqual(len(seen), 1, "the ingest loop never reached the event source")
        return seen

    def _run(self, **kwargs) -> None:
        self._daemon(**kwargs).run(watch_fn=lambda _p, **_k: iter(()), max_iterations=1)

    def test_serve_writes_a_state_file_and_removes_it(self) -> None:
        seen = self._run_probing(self.state.exists)
        self.assertEqual(seen, [True], "the state file must exist while the loop runs")
        self.assertFalse(self.state.exists(), "and must be gone after it stops")

    def test_index_watch_writes_no_state_file(self) -> None:
        seen = self._run_probing(self.state.exists, publish_state=False,
                                 holder=daemon.HOLDER_WATCH)
        self.assertEqual(seen, [False],
                         "a foreground watcher must not claim to be supervisable")

    def test_index_watch_still_holds_the_lock_under_its_own_label(self) -> None:
        seen = self._run_probing(lambda: daemon.SingleInstance.probe(self.lock)[2],
                                 publish_state=False, holder=daemon.HOLDER_WATCH)
        self.assertEqual(seen, [daemon.HOLDER_WATCH])

    def test_serve_holds_the_lock_under_its_own_label(self) -> None:
        seen = self._run_probing(lambda: daemon.SingleInstance.probe(self.lock)[2])
        self.assertEqual(seen, [daemon.HOLDER_SERVE])

    def test_a_second_loop_raises_alreadyrunning_naming_the_holder(self) -> None:
        held = daemon.SingleInstance(self.lock, holder=daemon.HOLDER_SERVE)
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        with self.assertRaises(daemon.AlreadyRunning) as caught:
            self._run()
        self.assertEqual(caught.exception.pid, os.getpid())
        self.assertEqual(caught.exception.holder, daemon.HOLDER_SERVE)

    def test_a_refused_second_loop_never_opens_the_archive(self) -> None:
        # Refusing before the store is opened is the point: the work a second
        # loop does before finding out it is second is work done twice.
        held = daemon.SingleInstance(self.lock)
        self.assertTrue(held.acquire())
        self.addCleanup(held.release)
        with self.assertRaises(daemon.AlreadyRunning):
            self._run()
        self.assertFalse((self.tmp / "muninn.db").exists())

    def test_the_lock_is_released_when_the_loop_ends(self) -> None:
        self._run()
        self.assertFalse(daemon.SingleInstance.probe(self.lock)[0])

    def test_the_sweep_still_runs_before_the_first_event_wait(self) -> None:
        """The daemon must not have broken indexer.watch()'s startup sweep.

        Restated here rather than left to tests/test_indexer.py because the
        daemon is now what runs the loop, and "the daemon skipped the sweep" is a
        silent data-loss path: events during downtime were missed by every
        watcher and only a sweep recovers them.
        """
        from muninn import indexer

        order: list[str] = []
        real_sweep = indexer.sweep

        def spy(*args, **kwargs):
            order.append("sweep")
            return real_sweep(*args, **kwargs)

        indexer.sweep = spy
        self.addCleanup(setattr, indexer, "sweep", real_sweep)

        def watching(_paths, **_kw):
            order.append("watch")
            return iter(())

        self._daemon().run(watch_fn=watching, max_iterations=1)
        self.assertEqual(order[:2], ["sweep", "watch"])


# ── What `doctor` says about it ───────────────────────────────────────────────

class DoctorRenderingTest(unittest.TestCase):
    """`doctor`'s daemon section renders the state file; it never echoes it.

    The file is Muninn's own 0600 one, so this is not a trust boundary so much as
    a refusal to build one: `doctor`'s output is what an agent relays to a human
    (CLAUDE.md, "The agent-facing contract"), and a field printed verbatim makes
    whatever wrote that file an author of that output.
    """

    def setUp(self) -> None:
        from muninn import cli, paths

        self.cli = cli
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-doctor-"))
        self.prior = paths.STATE_DIR
        paths.STATE_DIR = self.tmp
        self.addCleanup(setattr, paths, "STATE_DIR", self.prior)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(self) -> str:
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.cli._print_daemon_section()
        return buffer.getvalue()

    def test_a_hostile_db_path_cannot_put_an_escape_on_screen(self) -> None:
        # An ANSI escape here could rewrite the line above it in a terminal.
        (self.tmp / "daemon.json").write_text(json.dumps({
            "pid": os.getpid(), "port": 1234, "started": time.time(),
            "db": "/tmp/\x1b[2Kfake\x07 evil\n/second-line",
        }), encoding="utf-8")
        report = self._report()
        self.assertNotIn("\x1b", report)
        self.assertNotIn("\x07", report)
        self.assertIn("fake evil /second-line", report)

    def test_a_non_integer_pid_reads_as_not_running(self) -> None:
        # "pid": "1" would pass a truthiness check and crash pid_alive.
        (self.tmp / "daemon.json").write_text(
            json.dumps({"pid": "1", "port": 1, "started": 0.0}), encoding="utf-8")
        report = self._report()
        self.assertIn("stale", report.lower())
        self.assertNotIn("  running     pid", report)

    def test_a_live_daemon_with_no_port_is_reported_as_running(self) -> None:
        (self.tmp / "daemon.json").write_text(json.dumps({
            "pid": os.getpid(), "port": None, "started": time.time(),
            "db": str(self.tmp / "muninn.db"),
        }), encoding="utf-8")
        report = self._report()
        self.assertIn(f"running     pid {os.getpid()}", report)
        self.assertIn("none", report)

    def test_an_unusable_started_is_admitted_not_printed_raw(self) -> None:
        for bad in ("yesterday", None, True, 10.0 ** 30):
            with self.subTest(started=bad):
                rendered = self.cli._epoch_to_iso(bad)
                self.assertIn("unrecorded" if not isinstance(bad, float) else "unusable",
                              rendered)

    def test_absent_state_points_at_the_command_that_creates_it(self) -> None:
        self.assertIn("muninn serve", self._report())

    def test_unreadable_state_is_not_reported_as_running(self) -> None:
        (self.tmp / "daemon.json").write_text("{ broken", encoding="utf-8")
        report = self._report()
        self.assertIn("unreadable", report)
        self.assertNotIn("  running     pid", report)


# ── The real thing: a live daemon, signalled ──────────────────────────────────

_CHILD = textwrap.dedent(
    """
    import sys
    sys.argv = ["muninn", "serve"]
    from muninn.cli import main
    sys.exit(main())
    """
)


@POSIX_ONLY
class LiveLifecycleTest(unittest.TestCase):
    """Start a real ``muninn serve``, signal it, and check what is left behind.

    Everything in this class is deliberately black-box. It imports nothing from
    the daemon module and asserts only on files, ports and exit codes — which is
    exactly what a launchd job or a systemd unit can see, and therefore the only
    level at which "the teardown is reliable" is a real claim.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-live-"))
        self.home = self.tmp / "home"
        self.ravens = self.tmp / "ravens"
        # Every one of these matters. Missing HOME would read the developer's
        # real ~/.claude; missing RAVENS_STATE_DIR would publish a descriptor
        # naming a dead port into their live menubar.
        (self.home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.home),
            "XDG_STATE_HOME": str(self.tmp / "xdg"),
            "CODEX_HOME": str(self.tmp / "codex"),
            "RAVENS_STATE_DIR": str(self.ravens),
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        })
        self.state = self.tmp / "xdg" / "muninn" / "daemon.json"
        self.descriptor = self.ravens / f"{raven.NAME}.json"
        self.proc: subprocess.Popen | None = None

    def tearDown(self) -> None:
        import shutil

        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _child_output(self) -> str:
        """Whatever the child said, without blocking if it is still running.

        The pipe is never drained during a normal run, so on a timeout the
        reason the daemon did not start was being thrown away -- the failure
        read only "timed out", which is why this went undiagnosed. Killing it
        first closes the write end so the read cannot hang.
        """
        if self.proc is None:
            return "(no child was started)"
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            out, _ = self.proc.communicate(timeout=10)
        except Exception as exc:  # pragma: no cover - diagnostics must not mask
            return f"(could not read child output: {exc!r})"
        return out or "(the child wrote nothing)"

    def _start(self) -> subprocess.Popen:
        # start_new_session so the child leads its own process group: without it
        # a signal aimed at this test's group would hit pytest too, and the child
        # would inherit whatever SIGINT disposition the harness has — which is
        # SIG_IGN under some shells, so `test_sigint_leaves_nothing_behind` would
        # hang rather than fail, and for a reason that has nothing to do with the
        # daemon.
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        # Waiting for the file to *exist* is not enough after a crash: the dead
        # daemon's own state file is still sitting there, so the wait returns
        # instantly and every later assertion reads the corpse's pid. Wait for the
        # file to name *this* child.
        try:
            _wait_for(
                lambda: (daemon.read_state(self.state) or {}).get("pid") == self.proc.pid,
                STARTUP_TIMEOUT_S, f"a state file naming pid {self.proc.pid}")
        except AssertionError as exc:
            exited = self.proc.poll()
            raise AssertionError(
                f"{exc}\n"
                f"child exit status: {exited!r} "
                f"({'still running' if exited is None else 'exited'})\n"
                f"--- child output ---\n{self._child_output()}"
            ) from None
        return self.proc

    def _stop(self, sig: int) -> int:
        assert self.proc is not None
        self.proc.send_signal(sig)
        try:
            return self.proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a hung teardown
            self.proc.kill()
            raise AssertionError(f"the daemon did not exit on {signal.Signals(sig).name}")

    def test_startup_publishes_both_files_at_owner_only_modes(self) -> None:
        self._start()
        self.assertTrue(self.descriptor.exists(), "no raven descriptor was published")
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.state.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.descriptor.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.descriptor.parent.stat().st_mode), 0o700)

    def test_the_state_file_and_the_descriptor_agree(self) -> None:
        # Two files, one running process. A supervisor reading one and the host
        # reading the other must not learn different things.
        self._start()
        state = json.loads(self.state.read_text(encoding="utf-8"))
        published = json.loads(self.descriptor.read_text(encoding="utf-8"))
        self.assertEqual(state["pid"], self.proc.pid)
        self.assertEqual(state["pid"], published["pid"])
        self.assertEqual(state["port"], published["port"])

    def test_the_advertised_port_answers_the_menu(self) -> None:
        self._start()
        port = json.loads(self.state.read_text(encoding="utf-8"))["port"]
        self.assertIsNotNone(port, "the daemon published no menu port")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/menu", headers={"Host": f"127.0.0.1:{port}"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["title"], raven.DISPLAY)
        self.assertIn("sections", payload)

    def test_sigterm_leaves_nothing_behind(self) -> None:
        """The headline guarantee, and the one Huginn shipped broken (#43).

        This fails if the daemon stops installing its SIGTERM handler, because
        Python's default disposition kills the process without unwinding and
        both files survive. No amount of testing the handler in isolation catches
        that; only signalling a real process does.
        """
        self._start()
        self.assertEqual(self._stop(signal.SIGTERM), 0)
        self.assertFalse(self.descriptor.exists(),
                         "SIGTERM orphaned the raven descriptor — it now names a dead port")
        self.assertFalse(self.state.exists(),
                         "SIGTERM orphaned the state file — doctor will report a dead daemon "
                         "as running")

    def _post_action(self, action_id: str) -> dict:
        port = json.loads(self.state.read_text(encoding="utf-8"))["port"]
        self.assertIsNotNone(port, "the daemon published no menu port")
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{raven.ACTION_ENDPOINT}",
            data=json.dumps({"id": action_id}).encode("utf-8"),
            headers={"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_the_menu_quit_row_tears_down_as_cleanly_as_sigterm(self) -> None:
        """The reason Quit exists as a menu row at all, tested end to end.

        A menu-driven quit that orphaned the descriptor would be strictly worse
        than having no row: the user clicks Quit, the process dies, and the host
        then reports Muninn as "not answering on its recorded port" forever. That
        is Huginn's issue #43 exactly, and the only way to know Muninn does not
        have it is to click the row on a real process.
        """
        self._start()
        self.assertTrue(self._post_action(raven.QUIT)["ok"])
        # The reply arrived before the process went away, which is the ordering
        # the deferred followup exists to guarantee — urlopen above would have
        # raised on a dropped connection.
        self.assertEqual(self.proc.wait(timeout=SHUTDOWN_TIMEOUT_S), 0)
        self.assertFalse(self.descriptor.exists(),
                         "the menu's Quit row orphaned the raven descriptor")
        self.assertFalse(self.state.exists(),
                         "the menu's Quit row orphaned the state file")

    def test_the_menu_restart_row_comes_back_on_a_fresh_port(self) -> None:
        """A restart has to look like a restart to everything watching.

        Same pid — it restarts in this process rather than re-execing, so there is
        no argv to build and no interpreter path to write and then run. A *new*
        port and a republished descriptor, because the old listener was closed by
        the teardown; a restart that silently kept serving the old socket would
        mean the teardown did not actually run.
        """
        self._start()
        first = json.loads(self.state.read_text(encoding="utf-8"))["port"]
        self.assertTrue(self._post_action(raven.RESTART)["ok"])

        _wait_for(lambda: self.state.exists()
                  and (json.loads(self.state.read_text(encoding="utf-8")).get("port")
                       not in (None, first)),
                  STARTUP_TIMEOUT_S, "a state file naming a new port after the restart")
        second = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(second["pid"], self.proc.pid, "the restart forked or re-execed")
        self.assertNotEqual(second["port"], first)
        self.assertTrue(self.descriptor.exists(), "the restart did not republish the descriptor")
        self.assertEqual(json.loads(self.descriptor.read_text(encoding="utf-8"))["port"],
                         second["port"], "the descriptor still names the pre-restart port")
        self.assertIsNone(self.proc.poll(), "the restart exited instead of coming back")

        # And it is still a daemon that stops cleanly afterwards — a restarted
        # loop that lost its signal handler would fail here and nowhere else.
        self.assertEqual(self._stop(signal.SIGTERM), 0)
        self.assertFalse(self.descriptor.exists())
        self.assertFalse(self.state.exists())

    def test_sigterm_during_the_startup_sweep_still_tears_down(self) -> None:
        """The window `watchfiles` cannot cover, and the reason the handler exists.

        ``watchfiles.watch`` notices a terminating signal itself and returns from
        its generator (logging a misleading "KeyboardInterrupt caught" — see
        ``daemon.install_termination_handlers``). That makes the handler look
        redundant, and during the *sweep* it is not: no generator exists yet, so a
        SIGTERM there meets Python's default disposition and orphans both files.
        The sweep is also the longest window — measured ~45s against the real
        614-session corpus.

        This test signals as soon as the descriptor appears, which the daemon
        publishes *before* ``indexer.watch`` is called, so the signal lands with the
        sweep in progress and no event source alive.
        """
        self.proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        _wait_for(self.descriptor.exists, STARTUP_TIMEOUT_S,
                  "the descriptor, which is published before the sweep begins")
        self.assertEqual(self._stop(signal.SIGTERM), 0)
        self.assertFalse(self.descriptor.exists(),
                         "a SIGTERM during the startup sweep orphaned the descriptor")
        self.assertFalse(self.state.exists(),
                         "a SIGTERM during the startup sweep orphaned the state file")

    def test_sighup_leaves_nothing_behind(self) -> None:
        # A daemon started from a terminal that then closes gets SIGHUP and
        # orphans exactly the same two files.
        self._start()
        self.assertEqual(self._stop(signal.SIGHUP), 0)
        self.assertFalse(self.descriptor.exists())
        self.assertFalse(self.state.exists())

    def test_sigint_leaves_nothing_behind(self) -> None:
        # Ctrl-C already unwound via KeyboardInterrupt before this spec; the
        # assertion is that adding handlers for the other two did not break it.
        self._start()
        self.assertEqual(self._stop(signal.SIGINT), 0)
        self.assertFalse(self.descriptor.exists())
        self.assertFalse(self.state.exists())

    def test_sigkill_leaves_both_and_that_is_correct(self) -> None:
        # Not a gap to close. The host checks pid and `started` before trusting a
        # descriptor, and `doctor` cross-checks the state file's pid, so a crash
        # is *reported* rather than hidden. Machinery to guarantee removal would
        # have to run in the one path where the process is already gone.
        self._start()
        self._stop(signal.SIGKILL)
        self.assertTrue(self.descriptor.exists())
        self.assertTrue(self.state.exists())

    def test_a_second_daemon_refuses_and_leaves_the_first_alone(self) -> None:
        """The failure this prevents is the *loser* deleting the winner's files.

        Huginn's issue #40 shape: two daemons both passed a liveness probe during
        a restart, and the one that lost the race clobbered the winner's files on
        its way out — so a perfectly healthy daemon dropped out of the menubar.
        """
        self._start()
        first = json.loads(self.state.read_text(encoding="utf-8"))
        second = subprocess.run(
            [sys.executable, "-c", _CHILD], env=self.env,
            capture_output=True, text=True, timeout=STARTUP_TIMEOUT_S)
        self.assertEqual(second.returncode, daemon.EXIT_ALREADY_RUNNING)
        self.assertIn("already running", second.stderr)
        self.assertIn(str(first["pid"]), second.stderr,
                      "the refusal must name what is holding the lock")
        # The survivor is untouched: same pid, both files still published.
        self.assertTrue(self.descriptor.exists())
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["pid"],
                         first["pid"])
        self.assertIsNone(self.proc.poll(), "the running daemon was stopped by a refusal")

    def test_a_restart_over_a_crashed_daemons_files_succeeds(self) -> None:
        # SIGKILL leaves both files and a lock file behind. A supervisor's very
        # next action is to restart, and that must work rather than read the
        # stale files as "already running".
        self._start()
        crashed = json.loads(self.state.read_text(encoding="utf-8"))["pid"]
        self._stop(signal.SIGKILL)
        self._start()
        self.assertNotEqual(json.loads(self.state.read_text(encoding="utf-8"))["pid"], crashed)
        self.assertEqual(self._stop(signal.SIGTERM), 0)
        self.assertFalse(self.state.exists())

    def test_no_menubar_serves_no_descriptor_but_still_ingests(self) -> None:
        child = _CHILD.replace('"serve"', '"serve", "--no-menubar"')
        self.proc = subprocess.Popen(
            [sys.executable, "-c", child], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        _wait_for(self.state.exists, STARTUP_TIMEOUT_S, "the daemon's state file")
        self.assertFalse(self.descriptor.exists())
        # Running, and honest that there is no port — which write_state records
        # as null rather than omitting.
        self.assertIsNone(json.loads(self.state.read_text(encoding="utf-8"))["port"])
        self.assertEqual(self._stop(signal.SIGTERM), 0)
        self.assertFalse(self.state.exists())

    def test_index_watch_publishes_neither_file(self) -> None:
        """The foreground path is not a second publisher, which was spec 009's bug.

        Before spec 010 this command owned the descriptor, so Muninn's presence in
        the menubar depended on someone running a debug command. It must now
        publish nothing at all — otherwise the two commands race for one
        descriptor path and the loser's teardown removes the winner's.
        """
        child = _CHILD.replace('"serve"', '"index", "--watch"')
        self.proc = subprocess.Popen(
            [sys.executable, "-c", child], env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
        # No state file to wait on, so wait for the lock instead — the one thing
        # a foreground watcher does publish.
        lock = self.tmp / "xdg" / "muninn" / "daemon.lock"
        _wait_for(lambda: daemon.SingleInstance.probe(lock)[0] is True,
                  STARTUP_TIMEOUT_S, "index --watch to take the lock")
        self.assertFalse(self.state.exists(), "index --watch wrote a state file")
        self.assertFalse(self.descriptor.exists(), "index --watch published a descriptor")
        self.assertEqual(self._stop(signal.SIGTERM), 0)

    def test_doctor_reports_the_running_daemon_and_its_port(self) -> None:
        self._start()
        port = json.loads(self.state.read_text(encoding="utf-8"))["port"]
        report = subprocess.run(
            [sys.executable, "-c",
             'import sys; sys.argv=["muninn","doctor"]\n'
             'from muninn.cli import main; sys.exit(main())'],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertIn(f"pid {self.proc.pid}", report.stdout)
        self.assertIn(str(port), report.stdout)
        self.assertIn("serve", report.stdout)

    def test_doctor_calls_a_crashed_daemon_stale_rather_than_running(self) -> None:
        self._start()
        self._stop(signal.SIGKILL)
        report = subprocess.run(
            [sys.executable, "-c",
             'import sys; sys.argv=["muninn","doctor"]\n'
             'from muninn.cli import main; sys.exit(main())'],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertIn("stale", report.stdout.lower())
        self.assertNotIn("  running     pid", report.stdout)


if __name__ == "__main__":
    unittest.main()
