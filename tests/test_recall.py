"""Recall: the archive volunteering what you already know (muninn/recall.py).

Every other retrieval path waits to be asked, which is the wrong shape for the
material a person has forgotten they have — you do not search for it, because
you do not know it is there. Recall takes a *place* instead of a question.

The tests worth reading first are the ones about what recall declines to do:
it distinguishes "nothing to say" from "cannot say", it will not pad a section
with tool-invoked byproducts, and it keeps the expensive half out of the menu.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import raven, recall, store


def _session(st, session_id, *, cwd, started, provenance="human",
             outcome=None, topic=None, words=5000, source="claude"):
    st.conn.execute(
        "INSERT INTO sessions (session_id, source, provenance, cwd, started_at, "
        "words, text, topic, outcome) VALUES (?,?,?,?,?,?,?,?,?)",
        (session_id, source, provenance, cwd, started, words, "prose", topic, outcome))
    st.conn.commit()


class RecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-recall-"))
        self.st = store.open_store(self.tmp / "a.db")

    def tearDown(self) -> None:
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── where "now" is ───────────────────────────────────────────────────────

    def test_the_current_repo_is_the_most_recent_sessions(self):
        """Muninn ingests continuously, so it already knows this.

        It does not ask Huginn, and deliberately cannot: the raven protocol
        forbids one raven presenting another's credential, and reading Huginn's
        API would mean doing exactly that.
        """
        _session(self.st, "a", cwd="/w/old", started="2026-01-01T00:00:00Z")
        _session(self.st, "b", cwd="/w/current", started="2026-08-01T00:00:00Z")
        self.assertEqual(recall.current_repo(self.st), "current")

    def test_tool_invoked_work_is_not_where_you_are_working(self):
        """A `claude -p` byproduct is not a place a person is sitting."""
        _session(self.st, "a", cwd="/w/real", started="2026-01-01T00:00:00Z")
        _session(self.st, "b", cwd="/w/robot", started="2026-08-01T00:00:00Z",
                 provenance="tool-invoked")
        self.assertEqual(recall.current_repo(self.st), "real")

    def test_an_empty_archive_has_no_current_repo(self):
        self.assertIsNone(recall.current_repo(self.st))

    # ── unfinished ───────────────────────────────────────────────────────────

    def test_unfinished_means_ongoing_or_abandoned(self):
        for i, outcome in enumerate(("ongoing", "abandoned", "fixed", "exploratory")):
            _session(self.st, f"s{i}", cwd="/w/repo", started=f"2026-08-0{i+1}T00:00:00Z",
                     outcome=outcome, topic=outcome)
        found = {r.outcome for r in recall.unfinished(self.st, "repo")}
        self.assertEqual(found, {"ongoing", "abandoned"})

    def test_a_finished_exploration_is_not_a_loose_end(self):
        """Otherwise the real loose ends drown in everything ever poked at."""
        _session(self.st, "a", cwd="/w/repo", started="2026-08-01T00:00:00Z",
                 outcome="exploratory")
        self.assertEqual(recall.unfinished(self.st, "repo"), [])

    def test_unfinished_is_scoped_to_the_repository(self):
        _session(self.st, "here", cwd="/w/repo", started="2026-08-01T00:00:00Z",
                 outcome="ongoing")
        _session(self.st, "there", cwd="/w/other", started="2026-08-02T00:00:00Z",
                 outcome="ongoing")
        self.assertEqual([r.session_id for r in recall.unfinished(self.st, "repo")],
                         ["here"])

    def test_newest_first(self):
        _session(self.st, "old", cwd="/w/repo", started="2026-01-01T00:00:00Z",
                 outcome="ongoing")
        _session(self.st, "new", cwd="/w/repo", started="2026-08-01T00:00:00Z",
                 outcome="ongoing")
        self.assertEqual([r.session_id for r in recall.unfinished(self.st, "repo")],
                         ["new", "old"])

    # ── the distinction that matters ─────────────────────────────────────────

    def test_nothing_unfinished_is_reported_differently_from_cannot_know(self):
        """An empty list means two opposite things, and the difference is the point.

        With no enrichment, *every* session has a null outcome, so "no
        unfinished threads" is indistinguishable from "no session has ever been
        judged". Reporting the first when the second is true tells the user
        their loose ends are handled when nothing has looked.
        """
        _session(self.st, "a", cwd="/w/repo", started="2026-08-01T00:00:00Z")
        unenriched = recall.recall(self.st, repo="repo")
        self.assertIn("unfinished", unenriched.unavailable)
        self.assertIn("enrichment has not run", unenriched.unavailable["unfinished"])

        _session(self.st, "b", cwd="/w/repo", started="2026-08-02T00:00:00Z",
                 outcome="fixed")
        enriched = recall.recall(self.st, repo="repo")
        self.assertEqual(enriched.unfinished, [])
        self.assertNotIn("unfinished", enriched.unavailable)

    def test_no_embedding_provider_is_stated_not_silently_skipped(self):
        _session(self.st, "a", cwd="/w/repo", started="2026-08-01T00:00:00Z")
        found = recall.recall(self.st, repo="repo", model=None)
        self.assertIn("related", found.unavailable)

    # ── prior work ───────────────────────────────────────────────────────────

    def test_prior_work_does_not_repeat_the_unfinished_list(self):
        """The same session under two headings reads as two pieces of work."""
        _session(self.st, "loose", cwd="/w/repo", started="2026-08-02T00:00:00Z",
                 outcome="ongoing")
        _session(self.st, "done", cwd="/w/repo", started="2026-08-01T00:00:00Z",
                 outcome="fixed")
        found = recall.recall(self.st, repo="repo")
        self.assertEqual([r.session_id for r in found.unfinished], ["loose"])
        self.assertEqual([r.session_id for r in found.prior], ["done"])

    def test_tool_invoked_sessions_are_never_recalled(self):
        _session(self.st, "robot", cwd="/w/repo", started="2026-08-02T00:00:00Z",
                 provenance="tool-invoked")
        _session(self.st, "human", cwd="/w/repo", started="2026-08-01T00:00:00Z")
        found = recall.recall(self.st, repo="repo")
        self.assertEqual([r.session_id for r in found.prior], ["human"])

    def test_recall_defaults_to_where_the_work_is(self):
        _session(self.st, "a", cwd="/w/elsewhere", started="2026-01-01T00:00:00Z")
        _session(self.st, "b", cwd="/w/here", started="2026-08-01T00:00:00Z")
        self.assertEqual(recall.recall(self.st).repo, "here")


class MenuTests(unittest.TestCase):
    """The proactive half: Roost renders whatever Muninn's menu says."""

    def _rows(self, **kw):
        menu = raven.build_menu(recent=[], sessions=1, chunks=1, **kw)
        return {s["id"]: s for s in menu["sections"]}

    def test_unfinished_threads_reach_the_menu(self):
        sections = self._rows(
            unfinished=[{"session_id": "a" * 32, "topic": "the flaky retry",
                         "outcome": "ongoing", "started_at": "2026-08-01T00:00:00Z"}],
            unfinished_repo="huginn")
        self.assertIn("unfinished", sections)
        self.assertIn("huginn", sections["unfinished"]["title"])
        self.assertEqual(len(sections["unfinished"]["items"]), 1)

    def test_the_section_is_silent_when_there_is_nothing_to_say(self):
        """A permanent reassuring row is how a menu teaches people to skip it."""
        self.assertNotIn("unfinished", self._rows(unfinished=[]))
        self.assertNotIn("unfinished", self._rows())

    def test_it_comes_before_recent(self):
        """It is the only section here that asks for something."""
        menu = raven.build_menu(
            recent=[{"session_id": "b" * 32, "topic": "x",
                     "started_at": "2026-08-01T00:00:00Z", "source": "claude"}],
            sessions=1, chunks=1,
            unfinished=[{"session_id": "a" * 32, "topic": "loose end",
                         "outcome": "ongoing", "started_at": "2026-08-01T00:00:00Z"}])
        ids = [s["id"] for s in menu["sections"]]
        self.assertLess(ids.index("unfinished"), ids.index("recent"))

    def test_a_hostile_session_id_never_reaches_a_url(self):
        """Same constraint every other id in this menu is held to."""
        sections = self._rows(
            unfinished=[{"session_id": "../../etc/passwd", "topic": "x",
                         "outcome": "ongoing", "started_at": "2026-08-01T00:00:00Z"}])
        self.assertNotIn("unfinished", sections)

    def test_the_menu_is_capped(self):
        rows = [{"session_id": f"{i:032d}", "topic": f"t{i}", "outcome": "ongoing",
                 "started_at": "2026-08-01T00:00:00Z"} for i in range(20)]
        sections = self._rows(unfinished=rows)
        self.assertEqual(len(sections["unfinished"]["items"]), raven.UNFINISHED_LIMIT)


class CliTests(unittest.TestCase):
    """The command itself: exit code, JSON shape, and the empty case."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-recall-cli-"))
        self.db = self.tmp / "a.db"
        self.st = store.open_store(self.db)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv: str) -> tuple[int, str]:
        from muninn import cli
        self.st.close()          # the command opens its own connection
        parser = cli.build_parser()
        args = parser.parse_args(["--db", str(self.db), "recall", *argv])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = args.func(args)
        return rc, buf.getvalue()

    def test_json_is_one_object_with_a_section_per_kind(self):
        _session(self.st, "loose", cwd="/w/repo", started="2026-08-01T00:00:00Z",
                 outcome="ongoing", topic="the flaky retry")
        rc, out = self._run("--repo", "repo", "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(set(payload), {"repo", "unfinished", "prior", "related",
                                        "unavailable"})
        self.assertEqual(payload["unfinished"][0]["topic"], "the flaky retry")

    def test_an_empty_archive_is_not_an_error(self):
        """Nothing to recall is an answer, not a failure."""
        rc, out = self._run("--repo", "nowhere")
        self.assertEqual(rc, 0)
        self.assertTrue(out.strip())


if __name__ == "__main__":
    unittest.main()
