"""Acceptance tests for spec 004 (structured filters and search quality).

Each fixture archive is built to differ in exactly one dimension so a test
failure is unambiguous about which filter broke — "fixtures encode what you
expect" (docs/specs/README.md), so the fixtures here are deliberately
minimal and named for the one thing they isolate.

Sessions are inserted directly via the Store API rather than through
sources.py's parsers: spec 004 is about the filter/ranking layer sitting on
top of the schema, not about transcript parsing (that is covered by
tests/test_losslessness.py), so bypassing the parser keeps each fixture
exactly as narrow as the dimension it is testing.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import cli, store
from muninn.query import Filters, MAX_EXPANSION_TERMS, expand_terms, parse_date_prefix


def make_session(st: store.Store, session_id: str, text: str = "placeholder prose", **overrides) -> None:
    """Insert one fully-formed session row plus its chunks, minimal ceremony.

    Defaults describe an ordinary human Claude Code session; pass overrides
    for the one field a given test wants to vary.
    """
    rec = {
        "session_id": session_id,
        "source": "claude",
        "provenance": "human",
        "parent_id": None,
        "cwd": "/tmp/project",
        "branch": "main",
        "model": "claude-sonnet-5",
        "title": None,
        "started_at": "2026-07-15T00:00:00",
        "ended_at": "2026-07-15T00:05:00",
        "duration_s": 300.0,
        "user_turns": 1,
        "assistant_turns": 1,
        "tool_uses": 0,
        "tool_results": 0,
        "words": len(text.split()),
        "tokens": None,
        "text": text,
        "source_path": None,
        "source_present": 1,
        "origin": "raw",
        "ingested_at": "2026-07-15T00:05:00",
        "updated_at": "2026-07-15T00:05:00",
    }
    rec.update(overrides)
    st.upsert_session(rec)
    st.replace_chunks(session_id, text)


class TempArchiveTest(unittest.TestCase):
    """Shared tempdir/db plumbing. Never the real archive (CLAUDE.md, "Don't")."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-query-"))
        self.db = self.tmp / "muninn.db"
        self.st = store.open_store(self.db)

    def tearDown(self) -> None:
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# -- 1. Each filter alone narrows correctly ---------------------------------


class SingleFilterTest(TempArchiveTest):
    """One session matches the filter value, one differs in only that dimension."""

    def test_repo_filter(self) -> None:
        make_session(self.st, "match", "shared search term", cwd="/tmp/muninn")
        make_session(self.st, "other", "shared search term", cwd="/tmp/elsewhere")
        hits = self.st.search("shared search term", filters=Filters(repo="muninn"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_repo_filter_is_case_insensitive_substring(self) -> None:
        make_session(self.st, "match", "shared search term", cwd="/tmp/MuninnConsole")
        hits = self.st.search("shared search term", filters=Filters(repo="muninn"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_branch_filter(self) -> None:
        make_session(self.st, "match", "shared search term", branch="feature/filters")
        make_session(self.st, "other", "shared search term", branch="main")
        hits = self.st.search("shared search term", filters=Filters(branch="feature/filters"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_tool_filter(self) -> None:
        self.st.set_tools("match", {"Read": 3})
        self.st.set_tools("other", {"Bash": 1})
        make_session(self.st, "match", "shared search term")
        make_session(self.st, "other", "shared search term")
        hits = self.st.search("shared search term", filters=Filters(tool="read"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_model_filter(self) -> None:
        make_session(self.st, "match", "shared search term", model="claude-opus-5")
        make_session(self.st, "other", "shared search term", model="claude-sonnet-5")
        hits = self.st.search("shared search term", filters=Filters(model="opus"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_source_filter(self) -> None:
        make_session(self.st, "match", "shared search term", source="codex")
        make_session(self.st, "other", "shared search term", source="claude")
        hits = self.st.search("shared search term", filters=Filters(source="codex"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_since_filter(self) -> None:
        make_session(self.st, "match", "shared search term", started_at="2026-07-20T00:00:00")
        make_session(self.st, "other", "shared search term", started_at="2026-06-01T00:00:00")
        hits = self.st.search("shared search term", filters=Filters(since="2026-07-15"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_until_filter(self) -> None:
        make_session(self.st, "match", "shared search term", started_at="2026-06-01T00:00:00")
        make_session(self.st, "other", "shared search term", started_at="2026-07-20T00:00:00")
        hits = self.st.search("shared search term", filters=Filters(until="2026-06-15"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_provenance_filter_explicit(self) -> None:
        make_session(self.st, "match", "shared search term", provenance="subagent")
        make_session(self.st, "other", "shared search term", provenance="human")
        hits = self.st.search("shared search term", filters=Filters(provenance="subagent"))
        self.assertEqual({h["session_id"] for h in hits}, {"match"})

    def test_outcome_flag_parses_and_filters_without_error(self) -> None:
        """--outcome is wired but matches nothing until spec 005 enrichment lands.

        sessions.outcome is nullable and never populated by this spec, so the
        correct, expected behavior today is zero rows — not an exception. See
        docs/specs/004, "Out of scope".
        """
        make_session(self.st, "a", "shared search term")
        hits = self.st.search("shared search term", filters=Filters(outcome="fixed"))
        self.assertEqual(hits, [])


# -- 4. --file matches basename and suffix, aligned on a path boundary -----


class FileFilterTest(TempArchiveTest):
    def setUp(self) -> None:
        super().setUp()
        make_session(self.st, "s", "shared search term")
        self.st.set_files("s", ["/x/y/auth.py"])

    def test_basename_matches(self) -> None:
        hits = self.st.search("shared search term", filters=Filters(file="auth.py"))
        self.assertEqual({h["session_id"] for h in hits}, {"s"})

    def test_suffix_aligned_on_segment_boundary_matches(self) -> None:
        hits = self.st.search("shared search term", filters=Filters(file="y/auth.py"))
        self.assertEqual({h["session_id"] for h in hits}, {"s"})

    def test_unaligned_literal_suffix_does_not_match(self) -> None:
        """'th.py' is a literal suffix of 'auth.py' but not on a path boundary."""
        hits = self.st.search("shared search term", filters=Filters(file="th.py"))
        self.assertEqual(hits, [])


# -- 2. Composition ----------------------------------------------------------


class CompositionTest(TempArchiveTest):
    def test_repo_since_and_text_all_and_together(self) -> None:
        make_session(self.st, "all_three", "irreplaceable knowledge here",
                    cwd="/tmp/muninn", started_at="2026-07-20T00:00:00")
        # Matches repo + text but not the date.
        make_session(self.st, "two_of_three", "irreplaceable knowledge here",
                    cwd="/tmp/muninn", started_at="2026-06-01T00:00:00")
        # Matches date + text but not the repo.
        make_session(self.st, "also_two", "irreplaceable knowledge here",
                    cwd="/tmp/other", started_at="2026-07-20T00:00:00")

        hits = self.st.search(
            "irreplaceable knowledge",
            filters=Filters(repo="muninn", since="2026-07-15"),
        )
        self.assertEqual({h["session_id"] for h in hits}, {"all_three"})


# -- 3. Date prefixes ---------------------------------------------------------


class DatePrefixTest(unittest.TestCase):
    def test_year_prefix(self) -> None:
        start, end = parse_date_prefix("2026")
        self.assertTrue(start.startswith("2026-01-01"))
        self.assertTrue(end.startswith("2026-12-31"))

    def test_month_prefix(self) -> None:
        start, end = parse_date_prefix("2026-07")
        self.assertTrue(start.startswith("2026-07-01"))
        self.assertTrue(end.startswith("2026-07-31"))

    def test_day_prefix(self) -> None:
        start, end = parse_date_prefix("2026-07-31")
        self.assertTrue(start.startswith("2026-07-31"))
        self.assertTrue(end.startswith("2026-07-31"))

    def test_since_month_includes_first_of_month_excludes_prior_day(self) -> None:
        with tempfile.TemporaryDirectory(prefix="muninn-date-") as tmp:
            st = store.open_store(Path(tmp) / "m.db")
            make_session(st, "included", "shared search term", started_at="2026-07-01T00:00:00")
            make_session(st, "excluded", "shared search term", started_at="2026-06-30T23:59:59")
            hits = st.search("shared search term", filters=Filters(since="2026-07"))
            st.close()
        self.assertEqual({h["session_id"] for h in hits}, {"included"})


# -- 5/6. Provenance defaults -------------------------------------------------


class ProvenanceDefaultTest(TempArchiveTest):
    def test_tool_invoked_excluded_by_default(self) -> None:
        make_session(self.st, "tool", "shared search term", provenance="tool-invoked")
        hits = self.st.search("shared search term")
        self.assertEqual(hits, [])

    def test_tool_invoked_included_with_explicit_flag(self) -> None:
        make_session(self.st, "tool", "shared search term", provenance="tool-invoked")
        hits = self.st.search("shared search term", filters=Filters(provenance="tool-invoked"))
        self.assertEqual({h["session_id"] for h in hits}, {"tool"})

    def test_subagents_searched_by_default(self) -> None:
        make_session(self.st, "sub", "shared search term", provenance="subagent")
        hits = self.st.search("shared search term")
        self.assertEqual({h["session_id"] for h in hits}, {"sub"})


# -- 7. Expansion cap ---------------------------------------------------------


class ExpansionCapTest(unittest.TestCase):
    def test_ten_word_query_expands_to_at_most_the_cap(self) -> None:
        match = store.fts_query(
            "one two three four five six seven eight nine ten")
        expanded = expand_terms(match)
        or_terms = [t for t in expanded.split(" OR ") if t]
        self.assertLessEqual(len(or_terms), MAX_EXPANSION_TERMS)
        self.assertEqual(MAX_EXPANSION_TERMS, 4, "guardrail: do not raise this cap")


# -- 8. Dedup fills the limit -------------------------------------------------


class DedupTest(TempArchiveTest):
    def test_one_session_with_five_chunk_hits_does_not_starve_the_limit(self) -> None:
        # One session's text is split into 5 chunks, all matching; four other
        # sessions match once each. --limit 5 must return 5 SESSIONS, not one
        # session repeated for each of its chunk hits.
        big_text = " ".join([f"filler{i} needle" for i in range(5)])
        make_session(self.st, "big", big_text, words=100)
        # Force 5 distinct chunks for "big" by inserting them directly —
        # replace_chunks() would otherwise merge short text into one window.
        self.st.conn.execute("DELETE FROM chunks WHERE session_id = 'big'")
        for i in range(5):
            self.st.conn.execute(
                "INSERT INTO chunks (session_id, ordinal, body) VALUES (?, ?, ?)",
                ("big", i, f"needle chunk {i}"))
        for i in range(4):
            make_session(self.st, f"small{i}", "needle appears once here")

        hits = self.st.search("needle", limit=5)
        self.assertEqual(len(hits), 5)
        self.assertEqual(len({h["session_id"] for h in hits}), 5)
        big_hit = next(h for h in hits if h["session_id"] == "big")
        self.assertEqual(big_hit["chunk_hits"], 5)


# -- 9. Ranking tiebreak -------------------------------------------------------


class RankingTiebreakTest(TempArchiveTest):
    def test_equal_bm25_breaks_by_newest_first(self) -> None:
        # Identical bodies -> identical bm25 scores.
        make_session(self.st, "older", "identical ranking text", started_at="2026-01-01T00:00:00")
        make_session(self.st, "newer", "identical ranking text", started_at="2026-07-01T00:00:00")
        hits = self.st.search("identical ranking text")
        self.assertEqual(hits[0]["score"], hits[1]["score"])
        self.assertEqual([h["session_id"] for h in hits], ["newer", "older"])


# -- 10. SQL injection is inert ------------------------------------------------


class SqlInjectionTest(TempArchiveTest):
    def test_prose_containing_sql_is_searchable_and_tables_survive(self) -> None:
        payload = "'; DROP TABLE sessions; --"
        make_session(self.st, "s", f"unique_injection_marker {payload}")
        hits = self.st.search("unique_injection_marker")
        self.assertEqual({h["session_id"] for h in hits}, {"s"})
        self.assertEqual(self.st.count_sessions(), 1, "sessions table must survive")

    def test_filter_values_containing_sql_do_not_affect_query(self) -> None:
        make_session(self.st, "s", "shared search term", cwd="/tmp/project")
        payload = "x'; DROP TABLE sessions; --"
        hits = self.st.search("shared search term", filters=Filters(repo=payload))
        self.assertEqual(hits, [])
        self.assertEqual(self.st.count_sessions(), 1, "sessions table must survive")


# -- 11. Empty and operator-only queries ---------------------------------------


class DegenerateQueryTest(TempArchiveTest):
    def test_empty_query_returns_empty_not_raises(self) -> None:
        self.assertEqual(self.st.search(""), [])

    def test_bare_and_returns_empty_not_raises(self) -> None:
        self.assertEqual(self.st.search("AND"), [])

    def test_bare_star_returns_empty_not_raises(self) -> None:
        self.assertEqual(self.st.search("*"), [])

    def test_bare_near_operator_returns_empty_not_raises(self) -> None:
        self.assertEqual(self.st.search("NEAR/10"), [])


# -- 12. --json shape -----------------------------------------------------------


class JsonShapeTest(TempArchiveTest):
    def test_json_output_has_stable_keys_one_object_per_session(self) -> None:
        make_session(self.st, "a", "shared search term")
        make_session(self.st, "b", "shared search term")
        rows = self.st.search("shared search term")
        expected_keys = {"session_id", "source", "provenance", "started_at",
                         "cwd", "words", "excerpt", "score", "chunk_hits"}
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(set(row.keys()), expected_keys)

    def test_cli_json_flag_emits_a_json_array(self) -> None:
        make_session(self.st, "a", "shared search term")
        self.st.close()  # cmd_search opens its own connection to self.db

        import contextlib
        import io
        parser = cli.build_parser()
        args = parser.parse_args(["--db", str(self.db), "search", "shared search term", "--json"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertIsInstance(payload, list)
        self.assertEqual(payload[0]["session_id"], "a")
        # Re-open so tearDown's self.st.close() does not double-close.
        self.st = store.open_store(self.db)


# -- 13. muninn log -------------------------------------------------------------


class LogTest(TempArchiveTest):
    def test_reverse_chronological(self) -> None:
        make_session(self.st, "old", "x", started_at="2026-01-01T00:00:00")
        make_session(self.st, "new", "x", started_at="2026-07-01T00:00:00")
        rows = self.st.log()
        self.assertEqual([r["session_id"] for r in rows], ["new", "old"])

    def test_respects_repo_filter(self) -> None:
        make_session(self.st, "keep", "x", cwd="/tmp/muninn", started_at="2026-07-01T00:00:00")
        make_session(self.st, "drop", "x", cwd="/tmp/other", started_at="2026-07-02T00:00:00")
        rows = self.st.log(filters=Filters(repo="muninn"))
        self.assertEqual([r["session_id"] for r in rows], ["keep"])

    def test_respects_since_filter(self) -> None:
        make_session(self.st, "keep", "x", started_at="2026-07-15T00:00:00")
        make_session(self.st, "drop", "x", started_at="2026-06-01T00:00:00")
        rows = self.st.log(filters=Filters(since="2026-07"))
        self.assertEqual([r["session_id"] for r in rows], ["keep"])

    def test_cli_log_subcommand_runs(self) -> None:
        make_session(self.st, "a", "x")
        self.st.close()

        import contextlib
        import io
        parser = cli.build_parser()
        args = parser.parse_args(["--db", str(self.db), "log", "--limit", "5"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = args.func(args)
        self.assertEqual(rc, 0)
        self.assertIn("a"[:8], buf.getvalue())
        self.st = store.open_store(self.db)


if __name__ == "__main__":
    unittest.main()
