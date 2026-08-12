"""Derived thresholds: `muninn survey`, calibration.json, and drift.

The thing these tests defend is stated in
.valholl/articles/derived-calibration.md: a fixed threshold encodes one person's
habits as everyone's defaults. A proposed "enrich sessions >= 300 words" gate
selected 37% of Claude sessions but 91% of Codex ones on the same machine.
Derived gates on that corpus landed at 4,046 and 2,480 words while both hit ~85%
text coverage — so what must be held fixed is the *coverage*, and what must be
free to differ is the word count.

So the load-bearing assertions here are not "the gate equals N". They are that
two differently-shaped sources get different thresholds, that both land on the
same coverage, that tool-invoked sessions move neither, and that a second run
over an unchanged archive is byte-identical. A test that pinned the number would
pass while the design was being undone.

No test reads a real archive, ``~/.claude`` or ``~/.codex``: every corpus here is
built row by row in a tempdir.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import cli, survey
from muninn.store import open_store


class _Archive(unittest.TestCase):
    """A tempdir archive that can be filled with sessions of chosen shapes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-survey-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)
        self._n = 0

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, source: str, words: int, *, provenance: str = "human",
            cwd: str | None = None, present: int = 1) -> str:
        self._n += 1
        session_id = f"{source}-{provenance}-{self._n:04d}"
        self.st.upsert_session({
            "session_id": session_id, "source": source, "provenance": provenance,
            "text": "word " * words, "words": words, "user_turns": 3,
            "assistant_turns": 3, "tool_uses": 0, "tool_results": 0,
            "origin": "raw", "source_present": present, "cwd": cwd,
        })
        self.st.commit()
        return session_id

    def survey(self, **kwargs):
        # roots={} so no test ever walks a real transcript tree for index lag.
        return survey.survey(self.st, db=self.db, roots={}, **kwargs)


class GateDerivationTest(_Archive):
    """Coverage is the intent; the word threshold is what a corpus needs to hit it."""

    def _two_shaped_sources(self) -> None:
        # Deliberately different shapes, mirroring the real finding: one source
        # with a few long conversations, one with many shorter ones.
        for words in (12000, 9000, 7000, 5000, 400, 300, 250, 200, 150, 100):
            self.add("claude", words)
        for words in (3000, 2800, 2600, 2400, 2200, 2000, 1800, 1600, 1400, 1200):
            self.add("codex", words)

    def test_two_sources_get_different_thresholds(self) -> None:
        # The whole point. One constant across both would have been two
        # different policies wearing one number.
        self._two_shaped_sources()
        doc = self.survey()
        claude = doc["sources"]["claude"]["enrichment_gate"]["threshold_words"]
        codex = doc["sources"]["codex"]["enrichment_gate"]["threshold_words"]
        self.assertNotEqual(claude, codex)

    def test_both_thresholds_land_on_the_same_coverage_intent(self) -> None:
        # And this is why differing thresholds are correct rather than
        # inconsistent: they are two answers to one question.
        self._two_shaped_sources()
        doc = self.survey()
        for source in ("claude", "codex"):
            gate = doc["sources"][source]["enrichment_gate"]
            self.assertGreaterEqual(gate["coverage_pct"], survey.COVERAGE_TARGET_PCT)
            self.assertEqual(gate["target_coverage_pct"], survey.COVERAGE_TARGET_PCT)

    def test_the_gate_is_the_smallest_set_that_reaches_the_target(self) -> None:
        # Smallest, not merely sufficient: enriching everything would also cover
        # 85% and would defeat the purpose of having a gate.
        self._two_shaped_sources()
        gate = self.survey()["sources"]["claude"]["enrichment_gate"]
        words = sorted((12000, 9000, 7000, 5000, 400, 300, 250, 200, 150, 100),
                       reverse=True)
        one_fewer = sum(words[:gate["sessions"] - 1]) / sum(words) * 100.0
        self.assertLess(one_fewer, survey.COVERAGE_TARGET_PCT)

    def test_a_uniform_corpus_still_derives_a_gate(self) -> None:
        for _ in range(20):
            self.add("claude", 1000)
        gate = self.survey()["sources"]["claude"]["enrichment_gate"]
        self.assertEqual(gate["threshold_words"], 1000)
        self.assertGreaterEqual(gate["coverage_pct"], survey.COVERAGE_TARGET_PCT)


class ProvenanceScopingTest(_Archive):
    """Tool-invoked sessions contribute to no statistic. Pooling once cost 40x."""

    def test_tool_invoked_sessions_do_not_move_the_gate(self) -> None:
        for words in (5000, 4000, 3000, 200, 100):
            self.add("claude", words)
        before = self.survey()["sources"]["claude"]["enrichment_gate"]
        # 900 programmatic `claude -p` calls, the shape that made one corpus look
        # 40x larger and its median session 16x shorter.
        for _ in range(900):
            self.add("claude", 30, provenance="tool-invoked")
        after = self.survey()["sources"]["claude"]["enrichment_gate"]
        self.assertEqual(before, after)

    def test_tool_invoked_sessions_do_not_move_any_distribution(self) -> None:
        for words in (5000, 4000, 3000):
            self.add("claude", words)
        before = self.survey()["sources"]["claude"]
        for _ in range(50):
            self.add("claude", 30, provenance="tool-invoked")
        after = self.survey()["sources"]["claude"]
        for key in ("conversations", "conversation_words"):
            self.assertEqual(before[key], after[key])
        for cls in ("human", "subagent"):
            self.assertEqual(before["provenance"][cls], after["provenance"][cls])

    def test_they_are_still_counted_in_their_own_class(self) -> None:
        # Excluded from the derivation, not from the report. Their volume is the
        # signal that a corpus is contaminated, so hiding it would remove the
        # evidence the anomaly rule depends on.
        self.add("claude", 5000)
        for _ in range(50):
            self.add("claude", 30, provenance="tool-invoked")
        report = self.survey()["sources"]["claude"]
        self.assertEqual(report["provenance"]["tool-invoked"]["sessions"], 50)
        self.assertEqual(report["conversations"], 1)

    def test_subagent_sessions_do_count_as_conversations(self) -> None:
        # They hold real work, and `muninn search` covers them by default. A gate
        # derived without them would under-enrich exactly the transcripts that
        # cost 251 files and 725,706 words to recover once already.
        self.add("claude", 4000)
        self.add("claude", 4000, provenance="subagent")
        self.assertEqual(self.survey()["sources"]["claude"]["conversations"], 2)


class EmptyArchiveTest(_Archive):
    """A fresh archive is a normal thing to survey. The answer is not a crash."""

    def test_survey_succeeds_and_divides_by_nothing(self) -> None:
        doc = self.survey()
        self.assertEqual(doc["sources"], {})
        self.assertEqual(doc["archive"]["sessions"], 0)

    def test_it_says_so_rather_than_reporting_silence(self) -> None:
        self.assertTrue(any("no sessions" in note.lower()
                            for note in self.survey()["anomalies"]))

    def test_a_source_with_only_tool_invoked_sessions_derives_no_gate(self) -> None:
        for _ in range(10):
            self.add("claude", 100, provenance="tool-invoked")
        gate = self.survey()["sources"]["claude"]["enrichment_gate"]
        self.assertEqual(gate["threshold_words"], 0)
        self.assertEqual(gate["sessions"], 0)

    def test_wordless_conversations_derive_no_gate(self) -> None:
        for _ in range(5):
            self.add("claude", 0)
        gate = self.survey()["sources"]["claude"]["enrichment_gate"]
        self.assertEqual(gate["threshold_words"], 0)
        self.assertEqual(gate["coverage_pct"], 0.0)

    def test_the_cli_exits_zero_on_an_empty_archive(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--db", str(self.db), "survey"]), 0)


class IdempotenceTest(_Archive):
    """Two runs over an unchanged archive agree to the byte, timestamp aside."""

    def test_the_document_is_identical_apart_from_the_timestamp(self) -> None:
        for words in (5000, 3000, 900, 100):
            self.add("claude", words)
            self.add("codex", words // 2)
        first, second = self.survey(), self.survey()
        self.assertNotEqual(first.pop("surveyed_at"), None)
        second.pop("surveyed_at")
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_the_written_file_is_identical_apart_from_the_timestamp(self) -> None:
        self.add("claude", 5000)
        path = self.tmp / "calibration.json"
        for _ in range(2):
            survey.write_calibration(self.survey(), path)
            doc = json.loads(path.read_text())
            doc.pop("surveyed_at")
            written = json.dumps(doc, sort_keys=True)
        survey.write_calibration(self.survey(), path)
        doc = json.loads(path.read_text())
        doc.pop("surveyed_at")
        self.assertEqual(written, json.dumps(doc, sort_keys=True))

    def test_anomaly_order_is_deterministic(self) -> None:
        # Anomalies are prose in a diffable file. An unordered list would make
        # every re-survey look like a change.
        for source in ("codex", "claude"):
            self.add(source, 5000)
            for _ in range(20):
                self.add(source, 20, provenance="tool-invoked")
        self.assertEqual(self.survey()["anomalies"], self.survey()["anomalies"])


class AnomalyTest(_Archive):
    """The survey's first act is to report what is strange about the data."""

    def test_a_tool_invoked_majority_is_surfaced(self) -> None:
        # The finding that validated the whole idea: a prototype flagged 92%
        # tool-invoked unprompted, the exact contamination that had already
        # produced a 40x error in a hand analysis nobody had doubted.
        self.add("claude", 5000)
        for _ in range(30):
            self.add("claude", 20, provenance="tool-invoked")
        self.assertTrue(any("tool-invoked" in note for note in self.survey()["anomalies"]))

    def test_a_source_with_no_human_sessions_is_surfaced(self) -> None:
        for _ in range(10):
            self.add("codex", 500, provenance="subagent")
        self.assertTrue(any("human" in note for note in self.survey()["anomalies"]))

    def test_irreplaceable_sessions_are_surfaced(self) -> None:
        # Not a problem — the point of the archive — but the operator should know
        # how much of it exists nowhere else before deciding anything about it.
        self.add("claude", 5000, present=0)
        self.assertTrue(any("only copy" in note for note in self.survey()["anomalies"]))

    def test_a_thin_corpus_is_called_provisional(self) -> None:
        self.add("claude", 5000)
        self.assertTrue(any("provisional" in note for note in self.survey()["anomalies"]))

    def test_index_lag_is_reported_as_an_anomaly_not_left_to_bias_the_gate(self) -> None:
        # The failure the wiki article records: a gate derived while 149 newer
        # transcripts sat unindexed was 41% too high. Staleness has to travel
        # attached to the number it invalidates.
        self.add("claude", 5000)
        doc = self.survey()
        doc["index_lag"] = {"measured": True, "sources": {"claude": 149},
                            "last_sweep_at": "2026-07-23T00:00:00+00:00"}
        self.assertTrue(any("not yet indexed" in note
                            for note in survey.anomalies(doc)))

    def test_unmeasurable_lag_is_admitted(self) -> None:
        self.add("claude", 5000)
        doc = self.survey()
        doc["index_lag"] = {"measured": False, "reason": "unreadable"}
        self.assertTrue(any("staleness" in note for note in survey.anomalies(doc)))


class CalibrationFileTest(_Archive):
    """The artifact is meant to be read, diffed and committed."""

    def test_it_lands_beside_the_archive_it_describes(self) -> None:
        # Beside the database, not in a fixed state dir: a second archive must
        # not silently read thresholds derived from the first.
        self.assertEqual(survey.calibration_path(self.db).parent, self.db.parent)
        self.assertEqual(survey.calibration_path(self.db).name, "calibration.json")

    @unittest.skipIf(os.name == "nt", "Windows does not implement POSIX owner-only modes")
    def test_it_is_owner_only(self) -> None:
        path = self.tmp / "calibration.json"
        survey.write_calibration(self.survey(), path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_it_round_trips(self) -> None:
        self.add("claude", 5000)
        path = self.tmp / "calibration.json"
        doc = self.survey()
        survey.write_calibration(doc, path)
        self.assertEqual(survey.read_calibration(path), doc)

    def test_a_foreign_schema_reads_as_never_surveyed(self) -> None:
        # "Never surveyed" and "written by a version whose shape I cannot
        # interpret" mean the same thing to a caller: nothing here to trust.
        path = self.tmp / "calibration.json"
        path.write_text(json.dumps({"schema": "muninn.calibration/99"}))
        self.assertIsNone(survey.read_calibration(path))

    def test_unreadable_or_absent_reads_as_never_surveyed(self) -> None:
        self.assertIsNone(survey.read_calibration(self.tmp / "nope.json"))
        broken = self.tmp / "broken.json"
        broken.write_text("{not json")
        self.assertIsNone(survey.read_calibration(broken))

    def test_dry_run_writes_nothing(self) -> None:
        self.add("claude", 5000)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["--db", str(self.db), "survey", "--dry-run"])
        self.assertFalse(survey.calibration_path(self.db).exists())

    def test_the_cli_writes_where_it_says_it_did(self) -> None:
        self.add("claude", 5000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db), "survey"])
        path = survey.calibration_path(self.db)
        self.assertTrue(path.exists())
        self.assertIn(str(path), out.getvalue())

    def test_json_output_is_the_document_and_nothing_else(self) -> None:
        # An agent parses this. Anything else on stdout makes it unparseable.
        self.add("claude", 5000)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db), "survey", "--dry-run", "--json"])
        self.assertEqual(json.loads(out.getvalue())["schema"], survey.CALIBRATION_SCHEMA)


class DriftTest(_Archive):
    """When the stored thresholds stop describing the corpus."""

    def _calibrate(self) -> dict:
        return self.survey()

    def test_an_unchanged_corpus_has_not_drifted(self) -> None:
        # The other half of every assertion below. A drift check that fired
        # always would be indistinguishable from one that never fired.
        for words in (5000, 3000, 900):
            self.add("claude", words)
        self.assertEqual(survey.drift(self.st, self._calibrate()), [])

    def test_doubling_the_corpus_is_drift(self) -> None:
        for _ in range(10):
            self.add("claude", 1000)
        doc = self._calibrate()
        for _ in range(10):
            self.add("claude", 1000)
        self.assertTrue(any("grown" in r for r in survey.drift(self.st, doc)))

    def test_a_gate_that_now_selects_a_different_fraction_is_drift(self) -> None:
        # The signal a threshold comparison cannot produce: the stored number is
        # unchanged and the policy it encodes has changed completely. Coverage
        # barely moves here (98% -> 100%) while selection goes 60% -> 97%, which
        # is exactly why both halves are measured.
        for words in (5000, 4000, 3000, 100, 100):
            self.add("claude", words)
        doc = self._calibrate()
        for _ in range(60):
            self.add("claude", 8000)
        self.assertTrue(any("selects" in r for r in survey.drift(self.st, doc)))

    def test_an_overshooting_gate_is_not_reported_as_drift_on_arrival(self) -> None:
        # The bug the first draft had. The gate is the *smallest* set reaching
        # the target, so it always overshoots: one 5,000-word conversation
        # covers 100% of an 85% target. Comparing achieved coverage against the
        # target reported a correct, freshly written calibration as drifted, and
        # did it worst on the small corpora where a survey is most tentative.
        self.add("claude", 5000)
        doc = self._calibrate()
        self.assertEqual(doc["sources"]["claude"]["enrichment_gate"]["coverage_pct"], 100.0)
        self.assertEqual(survey.drift(self.st, doc), [])

    def test_a_new_source_is_drift(self) -> None:
        for _ in range(10):
            self.add("claude", 1000)
        doc = self._calibrate()
        for _ in range(10):
            self.add("codex", 1000)
        reasons = survey.drift(self.st, doc)
        self.assertTrue(any("codex" in r and "no thresholds" in r for r in reasons))

    def test_a_shifted_source_mix_is_drift(self) -> None:
        for _ in range(20):
            self.add("claude", 1000)
        for _ in range(2):
            self.add("codex", 1000)
        doc = self._calibrate()
        for _ in range(20):
            self.add("codex", 1000)
        self.assertTrue(any("share of conversations" in r
                            for r in survey.drift(self.st, doc)))

    def test_a_smaller_archive_is_reported_as_possibly_a_different_one(self) -> None:
        # The archive never deletes prose, so a shrink means these thresholds
        # are being applied to a corpus they were not derived from.
        for _ in range(10):
            self.add("claude", 1000)
        doc = self._calibrate()
        doc["archive"]["sessions"] = 400
        self.assertTrue(any("different archive" in r for r in survey.drift(self.st, doc)))

    def test_tool_invoked_growth_alone_does_not_drift_the_gate(self) -> None:
        # A machine that ran a lot of `claude -p` this month has not changed the
        # corpus the gate describes, and telling its owner to re-survey would
        # train them to ignore the warning.
        for words in (5000, 4000, 3000, 900, 100):
            self.add("claude", words)
        doc = self._calibrate()
        for _ in range(500):
            self.add("claude", 25, provenance="tool-invoked")
        self.assertEqual([r for r in survey.drift(self.st, doc) if "covers" in r], [])


class DoctorTest(_Archive):
    """Three states, kept distinct: never surveyed, current, drifted."""

    def _report(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_calibration_section(self.st, str(self.db))
        return out.getvalue()

    def test_never_surveyed_is_said_outright(self) -> None:
        report = self._report()
        self.assertIn("never surveyed", report)
        self.assertIn("muninn survey", report)

    def test_current_is_reported_as_a_positive_answer(self) -> None:
        # Stated, not implied by the absence of a warning. A section that only
        # speaks up when something is wrong leaves a reader unable to tell
        # "fine" from "not checked".
        self.add("claude", 5000)
        survey.write_calibration(self.survey(), survey.calibration_path(self.db))
        report = self._report()
        self.assertIn("drift", report)
        self.assertIn("none", report)
        self.assertNotIn("WARNING", report)

    def test_drift_names_its_reasons(self) -> None:
        # "Re-run survey" alone is an instruction; the reasons are the finding.
        for _ in range(10):
            self.add("claude", 1000)
        survey.write_calibration(self.survey(), survey.calibration_path(self.db))
        for _ in range(20):
            self.add("claude", 1000)
        report = self._report()
        self.assertIn("WARNING", report)
        self.assertIn("grown", report)

    def test_an_unreadable_calibration_is_not_reported_as_current(self) -> None:
        survey.calibration_path(self.db).write_text("{not json")
        self.assertIn("never surveyed", self._report())

    def test_the_gate_in_force_is_shown(self) -> None:
        # `doctor` is what an agent relays to a human, and "what threshold is
        # actually in force" is the question a calibration file exists to answer.
        for words in (5000, 4000, 3000, 100):
            self.add("claude", words)
        survey.write_calibration(self.survey(), survey.calibration_path(self.db))
        self.assertIn("enrich gate", self._report())


if __name__ == "__main__":
    unittest.main()
