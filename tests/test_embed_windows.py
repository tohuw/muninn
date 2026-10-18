"""One stored vector per window, and the ranking that proves it was worth it.

A chunk longer than the embedding model's context window is several windows.
Until schema v4 those windows were averaged into one row, because
``chunk_vectors`` keyed on ``(session_id, ordinal, model)`` and had nowhere to
put the second one. The average was shipped knowingly as a compromise and
written up as one: `embed.search_sessions` scores a session by its *best*
vector precisely because averaging lets irrelevant neighbours dilute an exact
hit, and averaging a chunk's windows does that one level down.

So the interesting test here is not "are there more rows". It is
:class:`DilutionTest`, which builds a ranking that **comes out in a different
order** under the two designs, and asserts the per-window one. Arithmetic
rather than a real model: the vectors are written down, so the expected
ordering is checkable by hand and the test cannot drift with the weights.
"""
from __future__ import annotations

import contextlib
import math
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from muninn import embed
from muninn.store import open_store

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

needs_numpy = unittest.skipUnless(
    HAVE_NUMPY, "numpy is in the optional [semantic] extra; "
                "the default install must pass without it")

#: Windows inside one text are separated by this in the fakes below. A text is
#: its own tokeniser here so a window boundary is exact and visible in the test.
SPLIT = "|"


class WindowingEmbedder:
    """A provider that windows, with vectors written down rather than learned.

    Mirrors the real contract in both directions: ``embed_windows`` returns a
    flat array plus owners, and ``embed`` returns the *mean* over each text's
    windows — which is what `embed_onnx` does, and what makes the comparison
    in :class:`DilutionTest` a comparison of the two storage designs rather
    than of two different providers.
    """

    name = "fake-windowing"
    model = "fake/windowed-v1"
    space = "fake/windowed-v1+w8s2win"
    dim = 4

    #: Deliberately not orthogonal. `near` is a decent match for the query and
    #: `tail` is an exact one, so which of them wins is decided by whether a
    #: chunk's windows were averaged — see DilutionTest for the arithmetic.
    TABLE = {
        "query": [1.0, 0.0, 0.0, 0.0],
        "tail": [1.0, 0.0, 0.0, 0.0],
        "filler": [0.0, 1.0, 0.0, 0.0],
        "near": [0.8, 0.6, 0.0, 0.0],
    }

    def available(self) -> str | None:
        return None

    def _vector(self, piece: str) -> list[float]:
        return self.TABLE[piece.strip()]

    def embed_windows(self, texts):
        import numpy as np

        owners: list[int] = []
        vectors: list[list[float]] = []
        for index, text in enumerate(texts):
            for piece in text.split(SPLIT):
                owners.append(index)
                vectors.append(self._vector(piece))
        if not vectors:
            return np.zeros((0, self.dim), dtype=np.float32), []
        return np.asarray(vectors, dtype=np.float32), owners

    def embed(self, texts):
        """The mean over each text's windows, normalised — the old behaviour."""
        import numpy as np

        rows = list(texts)
        if not rows:
            return np.zeros((0, self.dim), dtype=np.float32)
        array, owners = self.embed_windows(rows)
        out = np.zeros((len(rows), self.dim), dtype=np.float32)
        counts = np.zeros((len(rows), 1), dtype=np.float32)
        np.add.at(out, owners, array)
        np.add.at(counts, owners, 1.0)
        out /= np.maximum(counts, 1.0)
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0.0] = 1.0
        return (out / norms).astype(np.float32)


class PlainEmbedder:
    """No ``embed_windows`` and no ``space``: the three-attribute protocol.

    Present so every test below also says what happens to a provider written
    before any of this existed — `embed_mlx`, and any plugin-contributed
    provider. Those must keep storing exactly what they always stored.
    """

    name = "fake-plain"
    model = "fake/plain-v1"
    dim = 4

    def available(self) -> str | None:
        return None

    def embed(self, texts):
        import numpy as np

        out = []
        for text in texts:
            raw = [((hash((text, i)) % 997) + 1) / 997.0 for i in range(self.dim)]
            norm = math.sqrt(sum(v * v for v in raw))
            out.append([v / norm for v in raw])
        return np.asarray(out, dtype=np.float32) if out else np.zeros((0, 4), dtype=np.float32)


class _Archive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-windows-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)
        embed.clear_cache()

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        embed.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_session(self, session_id: str, text: str) -> None:
        self.st.upsert_session({
            "session_id": session_id, "source": "claude", "provenance": "human",
            "text": text, "words": len(text.split()), "user_turns": 1,
            "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "origin": "raw", "source_present": 1,
        })
        self.st.replace_chunks(session_id, text)
        self.st.commit()

    def pending(self, provider):
        return embed.pending_chunks(self.st, embed.space_of(provider))

    def rows_for(self, session_id: str):
        return self.st.conn.execute(
            "SELECT ordinal, window, model FROM chunk_vectors "
            "WHERE session_id = ? ORDER BY ordinal, window", (session_id,)).fetchall()


# ── The reason the table was rebuilt ──────────────────────────────────────────

@needs_numpy
class DilutionTest(_Archive):
    """The ranking that the mean got wrong, asserted end to end.

    Two sessions, one chunk each, and a query. The vectors are from
    ``WindowingEmbedder.TABLE``, so both orderings are arithmetic:

    ======================  ==========================  ===============
    session                 windows                     best / mean
    ======================  ==========================  ===============
    ``long`` filler|tail    ``[0,1,0,0]``, ``[1,0,0,0]``  **1.00** / 0.707
    ``short`` near          ``[0.8,0.6,0,0]``             0.80 / **0.80**
    ======================  ==========================  ===============

    The mean of ``long``'s two windows is ``[0.5,0.5,0,0]``, which normalises
    to ``[0.707,0.707,0,0]`` and scores 0.707 against the query — *below*
    ``short``'s 0.8. So averaging inverts the order, and the session that
    literally contains the query text loses to one that merely resembles it.

    That is the whole argument for one row per window, and it is the same
    argument `search_sessions` already makes about chunks within a session.
    No code in `search_sessions` changed to gain this: the max was always
    right and simply had nothing to choose between.
    """

    def setUp(self) -> None:
        super().setUp()
        self.provider = WindowingEmbedder()
        self.add_session("long", f"filler{SPLIT}tail")
        self.add_session("short", "near")
        # Both sessions are one chunk: these texts are far shorter than the
        # chunker's window, so `ordinal` is 0 for each and the only thing
        # distinguishing the designs is how many vectors that chunk gets.
        for session in ("long", "short"):
            self.assertEqual(
                1, len([r for r in self.pending(self.provider)
                        if r["session_id"] == session]))

    def _rank(self):
        embed.clear_cache()
        return embed.search_sessions(self.st, self.provider, "query", limit=5)

    def test_the_session_containing_the_query_wins(self) -> None:
        embed.embed_and_store(self.st, self.provider, self.pending(self.provider))
        self.st.commit()

        ranked = self._rank()
        self.assertEqual("long", ranked[0][0],
                         f"the session whose tail *is* the query lost: {ranked}")
        self.assertAlmostEqual(1.0, ranked[0][1], places=5)

    def test_averaging_the_windows_would_invert_it(self) -> None:
        """The counterfactual, run rather than asserted in a comment.

        Without this the test above could pass for the wrong reason — if
        `near` happened to score below `long`'s mean, per-window storage would
        be unnecessary and nothing would say so. Storing the provider's own
        mean (one row per chunk, which is exactly the pre-v4 behaviour) and
        getting the *opposite* order is what makes the assertion above
        load-bearing.
        """
        vectors = self.provider.embed(["filler" + SPLIT + "tail", "near"])
        for session, vector in zip(("long", "short"), vectors):
            embed.store_vectors(
                self.st, session, embed.space_of(self.provider),
                self.provider.dim, [list(vector)])
        self.st.commit()

        ranked = self._rank()
        self.assertEqual("short", ranked[0][0],
                         "the mean was supposed to lose this ranking; if it wins, "
                         "the vectors in TABLE no longer demonstrate dilution")


# ── Storage shape ─────────────────────────────────────────────────────────────

@needs_numpy
class WindowNumberingTest(_Archive):
    def setUp(self) -> None:
        super().setUp()
        self.provider = WindowingEmbedder()

    def test_each_window_is_its_own_row_numbered_from_zero(self) -> None:
        self.add_session("s1", f"filler{SPLIT}tail{SPLIT}near")
        written = embed.embed_and_store(self.st, self.provider, self.pending(self.provider))
        self.st.commit()

        self.assertEqual(3, written)
        self.assertEqual([(0, 0), (0, 1), (0, 2)],
                         [(r["ordinal"], r["window"]) for r in self.rows_for("s1")])

    def test_each_chunk_numbers_its_windows_independently(self) -> None:
        """A batch spans sessions and chunks; the counter must not be global.

        The bug this pins is a single running window counter, which would
        number the second chunk's first window 2 and leave every chunk after
        the first with no window 0 — invisible to search, which reads every
        row, and invisible to `pending_chunks`, which asks only whether any
        row exists.
        """
        self.add_session("a", f"filler{SPLIT}tail")
        self.add_session("b", f"near{SPLIT}filler")
        embed.embed_and_store(self.st, self.provider, self.pending(self.provider))
        self.st.commit()

        for session in ("a", "b"):
            with self.subTest(session=session):
                self.assertEqual([(0, 0), (0, 1)],
                                 [(r["ordinal"], r["window"]) for r in self.rows_for(session)])

    def test_a_re_embed_replaces_rather_than_duplicates(self) -> None:
        """Twice through the same chunk is an UPSERT, not two sets of rows.

        ``store_window_vectors`` conflicts on the full key including
        ``window``, and the worker re-embeds on any restart, so a key that
        omitted the window would either raise or silently double every row and
        rank each window twice.
        """
        self.add_session("s1", f"filler{SPLIT}tail")
        rows = self.pending(self.provider)
        for _ in range(2):
            # The same pending rows twice, which is what a restarted worker
            # does: it re-reads a batch it had already written if the commit
            # had not landed, and must not end up with two sets.
            embed.embed_and_store(self.st, self.provider, rows)
            self.st.commit()

        self.assertEqual(2, len(self.rows_for("s1")))

    def test_a_chunk_with_any_window_is_not_pending(self) -> None:
        """What makes the ``window`` column invisible to the read side.

        `pending_chunks` joins on (session, ordinal, model) and never on
        window, so a chunk is done once *any* of its windows is stored. That
        is the property that let the column be added without touching the
        query every caller shares.
        """
        self.add_session("s1", f"filler{SPLIT}tail")
        embed.embed_and_store(self.st, self.provider, self.pending(self.provider))
        self.st.commit()

        self.assertEqual([], self.pending(self.provider))
        self.assertEqual(0, embed.pending_count(self.st, embed.space_of(self.provider)))


@needs_numpy
class PlainProviderTest(_Archive):
    """A provider that never heard of windows stores what it always stored."""

    def setUp(self) -> None:
        super().setUp()
        self.provider = PlainEmbedder()

    def test_one_row_per_chunk_at_window_zero(self) -> None:
        self.add_session("s1", "a modest amount of text")
        written = embed.embed_and_store(self.st, self.provider, self.pending(self.provider))
        self.st.commit()

        self.assertEqual(1, written)
        self.assertEqual([(0, 0)],
                         [(r["ordinal"], r["window"]) for r in self.rows_for("s1")])

    def test_it_stores_under_the_model_id_not_a_recipe(self) -> None:
        """`space_of` falls back to the model, so the key is unchanged."""
        self.assertEqual(self.provider.model, embed.space_of(self.provider))

    def test_a_short_return_writes_nothing_rather_than_raising(self) -> None:
        """A broken provider must look like no progress, not like a crash.

        `embedder.BackgroundEmbedder` detects a stalled provider by a pass
        that writes zero rows against a non-empty backlog, and gives up after
        ``stall_limit`` of them. That guard is what stands between a broken
        metered provider and a hot loop billing for it.

        The callers used to get this free from ``zip``, which stops at the
        shorter side. Building an owner list from ``len(texts)`` instead
        turned the short write into an ``IndexError``, which the worker read
        as a transient failure and retried forever — found by
        ``test_embedder.py`` hanging, which is the good outcome and the reason
        this is asserted here too, at the level where the decision is made.
        """
        class ShortProvider(PlainEmbedder):
            def embed(self, texts):
                return []

        provider = ShortProvider()
        array, owners = embed.windows_of(provider, ["one", "two"])
        self.assertEqual([], owners)

        self.add_session("s1", "some text to embed")
        self.assertEqual(
            0, embed.embed_and_store(self.st, provider, self.pending(provider)))

    def test_windows_of_owns_the_fallback(self) -> None:
        """Asserted on `windows_of` directly, because that is the contract.

        Callers must not branch on whether a provider windows — one of them is
        the background worker and one is the CLI, and a branch in each is how
        they drift. The fallback lives here, once.
        """
        array, owners = embed.windows_of(self.provider, ["one", "two", "three"])
        self.assertEqual([0, 1, 2], owners)
        self.assertEqual(3, array.shape[0])


# ── Reporting ─────────────────────────────────────────────────────────────────

@needs_numpy
class CoverageReportingTest(_Archive):
    """`doctor` divides by chunks, and rows are no longer chunks."""

    def test_embedded_chunks_counts_chunks_not_windows(self) -> None:
        provider = WindowingEmbedder()
        self.add_session("s1", f"filler{SPLIT}tail{SPLIT}near")
        embed.embed_and_store(self.st, provider, self.pending(provider))
        self.st.commit()

        space = embed.space_of(provider)
        self.assertEqual(3, embed.vector_count(self.st, space))
        self.assertEqual(1, embed.embedded_chunks(self.st, space))

    def test_coverage_cannot_exceed_the_corpus(self) -> None:
        """The number `doctor` prints, pinned against the 137% it printed.

        Stated as a property rather than a string match: whatever the line
        looks like, chunks-with-a-vector can never exceed chunks, and it was
        rows/chunks that could.
        """
        provider = WindowingEmbedder()
        self.add_session("s1", f"filler{SPLIT}tail{SPLIT}near")
        self.add_session("s2", f"near{SPLIT}filler")
        embed.embed_and_store(self.st, provider, self.pending(provider))
        self.st.commit()

        space = embed.space_of(provider)
        self.assertGreater(embed.vector_count(self.st, space), self.st.count_chunks())
        self.assertLessEqual(embed.embedded_chunks(self.st, space), self.st.count_chunks())


class DimensionMismatchTest(_Archive):
    """A wrong-length vector still raises, and now says which window."""

    def test_it_names_the_window(self) -> None:
        with self.assertRaises(embed.DimensionMismatch) as caught:
            embed.store_window_vectors(self.st, "s1", "m", 4, [(7, 2, [1.0, 2.0])])
        message = str(caught.exception)
        self.assertIn("chunk 7", message)
        self.assertIn("window 2", message)


# ── The migration ─────────────────────────────────────────────────────────────

class MigrationTest(unittest.TestCase):
    """A v3 archive gains the column without losing a vector.

    Built by creating the **real v3 table** and then opening the archive
    normally, rather than by calling the migration directly: the thing being
    tested is that opening an existing archive migrates it, and a direct call
    would pass even if nothing wired it into `_connect_and_prepare`.
    """

    V3_DDL = """
        CREATE TABLE chunk_vectors (
            session_id TEXT NOT NULL,
            ordinal    INTEGER NOT NULL,
            model      TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vec        BLOB NOT NULL,
            PRIMARY KEY (session_id, ordinal, model)
        )"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-migrate-"))
        self.db = self.tmp / "archive.db"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.blob = embed.pack_vector([0.5, -0.25, 0.125, 0.0625])
        conn = sqlite3.connect(str(self.db))
        conn.execute(self.V3_DDL)
        conn.executemany(
            "INSERT INTO chunk_vectors (session_id, ordinal, model, dim, vec) "
            "VALUES (?, ?, ?, ?, ?)",
            [("s1", 0, "keep/me-v1", 4, self.blob),
             ("s1", 1, "keep/me-v1", 4, self.blob),
             ("s2", 0, "BAAI/bge-small-en-v1.5+w512s64mean", 4, self.blob)])
        conn.commit()
        conn.close()

    def _open(self):
        st = open_store(self.db)

        def close() -> None:
            with contextlib.suppress(Exception):
                st.close()

        self.addCleanup(close)
        return st

    def test_existing_vectors_survive_as_window_zero(self) -> None:
        st = self._open()
        rows = st.conn.execute(
            "SELECT ordinal, window, vec FROM chunk_vectors "
            "WHERE model = 'keep/me-v1' ORDER BY ordinal").fetchall()
        self.assertEqual([(0, 0), (1, 0)], [(r["ordinal"], r["window"]) for r in rows])
        # Bit-identical, not merely present: the copy goes through SQLite and a
        # BLOB that changed length would be a vector that silently cannot be
        # reshaped, which `load_matrix` drops without complaint.
        self.assertTrue(all(r["vec"] == self.blob for r in rows))

    def test_the_new_key_admits_several_windows_per_chunk(self) -> None:
        """The point of the rebuild. Under the v3 key this raises.

        Asserted by writing the row the old primary key forbade, rather than
        by reading ``PRAGMA index_info``: the constraint is what matters, not
        how SQLite reports it.
        """
        st = self._open()
        embed.store_window_vectors(
            st, "s1", "keep/me-v1", 4,
            [(0, 1, [1.0, 0.0, 0.0, 0.0]), (0, 2, [0.0, 1.0, 0.0, 0.0])])
        st.commit()
        self.assertEqual(
            3, st.conn.execute(
                "SELECT COUNT(*) n FROM chunk_vectors "
                "WHERE session_id = 's1' AND ordinal = 0").fetchone()["n"])

    def test_the_superseded_mean_space_is_dropped(self) -> None:
        """Deleted on purpose, and only this one name.

        That space held one mean vector per chunk and existed for part of a
        single day. No build carrying this migration can produce or read it —
        `embed_onnx.space` derives its suffix from its own constants and ends
        in ``win`` — so the rows are unreadable by construction, and `doctor`
        would report a second space forever with nothing able to remove it.
        """
        st = self._open()
        self.assertEqual([], st.conn.execute(
            "SELECT 1 FROM chunk_vectors WHERE model LIKE '%mean'").fetchall())
        self.assertEqual(2, st.conn.execute(
            "SELECT COUNT(*) n FROM chunk_vectors").fetchone()["n"])

    def test_opening_twice_is_not_a_second_rebuild(self) -> None:
        """Idempotence, asserted through the data rather than a flag.

        The migration runs on every open. A second pass that re-ran the
        rebuild would be harmless here but would silently re-copy the table
        on every single open of every archive forever, which is the kind of
        cost nothing would ever surface.
        """
        first = self._open()
        embed.store_window_vectors(first, "s3", "keep/me-v1", 4, [(0, 1, [1.0, 0, 0, 0])])
        first.commit()
        first.close()

        second = self._open()
        self.assertEqual(3, second.conn.execute(
            "SELECT COUNT(*) n FROM chunk_vectors WHERE model = 'keep/me-v1'"
        ).fetchone()["n"])


if __name__ == "__main__":
    unittest.main()
