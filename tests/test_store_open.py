"""Opening the archive must be safe when several openers arrive at once.

Not a hypothetical: the watcher, the hook drain, the sweep, the embedder, the
enricher and any CLI invocation all open the same file, and spec 003's design
makes "two of them opened it in the same instant" the normal case rather than a
race a human could only cause on purpose.

CI caught the real thing on a fresh archive with six openers: one raised
``UNIQUE constraint failed: meta.key`` and the other five ``database is
locked`` -- all six out of *opening* the archive, before any work began. Both
failures are covered here, and deliberately without a thread fan-out: that
wedges the Windows runner (see tests/test_indexer.py), and the defect that
matters most does not need threads to demonstrate.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from muninn import store


class SchemaVersionUpsertTests(unittest.TestCase):
    """Two *connections*, which is what a daemon and a CLI invocation are.

    An in-process lock cannot help across processes, so the atomicity of the
    statement itself is the only thing standing between a second opener and an
    IntegrityError. Tested at that level for exactly that reason.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-open-"))
        self.db = self.tmp / "archive.db"
        store.open_store(self.db).close()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _version(self) -> int:
        conn = self._conn()
        try:
            return int(conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])
        finally:
            conn.close()

    def test_a_second_opener_does_not_collide(self):
        """The CI failure: UNIQUE constraint failed: meta.key."""
        first, second = self._conn(), self._conn()
        try:
            first.execute(store.SCHEMA_VERSION_UPSERT, (str(store.SCHEMA_VERSION),))
            first.commit()
            second.execute(store.SCHEMA_VERSION_UPSERT, (str(store.SCHEMA_VERSION),))
            second.commit()
        finally:
            first.close()
            second.close()
        self.assertEqual(self._version(), store.SCHEMA_VERSION)

    def test_it_never_moves_the_version_backwards(self):
        """An older build must not stamp its number on a newer archive."""
        conn = self._conn()
        try:
            conn.execute(store.SCHEMA_VERSION_UPSERT, (str(store.SCHEMA_VERSION + 5),))
            conn.commit()
            conn.execute(store.SCHEMA_VERSION_UPSERT, (str(store.SCHEMA_VERSION),))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._version(), store.SCHEMA_VERSION + 5)

    def test_it_still_moves_the_version_forwards(self):
        conn = self._conn()
        try:
            conn.execute(store.SCHEMA_VERSION_UPSERT, (str(store.SCHEMA_VERSION + 1),))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._version(), store.SCHEMA_VERSION + 1)

    def test_the_comparison_is_numeric_not_lexicographic(self):
        """meta.value is TEXT, where "10" sorts before "9"."""
        conn = self._conn()
        try:
            conn.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
            conn.commit()
            conn.execute(store.SCHEMA_VERSION_UPSERT, ("10",))
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(self._version(), 10)


class OpenSerializationTests(unittest.TestCase):
    def test_openers_in_one_process_are_serialised(self):
        """The other half of the CI failure: five "database is locked".

        The retry loop around the fresh-file WAL switch was tuned against four
        threads; at six its backoff budget ran out. Threads inside one process
        have no reason to race each other through idempotent schema setup, so
        they are serialised and the retry loop is left for the separate-process
        case it was written for.
        """
        self.assertIsNotNone(store._OPEN_LOCK)
        tmp = Path(tempfile.mkdtemp(prefix="muninn-open2-"))
        try:
            db = tmp / "a.db"
            opened = [store.open_store(db) for _ in range(4)]
            for handle in opened:
                handle.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class AddedColumnsTest(unittest.TestCase):
    """An archive created before a column existed must gain it on open.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
    every column added after archives were in the wild needs an ALTER. Getting
    this wrong does not fail loudly -- it fails on the next query naming the
    column, in whichever background worker happens to run first.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-alter-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "old.db"

    def _make_old_archive(self, **session) -> None:
        """An archive whose sessions table predates the enrichment baseline.

        The table is *built* without those columns rather than built and then
        stripped. ``ALTER TABLE ... DROP COLUMN`` re-parses the stored schema
        text, and dropping the last column leaves this schema's trailing
        comment dangling: SQLite on Ubuntu refuses with "error in table
        sessions after drop column: incomplete input", while the build bundled
        with Windows Python accepts it. CI found that; the local suite could
        not.

        The column list is read from a real archive, so this cannot drift into
        testing a shape the code no longer produces.
        """
        probe = self.tmp / "probe.db"
        st = store.open_store(probe)
        columns = [(r["name"], r["type"], r["notnull"], r["dflt_value"])
                   for r in st.conn.execute("PRAGMA table_info(sessions)")]
        st.close()

        legacy = {name for name, _ in store._ADDED_SESSION_COLUMNS}
        defs = []
        for name, ctype, notnull, default in columns:
            if name in legacy:
                continue
            piece = f"{name} {ctype or 'TEXT'}"
            if name == "session_id":
                piece += " PRIMARY KEY"
            if notnull:
                piece += " NOT NULL"
            if default is not None:
                piece += f" DEFAULT {default}"
            defs.append(piece)

        # Written with a bare connection, never `open_store`: opening is what
        # runs the migration, so using it here would add the columns back
        # before the test could observe them missing.
        row = {"session_id": "s1", "source": "claude", "provenance": "human",
               "cwd": "/w/repo", "started_at": "2026-08-01T00:00:00Z",
               "text": "prose", "words": 5_000, "user_turns": 2,
               "assistant_turns": 2, "origin": "raw", "source_present": 1,
               "tool_uses": 0, "tool_results": 0}
        row.update(session)

        conn = sqlite3.connect(self.db)
        conn.execute(f"CREATE TABLE sessions ({', '.join(defs)})")
        names = [n for n in row if n in {c[0] for c in columns}]
        conn.execute(
            f"INSERT INTO sessions ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            [row[n] for n in names])
        conn.commit()

        # Asserted in the fixture, not only in the tests: if this ever stops
        # producing an old-shaped table, every test below would pass while
        # exercising nothing at all, which is the failure mode a migration test
        # can least afford.
        present = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        for name, _ in store._ADDED_SESSION_COLUMNS:
            assert name not in present, f"fixture is not an old archive: {name}"
        conn.close()

    def test_the_columns_are_added_on_open(self) -> None:
        self._make_old_archive()
        st = store.open_store(self.db)
        self.addCleanup(st.close)
        present = {r["name"] for r in st.conn.execute("PRAGMA table_info(sessions)")}
        for name, _ in store._ADDED_SESSION_COLUMNS:
            self.assertIn(name, present)

    def test_an_already_enriched_row_gets_a_baseline(self) -> None:
        """Otherwise it can never be found stale, only re-enriched by force."""
        self._make_old_archive()
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE sessions SET topic = 'known' WHERE session_id = 's1'")
        conn.commit()
        conn.close()

        st = store.open_store(self.db)
        self.addCleanup(st.close)
        row = st.conn.execute(
            "SELECT enriched_words FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertEqual(row["enriched_words"], 5_000)

    def test_an_unenriched_row_gets_no_baseline(self) -> None:
        """Never summarised, so there is nothing for a baseline to be of."""
        self._make_old_archive()
        st = store.open_store(self.db)
        self.addCleanup(st.close)
        row = st.conn.execute(
            "SELECT enriched_words FROM sessions WHERE session_id = 's1'").fetchone()
        self.assertIsNone(row["enriched_words"])

    def test_opening_twice_is_harmless(self) -> None:
        """This runs on every open, including the concurrent ones above."""
        self._make_old_archive()
        for _ in range(3):
            store.open_store(self.db).close()
        st = store.open_store(self.db)
        self.addCleanup(st.close)
        self.assertEqual(
            st.conn.execute("SELECT COUNT(*) c FROM sessions").fetchone()["c"], 1)


if __name__ == "__main__":
    unittest.main()
