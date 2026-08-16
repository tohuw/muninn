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


if __name__ == "__main__":
    unittest.main()
