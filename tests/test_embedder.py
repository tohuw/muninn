"""The background embedding worker (spec 014).

## What these tests are written against

The feature's promise is *"you finish a session, it becomes semantically
searchable"*. Most of what can go wrong with that is not "no vectors were
written" — it is one of three quieter things, and each has a test here that
fails if the guard is removed:

1. **A hot loop against a metered provider.** A stalled provider (returning
   fewer vectors than texts) or a policy refusal must stop the worker, not be
   retried forever. Against a metered provider that distinction is a bill.
2. **A daemon killed by an embedding failure.** Ingest durability outranks
   semantic recall; a provider outage must cost the archive nothing.
3. **The newest session embedded last.** On a fresh archive the backlog is
   thousands of chunks, and id-ordered draining would leave today's session
   until the end — which is precisely the session the promise is about.

:class:`WiringTest` exists for the reason ``tests/test_daemon.py`` states in its
own docstring: every unit test in this file would still pass with the daemon's
call to ``BackgroundEmbedder.start()`` deleted, and that call *is* the feature.

No test reaches a network or loads a model. The provider is always a fake, and
the tests that touch a real thread bound it explicitly.
"""
from __future__ import annotations

import contextlib
import math
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import cli, daemon, embed, embedder
from muninn.policy import PolicyRefused
from muninn.store import open_store

try:
    import numpy  # noqa: F401

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


class FakeProvider:
    """Deterministic unit vectors, with knobs for each failure this file covers.

    Deliberately a copy of ``test_embed.FakeEmbedder`` rather than an import of
    it: this one grows failure knobs that have no business in the storage tests,
    and a shared fake that both files bend to their needs is how a fixture stops
    describing anything.
    """

    name = "fake-embedder"
    model = "fake/model-v1"
    dim = 4

    def __init__(self, *, fail_times: int = 0, raises: Exception | None = None,
                 short: bool = False, on_call: object = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_times = fail_times
        self.raises = raises
        self.short = short          # return fewer vectors than texts: a stall
        self.on_call = on_call      # called with the batch, for stop-mid-pass

    def available(self) -> str | None:
        return None

    def _vector(self, text: str) -> list[float]:
        raw = [((abs(hash((text, i))) % 1000) + 1) / 1000.0 for i in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw))
        return [v / norm for v in raw]

    def embed(self, texts):
        texts = list(texts)
        self.calls.append(texts)
        if self.on_call is not None:
            self.on_call(texts)
        if self.raises is not None and len(self.calls) <= (self.fail_times or 10 ** 9):
            raise self.raises
        if self.short:
            return []
        return [self._vector(t) for t in texts]


class _Archive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-embedder-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)
        embed.clear_cache()

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        embed.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_session(self, session_id: str, text: str, *,
                    started_at: str | None = None, source: str = "claude") -> None:
        self.st.upsert_session({
            "session_id": session_id, "source": source, "provenance": "human",
            "text": text, "words": len(text.split()), "user_turns": 1,
            "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "origin": "raw", "source_present": 1, "started_at": started_at,
        })
        self.st.replace_chunks(session_id, text)
        self.st.commit()

    def worker(self, **kwargs) -> embedder.BackgroundEmbedder:
        kwargs.setdefault("provider", FakeProvider())
        kwargs.setdefault("batch", 1)
        kwargs.setdefault("window_batches", 4)
        # Zero waits: every test here bounds the loop by its own stop condition,
        # and a real backoff would make the retry and stall tests slow instead of
        # informative.
        kwargs.setdefault("backoff_start_s", 0.0)
        kwargs.setdefault("idle_interval_s", 0.0)
        return embedder.BackgroundEmbedder(self.db, **kwargs)


# ── One pass ──────────────────────────────────────────────────────────────────

class PassTest(_Archive):
    def test_a_pass_embeds_the_pending_chunks(self) -> None:
        self.add_session("s1", "alpha beta gamma")
        w = self.worker()
        written, seen = w._pass(self.st, w.provider)
        self.assertGreater(written, 0)
        self.assertEqual(written, seen)
        self.assertEqual(embed.pending_count(self.st, w.provider.model), 0)

    def test_a_second_pass_finds_nothing_and_says_so(self) -> None:
        self.add_session("s1", "alpha beta gamma")
        w = self.worker()
        w._pass(self.st, w.provider)
        self.assertEqual(w._pass(self.st, w.provider), (0, 0))

    def test_pending_count_agrees_with_what_a_pass_finds(self) -> None:
        self.add_session("s1", "alpha beta gamma")
        self.add_session("s2", "delta epsilon")
        w = self.worker(window_batches=1000)
        expected = embed.pending_count(self.st, w.provider.model)
        _written, seen = w._pass(self.st, w.provider)
        self.assertEqual(seen, expected)

    def test_re_chunking_a_session_makes_it_pending_again(self) -> None:
        """The automatic promise depends on this: an updated session re-embeds.

        ``replace_chunks`` drops the vectors for ordinals that no longer exist,
        so a session that grew between ingests reappears in the backlog by
        itself. Without it, an appended session would keep vectors describing
        only its first half — silently.
        """
        self.add_session("s1", "alpha beta")
        w = self.worker()
        w._pass(self.st, w.provider)
        self.assertEqual(embed.pending_count(self.st, w.provider.model), 0)

        self.add_session("s1", "alpha beta " + "gamma delta " * 400)
        self.assertGreater(embed.pending_count(self.st, w.provider.model), 0)

    def test_a_batch_that_succeeded_is_committed_before_a_later_one_fails(self) -> None:
        """Resumability, asserted from a *second connection* so it means commit.

        Checking ``vector_count`` on the writing connection would pass on
        uncommitted rows and prove nothing about surviving a kill.
        """
        for i in range(6):
            self.add_session(f"s{i}", f"session {i} text")
        provider = FakeProvider(raises=RuntimeError("provider fell over"), fail_times=10 ** 9)
        calls = {"n": 0}
        real_embed = FakeProvider().embed

        def flaky(texts):
            calls["n"] += 1
            if calls["n"] > 2:
                raise RuntimeError("provider fell over")
            return real_embed(texts)

        provider.embed = flaky      # type: ignore[method-assign]
        w = self.worker(provider=provider, batch=1)
        with self.assertRaises(RuntimeError):
            w._pass(self.st, provider)

        other = open_store(self.db)
        try:
            self.assertEqual(embed.vector_count(other, provider.model), 2)
        finally:
            other.close()

    def test_stop_mid_pass_abandons_the_remaining_batches(self) -> None:
        for i in range(6):
            self.add_session(f"s{i}", f"session {i} text")
        w = self.worker(batch=1, window_batches=6)
        w.provider.on_call = lambda _texts: w._stop.set()
        written, seen = w._pass(self.st, w.provider)
        self.assertEqual(len(w.provider.calls), 1,
                         "the worker kept calling a paid provider after being asked to stop")
        self.assertEqual(written, 1)
        self.assertGreater(seen, 1)


# ── Ordering ──────────────────────────────────────────────────────────────────

class OrderTest(_Archive):
    def test_the_newest_session_is_embedded_first(self) -> None:
        self.add_session("aaa-oldest", "oldest session", started_at="2026-01-01T00:00:00Z")
        self.add_session("mmm-middle", "middle session", started_at="2026-04-01T00:00:00Z")
        self.add_session("zzz-newest", "newest session", started_at="2026-08-01T00:00:00Z")

        rows = embed.pending_chunks(self.st, "fake/model-v1", newest_first=True)
        self.assertEqual(rows[0]["session_id"], "zzz-newest",
                         "id order would have put the oldest session first, which is the "
                         "session nobody is waiting on")

    def test_the_cli_keeps_id_order_so_a_resumed_run_continues_visibly(self) -> None:
        self.add_session("aaa-oldest", "oldest session", started_at="2026-01-01T00:00:00Z")
        self.add_session("zzz-newest", "newest session", started_at="2026-08-01T00:00:00Z")
        rows = embed.pending_chunks(self.st, "fake/model-v1")
        self.assertEqual(rows[0]["session_id"], "aaa-oldest")

    def test_an_undated_session_sorts_last_not_first(self) -> None:
        """A NULL ``started_at`` is missing data, not evidence of recency.

        SQLite sorts NULL lowest, so a bare ``ORDER BY started_at DESC`` puts
        undated sessions *last* — but the same expression with ASC, or a schema
        change, flips that. The query sorts on ``started_at IS NULL`` first so
        the behaviour is stated rather than inherited.
        """
        self.add_session("dated", "dated session", started_at="2026-01-01T00:00:00Z")
        self.add_session("undated", "undated session", started_at=None)
        rows = embed.pending_chunks(self.st, "fake/model-v1", newest_first=True)
        self.assertEqual(rows[0]["session_id"], "dated")
        self.assertEqual(rows[-1]["session_id"], "undated")

    def test_source_filtering_still_works(self) -> None:
        self.add_session("c1", "claude session", source="claude")
        self.add_session("x1", "codex session", source="codex")
        rows = embed.pending_chunks(self.st, "fake/model-v1", source="codex")
        self.assertTrue(rows)
        self.assertTrue(all(r["session_id"] == "x1" for r in rows))


# ── Failure handling: the three classes ───────────────────────────────────────

class FailureTest(_Archive):
    def test_no_provider_means_start_returns_false_rather_than_raising(self) -> None:
        """The default install ships no provider; that is normal, not an error."""
        w = embedder.BackgroundEmbedder(self.db, provider=None)
        with patch.object(embed, "resolve_provider",
                          side_effect=embed.EmbeddingUnavailable("no provider")):
            self.assertFalse(w.start())
        self.assertFalse(w.running)
        self.assertEqual(w.stopped_reason, embedder.STOPPED_NO_PROVIDER)

    def test_a_policy_refusal_stops_permanently_instead_of_retrying(self) -> None:
        """A refusal is a decision. Retrying it is a hot loop against a paid API."""
        self.add_session("s1", "alpha beta gamma")
        provider = FakeProvider(raises=PolicyRefused("this model is not permitted"),
                                fail_times=10 ** 9)
        w = self.worker(provider=provider)
        w._loop(self.st)        # returns rather than looping
        self.assertEqual(w.stopped_reason, embedder.STOPPED_POLICY)
        self.assertEqual(len(provider.calls), 1,
                         "a refused model was asked more than once")

    def test_a_stalled_provider_gives_up_after_the_limit(self) -> None:
        """Zero progress with a non-empty backlog must terminate, not spin."""
        self.add_session("s1", "alpha beta gamma")
        provider = FakeProvider(short=True)     # returns no vectors at all
        w = self.worker(provider=provider, stall_limit=3)
        w._loop(self.st)
        self.assertEqual(w.stopped_reason, embedder.STOPPED_STALLED)
        self.assertEqual(w.passes, 3, "the stall limit did not bound the passes")
        self.assertGreater(embed.pending_count(self.st, provider.model), 0)

    def test_a_transient_failure_is_retried_and_then_succeeds(self) -> None:
        self.add_session("s1", "alpha beta gamma")
        provider = FakeProvider(raises=OSError("network went away"), fail_times=2)
        w = self.worker(provider=provider)

        # Stop as soon as the backlog is drained, so the loop terminates without
        # relying on a timeout.
        original = w._pass

        def bounded(st, prov):
            result = original(st, prov)
            if embed.pending_count(st, prov.model) == 0:
                w._stop.set()
            return result

        w._pass = bounded       # type: ignore[method-assign]
        w._loop(self.st)
        self.assertEqual(embed.pending_count(self.st, provider.model), 0)
        self.assertGreaterEqual(len(provider.calls), 3, "it did not retry")
        self.assertIsNotNone(w.last_error)

    def test_an_unopenable_archive_does_not_raise_out_of_the_thread(self) -> None:
        """Nothing in the worker may reach the daemon as an exception."""
        w = self.worker()
        with patch.object(embedder.store, "open_store", side_effect=OSError("disk gone")):
            w._run()        # must not raise
        self.assertEqual(w.stopped_reason, embedder.STOPPED_STALLED)
        self.assertIn("disk gone", w.last_error or "")


# ── The thread ────────────────────────────────────────────────────────────────

class ThreadTest(_Archive):
    def test_the_thread_drains_the_backlog_and_stops_cleanly(self) -> None:
        for i in range(5):
            self.add_session(f"s{i}", f"session {i} alpha beta")
        drained = threading.Event()
        provider = FakeProvider()
        w = self.worker(provider=provider, batch=2, idle_interval_s=0.01)

        original = w._pass

        def watched(st, prov):
            result = original(st, prov)
            if embed.pending_count(st, prov.model) == 0:
                drained.set()
            return result

        w._pass = watched       # type: ignore[method-assign]
        self.assertTrue(w.start())
        try:
            self.assertTrue(drained.wait(timeout=10), "the worker never drained the backlog")
        finally:
            w.stop(timeout=10)
        self.assertFalse(w.running)
        self.assertEqual(w.stopped_reason, embedder.STOPPED_REQUESTED)
        self.assertGreater(w.vectors_written, 0)
        self.assertEqual(embed.pending_count(self.st, provider.model), 0)

    def test_stop_is_idempotent_and_safe_before_start(self) -> None:
        w = self.worker()
        w.stop()
        w.stop()
        self.assertFalse(w.running)

    def test_status_reports_the_model_and_the_reason(self) -> None:
        w = self.worker()
        status = w.status()
        self.assertEqual(status["model"], "fake/model-v1")
        self.assertEqual(status["reason"], embedder.STOPPED_NOT_STARTED)
        self.assertFalse(status["running"])


# ── Wiring: the call sites that are the feature ───────────────────────────────

class WiringTest(unittest.TestCase):
    """Every test above would pass with the daemon's ``start()`` call deleted."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-embedder-wire-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "archive.db"

    def _run_daemon(self, **kwargs) -> object:
        """Run a daemon whose ingest loop returns immediately."""
        service = daemon.Daemon(
            self.db, {}, menubar=False, publish_state=False,
            lock_file=self.tmp / "daemon.lock", **kwargs)
        service.run(max_iterations=0)
        return service

    def test_serve_starts_the_embedder(self) -> None:
        with patch.object(daemon.embedder, "BackgroundEmbedder") as factory:
            factory.return_value.start.return_value = True
            self._run_daemon()
        factory.assert_called_once()
        factory.return_value.start.assert_called_once()

    def test_the_embedder_is_stopped_on_teardown(self) -> None:
        with patch.object(daemon.embedder, "BackgroundEmbedder") as factory:
            factory.return_value.start.return_value = True
            self._run_daemon()
        factory.return_value.stop.assert_called_once()

    def test_a_worker_that_could_not_start_is_not_torn_down(self) -> None:
        """``start()`` returning False must leave nothing to stop."""
        with patch.object(daemon.embedder, "BackgroundEmbedder") as factory:
            factory.return_value.start.return_value = False
            service = self._run_daemon()
        self.assertIsNone(service.embedder)
        factory.return_value.stop.assert_not_called()

    def test_no_embed_declines_it(self) -> None:
        with patch.object(daemon.embedder, "BackgroundEmbedder") as factory:
            self._run_daemon(embed=False)
        factory.assert_not_called()

    def test_the_daemon_survives_an_embedder_that_fails_to_start(self) -> None:
        """A provider that raises at resolution must not cost the daemon its run."""
        with patch.object(embed, "resolve_provider", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                # Documenting the boundary: resolve_provider raising something
                # other than EmbeddingUnavailable is a provider bug, and start()
                # deliberately does not swallow arbitrary exceptions there — the
                # daemon's own except-clause is what keeps it alive.
                embedder.BackgroundEmbedder(self.db).start()


class EndToEndTest(unittest.TestCase):
    """A real ``Daemon``, a real thread, real vectors on disk.

    The mocked wiring tests above prove the call site exists; this one proves the
    path works. Only the provider is fake, and nothing about the worker, the
    thread, the connection or the teardown is patched — which is the difference
    between "``start()`` was called" and "my session is now searchable".
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-embedder-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "archive.db"
        self.root = self.tmp / "projects"
        self.root.mkdir()

        # Seed a session and close the connection: the daemon and its worker each
        # open their own, which is the arrangement under test.
        st = open_store(self.db)
        st.upsert_session({
            "session_id": "s1", "source": "claude", "provenance": "human",
            "text": "the session that should become searchable by itself",
            "words": 9, "user_turns": 1, "assistant_turns": 1, "tool_uses": 0,
            "tool_results": 0, "origin": "raw", "source_present": 1,
            "started_at": "2026-08-01T00:00:00Z",
        })
        st.replace_chunks("s1", "the session that should become searchable by itself")
        st.commit()
        st.close()
        embed.clear_cache()
        self.addCleanup(embed.clear_cache)

    def test_serving_embeds_a_pending_session_without_anyone_asking(self) -> None:
        provider = FakeProvider()
        drained = threading.Event()

        def watch_fn(_paths, **_kwargs):
            """Hold the ingest loop open until the worker has done its job."""
            drained.wait(timeout=15)
            return
            yield set()     # pragma: no cover - makes this a generator

        def poll() -> None:
            deadline = 15.0
            while deadline > 0 and not drained.is_set():
                probe = open_store(self.db)
                try:
                    if embed.pending_count(probe, provider.model) == 0:
                        drained.set()
                finally:
                    probe.close()
                if not drained.is_set():
                    threading.Event().wait(0.05)
                    deadline -= 0.05

        watcher = threading.Thread(target=poll, daemon=True)

        service = daemon.Daemon(
            self.db, {"claude": self.root},
            menubar=False, publish_state=False,
            lock_file=self.tmp / "daemon.lock")
        with patch.object(embed, "resolve_provider", return_value=provider):
            watcher.start()
            self.assertEqual(service.run(watch_fn=watch_fn), 0)
        watcher.join(timeout=5)

        self.assertTrue(drained.is_set(),
                        "the daemon ran but nothing embedded the pending session")
        st = open_store(self.db)
        try:
            self.assertGreater(embed.vector_count(st, provider.model), 0)
            self.assertEqual(embed.pending_count(st, provider.model), 0)
        finally:
            st.close()
        # And the worker was stopped by the teardown, not left running.
        self.assertIsNotNone(service.embedder)
        self.assertFalse(service.embedder.running)


class CommandWiringTest(unittest.TestCase):
    """`serve` embeds; `index --watch` does not. Asserted at the call site."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-embedder-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _embed_kwarg(self, *argv) -> bool:
        parser = cli.build_parser()
        args = parser.parse_args(list(argv))
        args.db = self.tmp / "archive.db"
        seen: dict[str, object] = {}

        class FakeDaemon:
            # Mirrors the real attribute the loop reads after run() returns; a
            # fake without it makes the restart check look like a wiring bug.
            restart_requested = False

            def __init__(self, *_a, **kw):
                seen.update(kw)

            def run(self, **_kw):
                return 0

        holder = daemon.HOLDER_SERVE if argv[0] == "serve" else daemon.HOLDER_WATCH
        with patch.object(cli.daemon, "Daemon", FakeDaemon):
            cli._run_ingest_loop(args, {}, menubar=False, holder=holder)
        return bool(seen["embed"])

    def test_serve_embeds_by_default(self) -> None:
        self.assertTrue(self._embed_kwarg("serve"))

    def test_serve_no_embed_does_not(self) -> None:
        self.assertFalse(self._embed_kwarg("serve", "--no-embed"))

    def test_index_watch_never_embeds(self) -> None:
        self.assertFalse(self._embed_kwarg("index", "--watch"))


class RestartLoopTest(unittest.TestCase):
    """The supervising loop around one ingest run."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-restart-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        parser = cli.build_parser()
        self.args = parser.parse_args(["serve"])
        self.args.db = self.tmp / "archive.db"

    def _run(self, restarts: int, exit_code: int = 0) -> tuple[int, int]:
        """Run the loop with a daemon that asks for ``restarts`` restarts."""
        runs = {"n": 0}

        class FakeDaemon:
            def __init__(self, *_a, **_kw) -> None:
                runs["n"] += 1
                self.restart_requested = runs["n"] <= restarts

            def run(self, **_kw) -> int:
                return exit_code

        with patch.object(cli.daemon, "Daemon", FakeDaemon), \
                patch.object(cli, "_announce", lambda _msg: None):
            code = cli._run_ingest_loop(self.args, {}, menubar=False,
                                        holder=daemon.HOLDER_SERVE)
        return code, runs["n"]

    def test_no_restart_runs_once_and_returns_the_code(self) -> None:
        self.assertEqual(self._run(restarts=0, exit_code=3), (3, 1))

    def test_a_restart_constructs_a_second_daemon(self) -> None:
        # A *new* instance, not a re-run of the old one: the action handler is
        # bound to the instance, and a reused daemon would carry a stale server
        # and a sticky restart flag.
        self.assertEqual(self._run(restarts=1), (0, 2))

    def test_restarts_are_not_bounded_by_a_count(self) -> None:
        self.assertEqual(self._run(restarts=4), (0, 5))

    def test_the_restart_sentinel_never_reaches_a_shell(self) -> None:
        """A supervisor reading it as an exit code would start racing us."""
        code, _runs = self._run(restarts=2)
        self.assertIsInstance(code, int)
        self.assertIsNot(code, cli._RESTART)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
