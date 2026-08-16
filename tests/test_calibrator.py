"""The worker that keeps the derived gate describing the archive (spec 011).

The failure it exists to prevent is not an error, which is what made it hard to
see: the archive tripled between surveys, the gate that had been derived to
cover 85% of conversation text fell to 74%, and enrichment went on reporting
"100% of eligible" throughout. It was telling the truth. The eligible set had
quietly shrunk.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import calibrator, store, survey


class CalibratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-calib-"))
        self.db = self.tmp / "archive.db"
        store.open_store(self.db).close()
        self.path = survey.calibration_path(self.db)

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _worker(self, **kw):
        return calibrator.BackgroundCalibrator(self.db, **kw)

    def test_an_unsurveyed_archive_is_surveyed(self):
        """A fresh install enriched nothing until someone ran survey by hand.

        enrich.plan refuses to default a threshold (spec 011), so "never
        surveyed" means "enrich nothing" -- and it looks identical to a working
        install that has no backlog.
        """
        reasons = self._worker().check_once()

        self.assertEqual(reasons, ["no calibration had been derived yet"])
        self.assertTrue(self.path.exists())

    def test_a_current_calibration_is_left_alone(self):
        worker = self._worker()
        worker.check_once()
        before = self.path.read_text(encoding="utf-8")

        self.assertEqual(worker.check_once(), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)
        self.assertEqual(worker.surveys, 1)

    def test_drift_triggers_a_re_survey(self):
        worker = self._worker()
        worker.check_once()

        with patch.object(survey, "drift", return_value=["the corpus has grown 3.0x"]):
            reasons = worker.check_once()

        self.assertEqual(reasons, ["the corpus has grown 3.0x"])
        self.assertEqual(worker.surveys, 2)

    def test_it_says_what_it_did_and_why(self):
        """Silent self-correction is its own kind of unexplained behaviour."""
        said: list[str] = []
        worker = self._worker(announce=said.append)

        with patch.object(survey, "drift", return_value=["the corpus has grown 3.0x"]):
            worker.check_once()
            worker.check_once()

        self.assertEqual(len(said), 2)
        self.assertIn("re-derived the enrichment gate", said[0])
        self.assertIn("grown 3.0x", said[1])

    def test_an_unreadable_calibration_is_treated_as_absent(self):
        """Present-but-unparseable is what doctor calls "never surveyed"."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ not json", encoding="utf-8")

        self.assertEqual(self._worker().check_once(),
                         ["no calibration had been derived yet"])
        json.loads(self.path.read_text(encoding="utf-8"))   # rewritten, valid

    def test_a_failed_pass_is_not_fatal(self):
        """The loop must survive a bad pass: the previous gate is still usable."""
        worker = self._worker()
        worker._stop.set()          # one pass, then exit
        with patch.object(worker, "check_once", side_effect=RuntimeError("boom")):
            worker._run()           # must not raise

    def test_surveying_never_reaches_a_provider(self):
        """The reason this is safe to do unattended.

        A survey is aggregates over the archive. If it ever grew a model call,
        turning it on by default would become a spending decision, and this
        would be the test that noticed.
        """
        worker = self._worker()
        with patch("muninn.providers.resolve_provider",
                   side_effect=AssertionError("the calibrator resolved a provider")):
            worker.check_once()


class DaemonWiringTests(unittest.TestCase):
    """On by default, and switchable off -- the operator keeps the choice."""

    def test_the_daemon_defaults_to_recalibrating(self):
        from muninn.daemon import Daemon

        self.assertTrue(Daemon(":memory:", {}).recalibrate)

    def test_it_can_be_turned_off(self):
        from muninn.daemon import Daemon

        self.assertFalse(Daemon(":memory:", {}, recalibrate=False).recalibrate)

    def test_serve_accepts_the_flag(self):
        from muninn.cli import build_parser

        args = build_parser().parse_args(["serve", "--no-recalibrate"])
        self.assertTrue(args.no_recalibrate)
        self.assertFalse(build_parser().parse_args(["serve"]).no_recalibrate)


if __name__ == "__main__":
    unittest.main()
