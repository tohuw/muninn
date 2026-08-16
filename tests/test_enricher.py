"""Automatic enrichment (spec 018): and above all, what it refuses to pay for.

The failure this file guards against is not "enrichment did not run". It is
**enrichment that ran and billed somebody without being asked** — a provider chain
whose seat-licensed primary hop went away, a rate table consulted once at startup,
an unknown model priced as though unknown meant zero. Those are all silent, and the
invoice arrives a month later.

The second failure, equally covered: a guard so cautious it refuses on every
ordinary install, which looks identical to a working guard and means the feature
did not ship.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from muninn import cost, enrich, enricher
from muninn.policy import PolicyRefused
from muninn.store import open_store


class FakeProvider:
    """A text provider whose model — and billing — can change under the worker."""

    name = "fake"

    def __init__(self, model: str = "gpt-5.6-luna", metered: bool | None = False,
                 available_reason: str | None = None) -> None:
        self._model = model
        self.metered = metered
        self._available_reason = available_reason
        self.calls = 0

    @property
    def model(self) -> str:
        return self._model

    def available(self) -> str | None:
        return self._available_reason

    def generate(self, prompt: str, **_kw) -> str:
        self.calls += 1
        return '{"topic": "t", "outcome": "fixed", "problem": "p", "resolution": "r"}'


class MeteredGuardTest(unittest.TestCase):
    """The money guard. Every test here is about not spending unasked."""

    def _worker(self, provider, **kw) -> enricher.BackgroundEnricher:
        return enricher.BackgroundEnricher("unused.db", provider=provider, **kw)

    def test_a_seat_licensed_provider_is_allowed(self) -> None:
        self.assertTrue(self._worker(FakeProvider())._spending_allowed())

    def test_a_metered_provider_is_refused_and_named(self) -> None:
        w = self._worker(FakeProvider(model="claude-haiku-4-5", metered=True))
        self.assertFalse(w._spending_allowed())
        self.assertEqual(w.stopped_reason, enricher.STOPPED_METERED)
        # Naming the model is the difference between a reader fixing the provider
        # and a reader debugging the worker.
        self.assertEqual(w.billed_model, "claude-haiku-4-5")

    def test_the_opt_in_permits_it(self) -> None:
        w = self._worker(FakeProvider(model="claude-haiku-4-5", metered=True),
                         allow_metered=True)
        self.assertTrue(w._spending_allowed())

    def test_the_provider_outranks_the_rate_table(self) -> None:
        """The bug that nearly shipped this feature dead.

        ``claude -p`` on a Claude Code subscription costs nothing marginal, and the
        rate table only knows the API price for that same id. Pricing from the id
        alone refused on every default public install — a guard that looks correct
        and blocks everybody.
        """
        self.assertTrue(cost.bills_per_token("claude-haiku-4-5"),
                        "precondition: with no rate on file this fails closed")
        w = self._worker(FakeProvider(model="claude-haiku-4-5", metered=False))
        self.assertTrue(w._spending_allowed())

    def test_no_opinion_falls_back_to_the_rate_table(self) -> None:
        """With no shipped prices, only local inference clears this on its own.

        That is the intended shape rather than a gap: a hosted model with no
        rate on file and a provider with no opinion is precisely the case where
        nobody has established that a call is free, so the guard refuses.
        """
        allowed = self._worker(FakeProvider(
            model="mlx-community/bge-small-en-v1.5-bf16", metered=None))
        refused = self._worker(FakeProvider(model="claude-sonnet-5", metered=None))
        self.assertTrue(allowed._spending_allowed())
        self.assertFalse(refused._spending_allowed())

    def test_an_unknown_model_with_no_opinion_is_refused(self) -> None:
        """Fails closed. A price nobody recorded is a price nobody ruled out."""
        w = self._worker(FakeProvider(model="some-model-nobody-priced", metered=None))
        self.assertFalse(w._spending_allowed())
        self.assertEqual(w.stopped_reason, enricher.STOPPED_METERED)

    def test_an_unreadable_model_is_refused(self) -> None:
        class Broken:
            name = "broken"
            metered = None

            @property
            def model(self):
                raise RuntimeError("provider exploded")

        w = self._worker(Broken())
        self.assertFalse(w._spending_allowed())

    def test_local_inference_does_not_bill_and_everything_else_is_unknown(self) -> None:
        """No prices ship, so the only thing this can rule out is local inference.

        Nothing leaves the box, so there is nothing to bill — that is a fact
        about where the work runs, not a price anyone looked up. Every hosted
        model is unknown until the reader supplies a rates.json, and unknown
        fails closed.
        """
        self.assertFalse(cost.bills_per_token("mlx-community/bge-small-en-v1.5-bf16"))
        self.assertFalse(cost.bills_per_token("BAAI/bge-small-en-v1.5"))
        self.assertTrue(cost.bills_per_token("claude-sonnet-5"))
        self.assertTrue(cost.bills_per_token("nothing-priced-here"))


class StartupRefusalTest(unittest.TestCase):
    """Three different reasons not to start, kept as three different facts."""

    def test_no_provider_is_not_an_error(self) -> None:
        w = enricher.BackgroundEnricher("unused.db")
        with mock.patch("muninn.providers.resolve_provider",
                        side_effect=RuntimeError("none installed")):
            self.assertFalse(w.start())
        self.assertEqual(w.stopped_reason, enricher.STOPPED_NO_PROVIDER)

    def test_an_unavailable_provider_does_not_start(self) -> None:
        w = enricher.BackgroundEnricher(
            "unused.db", provider=FakeProvider(available_reason="binary not on PATH"))
        self.assertFalse(w.start())
        self.assertEqual(w.stopped_reason, enricher.STOPPED_NO_PROVIDER)

    def test_a_provider_with_no_opinion_on_availability_still_starts(self) -> None:
        """None means usable. A *reason string* is what refuses — see start()."""
        w = enricher.BackgroundEnricher(
            "unused.db", provider=FakeProvider(), idle_interval_s=0.01)
        with mock.patch.object(enricher.BackgroundEnricher, "_run", lambda _self: None):
            self.assertTrue(w.start())
        w.stop()

    def test_a_metered_provider_does_not_start(self) -> None:
        w = enricher.BackgroundEnricher(
            "unused.db", provider=FakeProvider(model="claude-sonnet-5", metered=True))
        self.assertFalse(w.start())
        self.assertEqual(w.stopped_reason, enricher.STOPPED_METERED)
        self.assertFalse(w.running)

    def test_status_reports_why_rather_than_just_that(self) -> None:
        w = enricher.BackgroundEnricher(
            "unused.db", provider=FakeProvider(model="claude-sonnet-5", metered=True))
        w.start()
        status = w.status()
        self.assertEqual(status["reason"], enricher.STOPPED_METERED)
        self.assertEqual(status["billed_model"], "claude-sonnet-5")
        self.assertFalse(status["allow_metered"])


class LoopTest(unittest.TestCase):
    """The pass loop over a real archive."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-enricher-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self.st.close)
        for i in range(4):
            self.st.upsert_session({
                "session_id": f"s{i}", "source": "claude", "provenance": "human",
                "text": "word " * 4000, "words": 4000, "user_turns": 1,
                "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
                "origin": "raw", "source_present": 1,
            })
        self.st.commit()
        self.calibration = {"sources": {"claude": {
            "enrichment_gate": {"threshold_words": 1000}}}}

    def _worker(self, provider, **kw) -> enricher.BackgroundEnricher:
        return enricher.BackgroundEnricher(
            self.db, provider=provider, idle_interval_s=0.01,
            backoff_start_s=0.01, backoff_max_s=0.02, **kw)

    def test_a_pass_enriches_and_stops_when_asked(self) -> None:
        w = self._worker(FakeProvider(), batch=2)
        with mock.patch.object(enrich, "load_calibration", return_value=self.calibration):
            w.start()
            deadline = 5.0
            import time
            while w.enriched < 4 and deadline > 0:
                time.sleep(0.05)
                deadline -= 0.05
            w.stop()
        self.assertEqual(w.enriched, 4)
        self.assertGreater(w.passes, 1, "batch=2 over 4 sessions should be >1 pass")

    def test_the_guard_reruns_between_passes(self) -> None:
        """The failure this whole module exists for.

        The provider starts on its seat-licensed hop and flips to a metered one, the
        way a chain does when the primary binary disappears. The worker must notice
        *between passes* and stop — not carry on because startup said it was fine.
        """
        provider = FakeProvider()

        def enrich_then_lose_codex(*_a, **_kw):
            # The hop changes *while the worker is mid-run*, which is exactly what
            # an uninstalled or logged-out Codex CLI does to a chain provider.
            provider._model = "claude-sonnet-5"
            provider.metered = True
            return enrich.EnrichResult(enriched=1)

        w = self._worker(provider, batch=1)
        with mock.patch.object(enrich, "load_calibration", return_value=self.calibration), \
                mock.patch.object(enrich, "enrich_sessions",
                                  side_effect=enrich_then_lose_codex):
            w.start()
            import time
            deadline = 5.0
            while w.running and deadline > 0:
                time.sleep(0.02)
                deadline -= 0.02
            w.stop()
        self.assertEqual(w.stopped_reason, enricher.STOPPED_METERED)
        self.assertEqual(w.billed_model, "claude-sonnet-5")
        # Exactly one pass ran. It stopped at the next guard check rather than
        # finishing the corpus on a billed model.
        self.assertEqual(w.enriched, 1)

    def test_policy_refused_stops_permanently(self) -> None:
        w = self._worker(FakeProvider())
        with mock.patch.object(enrich, "load_calibration", return_value=self.calibration), \
                mock.patch.object(enrich, "enrich_sessions",
                                  side_effect=PolicyRefused("not an approved path")):
            w.start()
            import time
            deadline = 5.0
            while w.running and deadline > 0:
                time.sleep(0.05)
                deadline -= 0.05
            w.stop()
        self.assertEqual(w.stopped_reason, enricher.STOPPED_POLICY)

    def test_a_stalled_pass_gives_up_rather_than_looping_on_a_paid_api(self) -> None:
        """Zero enriched with candidates present, forever, is money on fire."""
        empty = enrich.EnrichResult(enriched=0, failed=2)
        w = self._worker(FakeProvider(), stall_limit=2)
        with mock.patch.object(enrich, "load_calibration", return_value=self.calibration), \
                mock.patch.object(enrich, "enrich_sessions", return_value=empty):
            w.start()
            import time
            deadline = 5.0
            while w.running and deadline > 0:
                time.sleep(0.05)
                deadline -= 0.05
            w.stop()
        self.assertEqual(w.stopped_reason, enricher.STOPPED_STALLED)
        self.assertGreaterEqual(w.passes, 2)

    def test_an_uncalibrated_archive_waits_instead_of_dying(self) -> None:
        """Spec 011 forbids defaulting the gate; a restart to recover is worse."""
        w = self._worker(FakeProvider())
        with mock.patch.object(enrich, "load_calibration", return_value=None):
            w.start()
            import time
            time.sleep(0.2)
            self.assertTrue(w.running, "it should be waiting, not stopped")
            self.assertEqual(w.enriched, 0)
            w.stop()
        self.assertEqual(w.stopped_reason, enricher.STOPPED_REQUESTED)

    def test_a_transient_failure_retries_rather_than_stopping(self) -> None:
        calls = {"n": 0}

        def flaky(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("network hiccup")
            return enrich.EnrichResult(enriched=1)

        w = self._worker(FakeProvider(), batch=1)
        with mock.patch.object(enrich, "load_calibration", return_value=self.calibration), \
                mock.patch.object(enrich, "enrich_sessions", side_effect=flaky):
            w.start()
            import time
            deadline = 5.0
            while calls["n"] < 3 and deadline > 0:
                time.sleep(0.02)
                deadline -= 0.02
            w.stop()
        self.assertGreaterEqual(calls["n"], 3)
        self.assertNotEqual(w.stopped_reason, enricher.STOPPED_STALLED)


class DaemonWiringTest(unittest.TestCase):
    """`serve` enriches; `index --watch` must never start spending."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-enrich-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _kwargs(self, *argv) -> dict:
        from muninn import cli, daemon

        parser = cli.build_parser()
        args = parser.parse_args(list(argv))
        args.db = self.tmp / "archive.db"
        seen: dict[str, object] = {}

        class FakeDaemon:
            restart_requested = False

            def __init__(self, *_a, **kw):
                seen.update(kw)

            def run(self, **_kw):
                return 0

        holder = daemon.HOLDER_SERVE if argv[0] == "serve" else daemon.HOLDER_WATCH
        with mock.patch.object(cli.daemon, "Daemon", FakeDaemon):
            cli._run_ingest_loop(args, {}, menubar=False, holder=holder)
        return seen

    def test_serve_enriches_by_default(self) -> None:
        self.assertTrue(self._kwargs("serve")["enrich"])

    def test_serve_does_not_allow_spending_by_default(self) -> None:
        # The default must be the safe one. A daemon that bills unasked is the
        # whole failure this spec is about.
        self.assertFalse(self._kwargs("serve")["enrich_metered"])

    def test_no_enrich_declines_it(self) -> None:
        self.assertFalse(self._kwargs("serve", "--no-enrich")["enrich"])

    def test_the_opt_in_is_passed_through(self) -> None:
        self.assertTrue(self._kwargs("serve", "--enrich-metered")["enrich_metered"])

    def test_index_watch_never_enriches(self) -> None:
        self.assertFalse(self._kwargs("index", "--watch")["enrich"])

    def test_embedding_and_enrichment_are_separate_switches(self) -> None:
        """They are different amounts of money; one flag would conflate them."""
        kw = self._kwargs("serve", "--no-enrich")
        self.assertTrue(kw["embed"])
        self.assertFalse(kw["enrich"])


class VocabularyTest(unittest.TestCase):
    """Spec 018: nothing model-backed is described as "free"."""

    def test_the_cost_report_never_says_free(self) -> None:
        doc = cost.project(words=100_000, chunks=300, enrich_words=100_000,
                           enrich_calls=10, enrich_sessions=10,
                           text_model="gpt-5.6-luna")
        import json

        self.assertNotIn("free", json.dumps(doc).lower())

    def test_the_renamed_helper_is_the_only_spelling(self) -> None:
        # The rename exists so the vocabulary cannot drift back in through a
        # copied identifier.
        self.assertFalse(hasattr(cost, "free_stages"))
        self.assertTrue(callable(cost.unmetered_stages))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
