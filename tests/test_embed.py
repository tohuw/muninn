"""Embeddings: storage, model isolation, cosine, and the no-provider refusal.

Two halves, split by what they need:

- **Storage and refusal tests always run.** They use struct and sqlite, and they
  cover the properties that would corrupt an archive or lie to a user.
- **Matrix and cosine tests skip without numpy**, which is correct rather than a
  gap: numpy lives in the optional ``[semantic]`` extra, and the default install
  must pass the whole suite with it absent (spec 006, definition of done). A
  skipped test says so out loud; a test that silently passed without exercising
  anything would not.

No test loads a model or reaches a network. The provider is always a fake
producing deterministic vectors.
"""
from __future__ import annotations

import contextlib
import io
import math
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import cli, embed, policy
from muninn.store import open_store

try:
    import numpy  # noqa: F401

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

needs_numpy = unittest.skipUnless(
    HAVE_NUMPY, "numpy is in the optional [semantic] extra; "
                "the default install must pass without it")


class FakeEmbedder:
    """Deterministic unit vectors keyed off the text, so results are checkable."""

    name = "fake-embedder"
    model = "fake/model-v1"
    dim = 4

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = table or {}
        self.calls: list[list[str]] = []

    def available(self) -> str | None:
        return None

    def _vector(self, text: str) -> list[float]:
        if text in self.table:
            return self.table[text]
        # Stable pseudo-vector from the text, normalised.
        raw = [((hash((text, i)) % 1000) + 1) / 1000.0 for i in range(self.dim)]
        norm = math.sqrt(sum(v * v for v in raw))
        return [v / norm for v in raw]

    def embed(self, texts):
        self.calls.append(list(texts))
        if HAVE_NUMPY:
            import numpy as np

            return np.asarray([self._vector(t) for t in texts], dtype=np.float32)
        return [self._vector(t) for t in texts]


class _Archive(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-embed-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)
        embed.clear_cache()

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        embed.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_session(self, session_id: str, text: str, *, source: str = "claude") -> None:
        self.st.upsert_session({
            "session_id": session_id, "source": source, "provenance": "human",
            "text": text, "words": len(text.split()), "user_turns": 1,
            "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "origin": "raw", "source_present": 1,
        })
        self.st.replace_chunks(session_id, text)
        self.st.commit()


# ── Storage: runs on the default install ──────────────────────────────────────

class StorageTest(_Archive):
    def test_vectors_round_trip_bit_identical(self) -> None:
        # Criterion 3. float32 in, float32 out, no drift through the BLOB.
        values = [0.5, -0.25, 0.125, 0.0625]
        embed.store_vectors(self.st, "s1", "m", 4, [values])
        self.st.commit()
        blob = self.st.conn.execute(
            "SELECT vec FROM chunk_vectors WHERE session_id = 's1'").fetchone()["vec"]
        self.assertEqual(list(embed.unpack_vector(blob)), values)
        self.assertEqual(len(blob), 4 * 4)

    def test_two_models_coexist_without_mixing(self) -> None:
        # Criterion 4. Mixing embedding spaces does not error and does not look
        # wrong — it returns confident nonsense — so it is prevented by the key.
        embed.store_vectors(self.st, "s1", "model-a", 4, [[1, 0, 0, 0]])
        embed.store_vectors(self.st, "s1", "model-b", 4, [[0, 1, 0, 0]])
        self.st.commit()
        self.assertEqual(embed.vector_count(self.st, "model-a"), 1)
        self.assertEqual(embed.vector_count(self.st, "model-b"), 1)
        self.assertEqual(embed.vector_count(self.st), 2)
        self.assertEqual([m[0] for m in embed.models_present(self.st)],
                         ["model-a", "model-b"])

    def test_a_wrong_length_vector_is_refused(self) -> None:
        # Criterion 5. Padding or truncating would produce a row that ranks
        # perfectly happily and is silently meaningless.
        with self.assertRaises(embed.DimensionMismatch):
            embed.store_vectors(self.st, "s1", "m", 4, [[1.0, 2.0]])
        self.assertEqual(embed.vector_count(self.st), 0)

    def test_re_embedding_a_chunk_replaces_rather_than_duplicates(self) -> None:
        embed.store_vectors(self.st, "s1", "m", 4, [[1, 0, 0, 0]])
        embed.store_vectors(self.st, "s1", "m", 4, [[0, 0, 0, 1]])
        self.st.commit()
        self.assertEqual(embed.vector_count(self.st, "m"), 1)
        blob = self.st.conn.execute("SELECT vec FROM chunk_vectors").fetchone()["vec"]
        self.assertEqual(embed.unpack_vector(blob)[3], 1.0)

    def test_stale_vectors_are_dropped_when_a_session_is_re_chunked(self) -> None:
        # Criterion 13. A vector keyed to an ordinal whose text no longer exists
        # would be returned by cosine search and then fail to render.
        self.add_session("s1", "word " * 3000)
        n_before = self.st.count_chunks()
        self.assertGreater(n_before, 1)
        for ordinal in range(n_before):
            embed.store_vectors(self.st, "s1", "m", 4, [[1, 0, 0, 0]],
                                start_ordinal=ordinal)
        self.st.commit()
        self.assertEqual(embed.vector_count(self.st, "m"), n_before)

        self.st.replace_chunks("s1", "much shorter now")
        self.st.commit()
        self.assertEqual(self.st.count_chunks(), 1)
        self.assertEqual(embed.vector_count(self.st, "m"), 1)

    def test_surviving_ordinals_keep_their_vectors(self) -> None:
        # The other direction: re-indexing one session must not cost a full
        # re-embed of it. Only orphans go.
        self.add_session("s1", "word " * 3000)
        for ordinal in range(self.st.count_chunks()):
            embed.store_vectors(self.st, "s1", "m", 4, [[1, 0, 0, 0]],
                                start_ordinal=ordinal)
        self.st.commit()
        self.st.replace_chunks("s1", "word " * 3000)   # same shape
        self.st.commit()
        self.assertGreater(embed.vector_count(self.st, "m"), 1)

    def test_the_schema_migrated_without_touching_anything_else(self) -> None:
        # v2 -> v3 adds a table. The archive's own guarantee is that nothing
        # else moved.
        self.add_session("s1", "the only surviving copy")
        version = self.st.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
        self.assertEqual(version, "3")
        self.assertEqual(self.st.session_text("s1"), "the only surviving copy")


# ── The refusal: runs on the default install, and must ────────────────────────

class NoProviderTest(_Archive):
    """Criteria 8 and 9: no provider is an error, never a silent downgrade."""

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.db), *argv])
        return code, out.getvalue(), err.getvalue()

    def setUp(self) -> None:
        super().setUp()
        self.add_session("s1", "the auth redirect decision we made in June")
        self.unavailable = patch.object(
            embed, "resolve_provider",
            side_effect=embed.EmbeddingUnavailable(
                "no embedding provider is available — install `uv sync --extra semantic`"))

    def test_semantic_without_a_provider_exits_nonzero(self) -> None:
        with self.unavailable:
            code, out, err = self._run("search", "auth", "--semantic")
        self.assertEqual(code, 2)
        self.assertIn("semantic", err)

    def test_semantic_without_a_provider_returns_no_results_at_all(self) -> None:
        # The failure spec 006 names outright: never return lexical results
        # labelled as semantic. The query below *does* match lexically, so a
        # silent downgrade would print a hit.
        with self.unavailable:
            _code, out, _err = self._run("search", "auth", "--semantic")
        self.assertEqual(out.strip(), "")

    def test_deep_without_a_provider_is_an_error_not_a_downgrade(self) -> None:
        # Criterion 9.
        with self.unavailable:
            code, out, _err = self._run("search", "auth", "--deep")
        self.assertEqual(code, 2)
        self.assertEqual(out.strip(), "")

    def test_plain_search_is_unaffected(self) -> None:
        # The other half: the refusal must not have broken the default path.
        with self.unavailable:
            code, out, _err = self._run("search", "auth")
        self.assertEqual(code, 0)
        self.assertIn("s1", out)

    def test_the_message_names_the_fix(self) -> None:
        with self.unavailable:
            _code, _out, err = self._run("embed")
        self.assertIn("--extra semantic", err)

    def test_resolve_provider_refuses_rather_than_inventing_a_fallback(self) -> None:
        with patch("muninn.plugins.discover_plugins") as discovered:
            discovered.return_value = type("R", (), {"specs": ()})()
            with patch.dict(sys.modules, {"muninn.embed_mlx": None}):
                with self.assertRaises(embed.EmbeddingUnavailable):
                    embed.resolve_provider()


class LazyImportTest(unittest.TestCase):
    """Criterion 10: importing muninn must not import numpy or mlx."""

    @unittest.skipIf(sys.platform == "win32", "subprocess-based; see WINDOWS.md")
    def test_importing_the_cli_pulls_in_neither(self) -> None:
        # A subprocess because this process may already have imported numpy for
        # another test. muninn.cli imports muninn.embed unconditionally to
        # register the subcommands, so a module-level `import numpy` would make
        # the *default* install fail at startup rather than at --semantic.
        code = ("import sys; import muninn.cli; "
                "print('numpy' in sys.modules, 'mlx' in sys.modules, "
                "'mlx_embeddings' in sys.modules)")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False False False")


# ── Matrix and cosine: need numpy ─────────────────────────────────────────────

@needs_numpy
class CosineTest(_Archive):
    def _matrix(self, vectors: dict[tuple[str, int], list[float]], model="m"):
        for (session_id, ordinal), vector in vectors.items():
            embed.store_vectors(self.st, session_id, model, len(vector), [vector],
                                start_ordinal=ordinal)
        self.st.commit()
        return embed.load_matrix(self.st, model, use_cache=False)

    def test_top_k_matches_hand_computed_similarities(self) -> None:
        # Criterion 6. Unit vectors, so cosine is the dot product and the
        # expected values are readable: query [1,0] gives a=1.0, c=0.707, b=0.0.
        matrix = self._matrix({
            ("a", 0): [1.0, 0.0],
            ("b", 0): [0.0, 1.0],
            ("c", 0): [0.7071067811865476, 0.7071067811865476],
        })
        hits = embed.cosine_topk(matrix, [1.0, 0.0], k=3)
        self.assertEqual([key[0] for key, _ in hits], ["a", "c", "b"])
        self.assertAlmostEqual(hits[0][1], 1.0, places=5)
        self.assertAlmostEqual(hits[1][1], 0.7071, places=3)
        self.assertAlmostEqual(hits[2][1], 0.0, places=5)

    def test_k_larger_than_the_corpus_is_not_an_error(self) -> None:
        matrix = self._matrix({("a", 0): [1.0, 0.0]})
        self.assertEqual(len(embed.cosine_topk(matrix, [1.0, 0.0], k=50)), 1)

    def test_an_empty_archive_returns_nothing(self) -> None:
        self.assertEqual(embed.cosine_topk(embed.load_matrix(self.st, "m"), [1.0]), [])

    def test_a_query_of_the_wrong_dimension_is_refused(self) -> None:
        matrix = self._matrix({("a", 0): [1.0, 0.0]})
        with self.assertRaises(embed.DimensionMismatch):
            embed.cosine_topk(matrix, [1.0, 0.0, 0.0])

    def test_the_matrix_holds_only_the_requested_model(self) -> None:
        # Criterion 4's read-path half.
        embed.store_vectors(self.st, "a", "model-a", 2, [[1.0, 0.0]])
        embed.store_vectors(self.st, "b", "model-b", 2, [[0.0, 1.0]])
        self.st.commit()
        matrix = embed.load_matrix(self.st, "model-a", use_cache=False)
        self.assertEqual([k[0] for k in matrix.keys], ["a"])

    def test_a_blob_that_disagrees_with_its_dim_is_dropped_not_reshaped(self) -> None:
        embed.store_vectors(self.st, "a", "m", 2, [[1.0, 0.0]])
        self.st.conn.execute(
            "INSERT INTO chunk_vectors (session_id, ordinal, model, dim, vec) "
            "VALUES ('b', 0, 'm', 2, ?)", (b"\x00\x00",))
        self.st.commit()
        matrix = embed.load_matrix(self.st, "m", use_cache=False)
        self.assertEqual([k[0] for k in matrix.keys], ["a"])

    def test_the_cache_is_keyed_on_the_archive_changing(self) -> None:
        self._matrix({("a", 0): [1.0, 0.0]})
        first = embed.load_matrix(self.st, "m")
        self.assertIs(embed.load_matrix(self.st, "m"), first)
        time.sleep(0.01)
        embed.store_vectors(self.st, "b", "m", 2, [[0.0, 1.0]])
        self.st.commit()
        self.assertEqual(len(embed.load_matrix(self.st, "m")), 2)


@needs_numpy
class SessionSearchTest(_Archive):
    def test_a_session_scores_by_its_best_chunk_not_its_mean(self) -> None:
        # A long session with one exactly-relevant passage beats a short one
        # that is vaguely on-topic throughout; averaging inverts that.
        self.add_session("long", "word " * 10)
        self.add_session("short", "word " * 10)
        embed.store_vectors(self.st, "long", "fake/model-v1", 4,
                            [[1, 0, 0, 0], [0, 0, 0, 1], [0, 0, 0, 1]])
        embed.store_vectors(self.st, "short", "fake/model-v1", 4, [[0.7, 0.7, 0, 0]])
        self.st.commit()
        provider = FakeEmbedder({"q": [1.0, 0.0, 0.0, 0.0]})
        ranked = embed.search_sessions(self.st, provider, "q", limit=5)
        self.assertEqual(ranked[0][0], "long")

    def test_search_routes_through_the_policy_chokepoint(self) -> None:
        # An embedding call is still a model call.
        refusing = policy.ModelPolicy(name="none", allow=(), reason="nothing permitted")
        with patch.object(policy, "resolve", return_value=(refusing,)):
            with self.assertRaises(policy.PolicyRefused):
                embed.search_sessions(self.st, FakeEmbedder(), "q")


@needs_numpy
class CorrelateTest(_Archive):
    def test_the_planted_twin_comes_first_and_the_query_is_excluded(self) -> None:
        # Criterion 12.
        for sid in ("query", "twin", "unrelated"):
            self.add_session(sid, f"{sid} text")
        embed.store_vectors(self.st, "query", "m", 4, [[1, 0, 0, 0], [0.9, 0.1, 0, 0]])
        embed.store_vectors(self.st, "twin", "m", 4, [[0.95, 0.05, 0, 0]])
        embed.store_vectors(self.st, "unrelated", "m", 4, [[0, 0, 0, 1]])
        self.st.commit()
        neighbours = embed.correlate(self.st, "m", "query", limit=5)
        self.assertNotIn("query", [sid for sid, _ in neighbours])
        self.assertEqual(neighbours[0][0], "twin")

    def test_an_unknown_session_returns_nothing_rather_than_raising(self) -> None:
        self.assertEqual(embed.correlate(self.st, "m", "nope"), [])


@needs_numpy
class LatencyTest(_Archive):
    def test_twenty_thousand_vectors_search_well_under_the_budget(self) -> None:
        # Criterion 7, and the measurement the whole design rests on: brute
        # force is enough, so no ANN index is ever warranted.
        import numpy as np

        rng = np.random.default_rng(20260806)
        raw = rng.standard_normal((20_000, 128)).astype(np.float32)
        raw /= np.linalg.norm(raw, axis=1, keepdims=True)
        matrix = embed.Matrix(keys=tuple((f"s{i}", 0) for i in range(20_000)),
                              array=np.ascontiguousarray(raw))
        query = raw[7].copy()

        embed.cosine_topk(matrix, query, k=20)      # warm
        start = time.perf_counter()
        hits = embed.cosine_topk(matrix, query, k=20)
        elapsed_ms = (time.perf_counter() - start) * 1000

        self.assertEqual(hits[0][0][0], "s7")
        self.assertLess(elapsed_ms, 50.0,
                        f"brute-force cosine took {elapsed_ms:.1f}ms — if this is "
                        f"genuinely slow now, measure before reaching for an index")


if __name__ == "__main__":
    unittest.main()
