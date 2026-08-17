"""Failures are enumerated, not just counted (muninn/store.py).

`parse_failures` could say *sixteen enrichment failures* and nothing more. The
affected sessions could not be found, re-run, or even confirmed to still be
broken — so diagnosing one meant opening the SQLite file by hand, which is the
thing the CLI exists to make unnecessary. This repo already had the rule, in
docs/specs/README.md: "a count cannot be audited after the fact, and every
silent skip in the predecessor tools was a data-loss path nobody noticed."

Both records are kept. The aggregate carries lifetime totals so a rising rate
stays visible; the log is bounded and names names. Neither substitutes for the
other.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import store


class FailureLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-failures-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.st = store.open_store(self.tmp / "a.db")
        self.addCleanup(self.st.close)

    def _session(self, session_id: str, *, topic: str | None = None) -> None:
        self.st.conn.execute(
            "INSERT INTO sessions (session_id, source, provenance, words, text, topic) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, "claude", "human", 500, "prose", topic))
        self.st.commit()

    def test_a_failure_names_the_session(self):
        self._session("s1")
        self.st.record_parse_failure("enrich", "invalid-json", session_id="s1")
        self.st.commit()
        rows = self.st.recent_failures()
        self.assertEqual([r["session_id"] for r in rows], ["s1"])
        self.assertEqual(rows[0]["category"], "invalid-json")

    def test_the_lifetime_total_is_still_kept(self):
        """The log is trimmed, so it cannot answer "is this getting worse"."""
        for _ in range(3):
            self.st.record_parse_failure("enrich", "invalid-json", session_id="s1")
        self.st.commit()
        total = self.st.conn.execute(
            "SELECT count FROM parse_failures WHERE source = 'enrich'").fetchone()
        self.assertEqual(total["count"], 3)

    def test_it_says_which_failures_have_since_resolved(self):
        """An enrichment failure writes no facets and is retried next pass.

        Most heal on their own. Reporting them all as outstanding sends somebody
        chasing work that is already done.
        """
        self._session("healed", topic="a topic it now has")
        self._session("broken")
        self.st.record_parse_failure("enrich", "invalid-json", session_id="healed")
        self.st.record_parse_failure("enrich", "provider-error", session_id="broken")
        self.st.commit()

        state = {r["session_id"]: bool(r["enriched"]) for r in self.st.recent_failures()}
        self.assertTrue(state["healed"])
        self.assertFalse(state["broken"])

    def test_a_session_that_left_the_archive_is_distinguished(self):
        """Different from "still broken", and it needs a different response."""
        self.st.record_parse_failure("enrich", "invalid-json", session_id="gone")
        self.st.commit()
        row = self.st.recent_failures()[0]
        self.assertTrue(row["missing"])
        self.assertFalse(row["enriched"])

    def test_a_failure_with_no_session_is_still_recorded(self):
        """Ingest can fail on a transcript before it yields a session id."""
        self.st.record_parse_failure("claude", "unparsable-line", count=4)
        self.st.commit()
        row = self.st.recent_failures()[0]
        self.assertIsNone(row["session_id"])
        self.assertEqual(row["count"], 4)

    def test_newest_first(self):
        for i in range(4):
            self.st.record_parse_failure("enrich", f"c{i}", session_id=f"s{i}")
        self.st.commit()
        rows = self.st.recent_failures()
        self.assertEqual([r["category"] for r in rows], ["c3", "c2", "c1", "c0"])

    def test_it_can_be_filtered_by_source(self):
        self.st.record_parse_failure("enrich", "invalid-json", session_id="s1")
        self.st.record_parse_failure("claude", "bad-line")
        self.st.commit()
        rows = self.st.recent_failures(source="enrich")
        self.assertEqual([r["source"] for r in rows], ["enrich"])

    def test_the_log_is_bounded(self):
        """A pathological ingest must not grow the archive without bound."""
        limit = store.FAILURE_LOG_LIMIT
        for i in range(limit + 40):
            self.st.record_parse_failure("enrich", "invalid-json", session_id=f"s{i}")
        self.st.commit()
        kept = self.st.conn.execute(
            "SELECT COUNT(*) c FROM failure_log").fetchone()["c"]
        self.assertLessEqual(kept, limit + 1)
        # The newest survive; trimming from the wrong end would keep the
        # failures nobody can act on any more.
        newest = self.st.recent_failures(limit=1)[0]
        self.assertEqual(newest["session_id"], f"s{limit + 39}")

    def test_trimming_one_source_leaves_another_alone(self):
        self.st.record_parse_failure("claude", "bad-line")
        for i in range(store.FAILURE_LOG_LIMIT + 5):
            self.st.record_parse_failure("enrich", "invalid-json", session_id=f"s{i}")
        self.st.commit()
        self.assertEqual(len(self.st.recent_failures(source="claude")), 1)


if __name__ == "__main__":
    unittest.main()
