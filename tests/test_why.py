"""Decision-level blame (muninn/why.py).

The tests worth reading first are the ones about what `why` refuses to claim.
Attribution is a guess dressed as a fact unless it is bounded, and the bound
here came from a measurement: session length spans four orders of magnitude, so
"a session was open when this commit landed" is coincidence rather than
evidence. Most of this file exists to keep that door shut.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from muninn import store, why


def _session(st, session_id, *, started, ended, provenance="human",
             topic=None, outcome=None, decisions=None, files=(), source="claude"):
    facets = json.dumps({"decisions": list(decisions)}) if decisions else None
    st.conn.execute(
        "INSERT INTO sessions (session_id, source, provenance, started_at, "
        "ended_at, words, text, topic, outcome, facets_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, source, provenance, started, ended, 500, "prose",
         topic, outcome, facets))
    st.set_files(session_id, files)
    st.conn.commit()


class MatchingTests(unittest.TestCase):
    """What counts as an explanation, with git stubbed out."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-why-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.st = store.open_store(self.tmp / "a.db")
        self.addCleanup(self.st.close)
        self.repo = self.tmp / "repo"
        (self.repo / "pkg").mkdir(parents=True)
        self.target = self.repo / "pkg" / "thing.py"
        self.target.write_text("x = 1\n", encoding="utf-8")

    def _explain(self, changes):
        original_root, original_log = why.repo_root, why.commits_for
        why.repo_root = lambda _p: str(self.repo)
        why.commits_for = lambda *a, **k: list(changes)
        try:
            return why.explain(self.st, str(self.target))
        finally:
            why.repo_root, why.commits_for = original_root, original_log

    @staticmethod
    def _change(sha="abc1234", when="2026-08-10T12:00:00+00:00", subject="do a thing"):
        return why.Change(sha=sha, when=when, subject=subject, author="someone")

    def test_a_session_that_edited_the_file_explains_the_commit(self):
        _session(self.st, "s1", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00", topic="the retry fix",
                 outcome="fixed", decisions=["retry twice, then give up"],
                 files=[str(self.target)])
        found = self._explain([self._change()])
        attributed = found.changes[0].sessions
        self.assertEqual([a.session_id for a in attributed], ["s1"])
        self.assertEqual(attributed[0].confidence, why.TOUCHED_FILE)
        self.assertEqual(attributed[0].decisions, ("retry twice, then give up",))

    def test_merely_being_open_is_not_an_explanation(self):
        """The measurement that removed a whole confidence tier.

        Session length spans four orders of magnitude — a 12-minute median
        against a 27-day p95 — so a session parked for a month overlaps every
        commit made that month. At one arbitrary commit instant, eight sessions
        were 'open'. Offering them put three unrelated modding sessions under a
        change to `cost.py` on the first live run.
        """
        _session(self.st, "bystander", started="2026-07-01T00:00:00+00:00",
                 ended="2026-09-01T00:00:00+00:00", topic="something else",
                 files=[str(self.tmp / "elsewhere" / "other.py")])
        found = self._explain([self._change()])
        self.assertEqual(found.changes[0].sessions, ())

    def test_working_elsewhere_in_the_repo_is_the_weaker_claim(self):
        _session(self.st, "s2", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00",
                 files=[str(self.repo / "pkg" / "neighbour.py")])
        attributed = self._explain([self._change()]).changes[0].sessions
        self.assertEqual(attributed[0].confidence, why.TOUCHED_REPO)

    def test_the_strongest_claim_is_listed_first(self):
        _session(self.st, "weak", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00",
                 files=[str(self.repo / "pkg" / "neighbour.py")])
        _session(self.st, "strong", started="2026-08-10T11:30:00+00:00",
                 ended="2026-08-10T12:30:00+00:00", files=[str(self.target)])
        attributed = self._explain([self._change()]).changes[0].sessions
        self.assertEqual([a.session_id for a in attributed], ["strong", "weak"])

    def test_a_commit_outside_every_session_is_reported_unexplained(self):
        """Plenty of commits are written by hand; that is an answer."""
        _session(self.st, "s1", started="2026-08-01T00:00:00+00:00",
                 ended="2026-08-01T01:00:00+00:00", files=[str(self.target)])
        self.assertEqual(self._explain([self._change()]).changes[0].sessions, ())

    def test_a_tool_invoked_session_never_explains_anything(self):
        """A `claude -p` byproduct decided nothing."""
        _session(self.st, "robot", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00", provenance="tool-invoked",
                 files=[str(self.target)])
        self.assertEqual(self._explain([self._change()]).changes[0].sessions, ())

    def test_a_same_named_file_in_another_project_is_not_this_file(self):
        """A basename match would put someone else's decisions under your file.

        Worse than a miss: it arrives wearing the strongest confidence label.
        """
        other = self.tmp / "unrelated" / "pkg" / "thing.py"
        other.parent.mkdir(parents=True)
        _session(self.st, "elsewhere", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00", files=[str(other)])
        self.assertEqual(self._explain([self._change()]).changes[0].sessions, ())

    def test_the_same_file_under_a_different_checkout_root_still_matches(self):
        """Another machine, or another clone. The repo-relative tail is stable."""
        elsewhere = os.path.join("/home/someone/src/repo", "pkg", "thing.py")
        _session(self.st, "mac", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00", files=[elsewhere])
        attributed = self._explain([self._change()]).changes[0].sessions
        self.assertEqual([a.session_id for a in attributed], ["mac"])

    def test_work_with_no_commit_is_reported_separately(self):
        """Exploration and reverted attempts are invisible to git by construction."""
        _session(self.st, "explored", started="2026-08-01T00:00:00+00:00",
                 ended="2026-08-01T01:00:00+00:00", topic="tried something",
                 files=[str(self.target)])
        found = self._explain([])
        self.assertEqual([a.session_id for a in found.uncommitted], ["explored"])

    def test_a_session_credited_with_a_commit_is_not_also_listed_as_uncommitted(self):
        _session(self.st, "s1", started="2026-08-10T11:00:00+00:00",
                 ended="2026-08-10T13:00:00+00:00", files=[str(self.target)])
        found = self._explain([self._change()])
        self.assertEqual(found.uncommitted, ())

    def test_a_naive_timestamp_is_read_rather_than_discarded(self):
        """Sources differ on recording an offset; dropping them narrows this
        silently to whichever tools happen to be timezone-aware."""
        _session(self.st, "naive", started="2026-08-10T11:00:00",
                 ended="2026-08-10T13:00:00", files=[str(self.target)])
        attributed = self._explain([self._change()]).changes[0].sessions
        self.assertEqual([a.session_id for a in attributed], ["naive"])


class OutsideGitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-why-nogit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.st = store.open_store(self.tmp / "a.db")
        self.addCleanup(self.st.close)

    def test_a_file_outside_a_work_tree_says_so_and_still_answers(self):
        loose = self.tmp / "loose.txt"
        loose.write_text("hi", encoding="utf-8")
        original = why.repo_root
        why.repo_root = lambda _p: None
        try:
            found = why.explain(self.st, str(loose))
        finally:
            why.repo_root = original
        self.assertIn("commits", found.unavailable)
        self.assertIn("git work tree", found.unavailable["commits"])
        self.assertEqual(found.changes, ())

    def test_an_empty_result_distinguishes_its_two_reasons(self):
        """"No commits" and "no sessions recorded touching it" are different."""
        loose = self.tmp / "loose.txt"
        loose.write_text("hi", encoding="utf-8")
        original = why.repo_root
        why.repo_root = lambda _p: None
        try:
            found = why.explain(self.st, str(loose))
        finally:
            why.repo_root = original
        self.assertIn("sessions", found.unavailable)
        self.assertIn("tool calls", found.unavailable["sessions"])


class GitReadingTests(unittest.TestCase):
    """Against a real repository, because the plumbing is where this breaks."""

    @classmethod
    def setUpClass(cls):
        if shutil.which("git") is None:
            raise unittest.SkipTest("git is not installed")

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-why-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
               "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e"}
        run = lambda *a: subprocess.run(["git", "-C", str(self.tmp), *a],  # noqa: E731
                                        capture_output=True, env=env, check=True)
        run("init", "-q")
        self.file = self.tmp / "a.txt"
        # A subject with a character cp1252 cannot encode. `text=True` decodes
        # with the *locale* codec, and the resulting UnicodeDecodeError is
        # neither OSError nor SubprocessError, so it escapes every handler
        # written to catch a subprocess failing. This project has been bitten
        # by that exact class three times.
        self.file.write_text("one\n", encoding="utf-8")
        run("add", "a.txt")
        run("commit", "-qm", "first — em dash, ünicode")

    def test_the_commit_log_is_read_without_a_locale_decoding_error(self):
        changes = why.commits_for(str(self.tmp), str(self.file))
        self.assertEqual(len(changes), 1)
        self.assertIn("em dash", changes[0].subject)

    def test_the_work_tree_is_found_from_the_file(self):
        found = why.repo_root(str(self.file))
        self.assertIsNotNone(found)
        self.assertEqual(os.path.normcase(os.path.realpath(found)),
                         os.path.normcase(os.path.realpath(self.tmp)))

    def test_a_path_outside_any_work_tree_returns_none(self):
        outside = Path(tempfile.mkdtemp(prefix="muninn-why-outside-"))
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        loose = outside / "b.txt"
        loose.write_text("x", encoding="utf-8")
        # A temp dir can sit inside an unrelated checkout on some machines; the
        # assertion is only that it is not *this* one.
        found = why.repo_root(str(loose))
        if found is not None:
            self.assertNotEqual(os.path.normcase(os.path.realpath(found)),
                                os.path.normcase(os.path.realpath(self.tmp)))

    def test_a_filename_can_never_be_read_as_an_option(self):
        """Every path argument goes after a literal `--`."""
        source = Path(why.__file__).read_text(encoding="utf-8")
        self.assertIn('"--", path', source)


if __name__ == "__main__":
    unittest.main()
