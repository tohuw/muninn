"""Conversation names, carried from ingest all the way out to the CLI.

Muninn has always *stored* a session's title -- claude.ai calls it the
conversation name, codexdex writes it as a ``title`` header -- and then never
showed it to anyone. The archive knew the session as "Neurospicy 3"; every
command printed the enrichment-derived topic instead, so the one handle a
person actually uses to refer to a conversation was unreachable through the
tool boundary. Reading it required opening the SQLite file directly, which is
precisely what the agent-facing contract tells callers not to do.

These tests pin the two halves of the fix that can regress independently:

1. **The field survives the query layer.** ``search`` and ``log`` project it,
   ``recall`` and ``why`` carry it on their records, and every ``--json``
   payload has the key -- present and ``None`` rather than absent, so a parser
   never has to guess whether a missing key means "no title" or "old Muninn".
2. **Title and topic never contend for one slot.** They answer different
   questions and a row shows both. A title is how somebody finds a
   conversation again; a topic is what it turned out to be about.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import cli, recall, store


def _session(st: store.Store, session_id: str, *, title=None, topic=None,
             text="shared search term", cwd="/w/proj",
             started="2026-05-15T00:00:00Z", provenance="human",
             source="claude-cloud") -> None:
    st.upsert_session({
        "session_id": session_id, "source": source, "provenance": provenance,
        "parent_id": None, "cwd": cwd, "branch": None, "model": None,
        "title": title, "started_at": started, "ended_at": started,
        "duration_s": 0.0, "user_turns": 1, "assistant_turns": 1,
        "tool_uses": 0, "tool_results": 0, "words": len(text.split()),
        "tokens": None, "text": text, "source_path": None, "source_present": 1,
        "origin": "raw", "ingested_at": started, "updated_at": started,
    })
    st.replace_chunks(session_id, text)
    if topic is not None:
        st.conn.execute("UPDATE sessions SET topic = ? WHERE session_id = ?",
                        (topic, session_id))
        st.conn.commit()


def _run(db: Path, argv: list[str]) -> tuple[int, str]:
    """Drive the real CLI in-process and capture stdout.

    In-process rather than subprocess: the Windows CI runner wedges on the
    latter (see tests/test_query.py), and the property under test is the
    command's output contract, not process spawning.
    """
    args = cli.build_parser().parse_args(["--db", str(db), *argv])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = args.func(args)
    return rc, buf.getvalue()


class TitleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-titles-"))
        self.db = self.tmp / "a.db"
        self.st = store.open_store(self.db)

    def tearDown(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the query layer ----------------------------------------------------

    def test_search_projects_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3")
        hits = self.st.search("shared search term")
        self.assertEqual(hits[0]["title"], "Neurospicy 3")

    def test_log_projects_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3")
        self.assertEqual(self.st.log()[0]["title"], "Neurospicy 3")

    def test_an_untitled_session_reports_none_not_a_missing_key(self) -> None:
        """Absent and null are different answers; only one is honest here.

        Codex rollouts and Claude Code sessions carry no conversation name at
        all. A caller must be able to tell "this source has no titles" from
        "this Muninn predates titles", and a dropped key cannot say that.
        """
        _session(self.st, "a", title=None)
        self.assertIn("title", self.st.search("shared search term")[0].keys())
        self.assertIsNone(self.st.search("shared search term")[0]["title"])
        self.assertIsNone(self.st.log()[0]["title"])

    # -- the JSON boundary --------------------------------------------------

    def test_search_json_carries_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3")
        self.st.close()
        rc, out = _run(self.db, ["search", "shared search term", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)[0]["title"], "Neurospicy 3")

    def test_log_json_carries_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3")
        self.st.close()
        rc, out = _run(self.db, ["log", "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)[0]["title"], "Neurospicy 3")

    def test_recall_json_carries_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3", cwd="/w/proj")
        found = recall.recall(self.st, repo="proj")
        rows = found.prior + found.unfinished
        self.assertTrue(rows, "fixture should land in some recall section")
        self.assertEqual(rows[0].title, "Neurospicy 3")
        self.assertEqual(rows[0].to_dict()["title"], "Neurospicy 3")

    # -- the human output ---------------------------------------------------

    def test_show_names_the_conversation_in_its_header(self) -> None:
        _session(self.st, "abc12345", title="Neurospicy 3")
        self.st.close()
        rc, out = _run(self.db, ["show", "abc12345"])
        self.assertEqual(rc, 0)
        self.assertIn("# title: Neurospicy 3", out)

    def test_log_shows_the_title_and_the_topic_together(self) -> None:
        """Neither one is a substitute for the other, so the row carries both."""
        _session(self.st, "a", title="Neurospicy 3",
                 topic="Late-identified adult autism diagnosis")
        self.st.close()
        rc, out = _run(self.db, ["log"])
        self.assertEqual(rc, 0)
        self.assertIn('"Neurospicy 3"', out)
        self.assertIn("Late-identified adult autism diagnosis", out)

    def test_search_shows_the_title(self) -> None:
        _session(self.st, "a", title="Neurospicy 3")
        self.st.close()
        rc, out = _run(self.db, ["search", "shared search term"])
        self.assertEqual(rc, 0)
        self.assertIn('"Neurospicy 3"', out)

    def test_an_untitled_session_prints_no_empty_quotes(self) -> None:
        _session(self.st, "a", title=None, topic="Something derived")
        self.st.close()
        rc, out = _run(self.db, ["log"])
        self.assertEqual(rc, 0)
        self.assertNotIn('""', out)
        self.assertIn("Something derived", out)

    def test_a_multiline_title_cannot_tear_the_row(self) -> None:
        """Titles are transcript text, and transcript text contains anything.

        A newline mid-title would split one session across two output lines and
        make the second look like a row of its own. Collapsing whitespace is
        the cheapest fix that keeps the name readable.
        """
        _session(self.st, "a", title="Neurospicy 3\nand a second line")
        self.st.close()
        rc, out = _run(self.db, ["log"])
        self.assertEqual(rc, 0)
        rows = [ln for ln in out.splitlines() if ln.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn('"Neurospicy 3 and a second line"', rows[0])


if __name__ == "__main__":
    unittest.main()
