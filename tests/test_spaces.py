"""`muninn spaces`: listing embedding spaces, and deleting a superseded one.

The command exists because `doctor` spent two releases advising an action the
tool could not perform — "the others are dead weight, finish the re-embed or
delete them" — while every delete path in the archive was keyed by session.
These tests are therefore written against what that advice promised, not
against what the new code happens to do.

Two properties are load-bearing and each is asserted from both sides:

- **The active space is never deleted by accident.** It is refused by name,
  and refused again when no provider is installed to say which space is
  active, because "stale" and "unknowable" look identical from outside and
  only one of them is safe.
- **A name matches exactly or not at all.** The two spaces in a half-finished
  re-embed differ by a suffix, so the stale name is a *prefix* of the live
  one, and any convenience matching would make the worst typo the easy one.

No test loads a model or reaches a network; the provider is a fake declaring a
space.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import cli, embed
from muninn.store import open_store

try:
    import numpy  # noqa: F401

    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False

#: The shape the real archive is in mid-re-embed: same weights, same dim, one
#: recipe superseding the other, and the stale name a prefix of the live one.
STALE = "fake/model-v1"
LIVE = "fake/model-v1+w512s64win"


class FakeProvider:
    """Three required attributes plus a declared ``space``."""

    name = "fake-embedder"
    model = "fake/model-v1"
    dim = 4
    space = LIVE

    def available(self) -> str | None:
        return None

    def embed(self, texts):   # pragma: no cover - never called by `spaces`
        raise AssertionError("`muninn spaces` must not embed anything")


class _Spaces(unittest.TestCase):
    """An archive holding one session and two spaces over its chunks."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-spaces-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)
        embed.clear_cache()

        self.st.upsert_session({
            "session_id": "s1", "source": "claude", "provenance": "human",
            "text": "the auth redirect decision we made in June",
            "words": 8, "user_turns": 1, "assistant_turns": 1,
            "tool_uses": 0, "tool_results": 0,
            "origin": "raw", "source_present": 1,
        })
        self.st.replace_chunks("s1", "the auth redirect decision we made in June")
        # One vector in the stale space, two windows of the same chunk in the
        # live one, so a row count and a chunk count cannot be confused.
        embed.store_vectors(self.st, "s1", STALE, 4, [[1.0, 0.0, 0.0, 0.0]])
        embed.store_window_vectors(self.st, "s1", LIVE, 4, [
            (0, 0, [0.0, 1.0, 0.0, 0.0]),
            (0, 1, [0.0, 0.0, 1.0, 0.0]),
        ])
        self.st.commit()
        self.st.close()

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        embed.clear_cache()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.db), *argv])
        return code, out.getvalue(), err.getvalue()

    def _with_provider(self):
        return patch.object(embed, "resolve_provider", return_value=FakeProvider())

    def _without_provider(self):
        return patch.object(
            embed, "resolve_provider",
            side_effect=embed.EmbeddingUnavailable(
                "no embedding provider is available — `uv sync --extra semantic`"))

    def _counts(self) -> dict[str, int]:
        st = open_store(self.db)
        try:
            return {model: rows for model, _dim, rows in embed.models_present(st)}
        finally:
            st.close()


class ListingTest(_Spaces):
    def test_the_space_the_provider_produces_is_named_active(self) -> None:
        with self._with_provider():
            code, out, _err = self._run("spaces")
        self.assertEqual(code, 0)
        live = next(line for line in out.splitlines() if LIVE in line and "dim" in line)
        self.assertIn("active", live)

    def test_the_other_space_is_named_superseded(self) -> None:
        with self._with_provider():
            _code, out, _err = self._run("spaces")
        stale = next(line for line in out.splitlines()
                     if line.strip().startswith(STALE + " ") and "dim" in line)
        self.assertIn("superseded", stale)

    def test_rows_and_chunks_are_reported_separately(self) -> None:
        # Two windows of one chunk. A report that printed rows as coverage
        # would say this archive holds two chunks, which is how `doctor` came
        # to print "137% of chunks" before windowing was accounted for.
        with self._with_provider():
            _code, out, _err = self._run("spaces")
        live = next(line for line in out.splitlines() if LIVE in line and "dim" in line)
        self.assertIn("2 vectors", live)
        self.assertIn("1 chunk(s)", live)

    def test_the_report_prints_the_drop_command_for_the_superseded_space(self) -> None:
        # The point of the listing: the next command, exactly as it must be
        # typed. Describing it instead is what leaves someone retyping a name
        # that differs from the live one by a suffix.
        with self._with_provider():
            _code, out, _err = self._run("spaces")
        self.assertIn(f"muninn spaces --drop {STALE}\n", out)
        self.assertNotIn(f"--drop {LIVE}", out)

    def test_without_a_provider_nothing_is_called_superseded(self) -> None:
        # A checkout whose [semantic] extra is not synced must not be told to
        # delete its vectors. Both halves matter: no verdict, and no command.
        with self._without_provider():
            code, out, _err = self._run("spaces")
        self.assertEqual(code, 0)
        self.assertNotIn("superseded", out)
        self.assertNotIn("--drop", out)
        self.assertIn("unknown", out)

    def test_an_archive_with_no_vectors_says_so(self) -> None:
        st = open_store(self.db)
        embed.drop_space(st, STALE)
        embed.drop_space(st, LIVE)
        st.commit()
        st.close()
        with self._with_provider():
            code, out, _err = self._run("spaces")
        self.assertEqual(code, 0)
        self.assertIn("no embedding spaces", out)


class DropTest(_Spaces):
    def test_dropping_the_superseded_space_leaves_the_active_one_untouched(self) -> None:
        with self._with_provider():
            code, out, _err = self._run("spaces", "--drop", STALE)
        self.assertEqual(code, 0)
        self.assertEqual(self._counts(), {LIVE: 2})
        self.assertIn("dropped 1 vector(s)", out)

    def test_the_active_space_is_refused_without_force(self) -> None:
        # The load-bearing refusal. Dropping this costs hours of re-embedding
        # and leaves semantic search answering nothing in the meantime.
        with self._with_provider():
            code, _out, err = self._run("spaces", "--drop", LIVE)
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(self._counts(), {STALE: 1, LIVE: 2})

    def test_force_drops_the_active_space_and_says_what_that_costs(self) -> None:
        with self._with_provider():
            code, out, _err = self._run("spaces", "--drop", LIVE, "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self._counts(), {STALE: 1})
        self.assertIn("semantic search is unavailable", out)

    def test_a_prefix_of_a_real_space_is_not_a_match(self) -> None:
        # The other load-bearing guard, and the reason exact matching is worth
        # a test: STALE is itself a prefix of LIVE, so anything looser than
        # equality makes deleting the live space the convenient outcome.
        with self._with_provider():
            code, _out, err = self._run("spaces", "--drop", "fake/model")
        self.assertEqual(code, 1)
        self.assertEqual(self._counts(), {STALE: 1, LIVE: 2})
        self.assertIn(STALE, err)
        self.assertIn(LIVE, err)

    def test_an_unknown_space_lists_what_is_actually_present(self) -> None:
        with self._with_provider():
            code, _out, err = self._run("spaces", "--drop", "other/model-v9")
        self.assertEqual(code, 1)
        self.assertIn("other/model-v9", err)
        self.assertIn(STALE, err)

    def test_dry_run_deletes_nothing(self) -> None:
        with self._with_provider():
            code, out, _err = self._run("spaces", "--drop", STALE, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would drop 1 vector(s)", out)
        self.assertEqual(self._counts(), {STALE: 1, LIVE: 2})

    def test_without_a_provider_dropping_is_refused_rather_than_guessed(self) -> None:
        with self._without_provider():
            code, _out, err = self._run("spaces", "--drop", STALE)
        self.assertEqual(code, 2)
        self.assertIn("--force", err)
        self.assertEqual(self._counts(), {STALE: 1, LIVE: 2})

    def test_without_a_provider_force_is_how_an_operator_overrides_it(self) -> None:
        with self._without_provider():
            code, _out, _err = self._run("spaces", "--drop", STALE, "--force")
        self.assertEqual(code, 0)
        self.assertEqual(self._counts(), {LIVE: 2})

    def test_the_freed_space_is_reported_as_reusable_not_as_reclaimed(self) -> None:
        # SQLite returns the pages to its own freelist, not to the filesystem.
        # Saying "freed 221 MB" and leaving the file the same size is the
        # report claiming something the reader can check and find false.
        with self._with_provider():
            _code, out, _err = self._run("spaces", "--drop", STALE)
        self.assertIn("VACUUM", out)


class PrimitiveTest(_Spaces):
    """`embed.drop_space` itself, including what it deliberately does not do."""

    def test_it_returns_the_number_of_rows_it_removed(self) -> None:
        st = open_store(self.db)
        self.addCleanup(st.close)
        self.assertEqual(embed.drop_space(st, LIVE), 2)
        self.assertEqual(embed.drop_space(st, "never/stored"), 0)

    def test_it_does_not_commit_so_the_caller_owns_the_transaction(self) -> None:
        # Documented, and worth pinning: a wrapper that needs to record the
        # deletion alongside it must be able to do both or neither.
        st = open_store(self.db)
        self.addCleanup(st.close)
        embed.drop_space(st, STALE)
        st.conn.rollback()
        self.assertEqual(embed.vector_count(st, STALE), 1)

    def test_it_touches_no_other_space(self) -> None:
        st = open_store(self.db)
        self.addCleanup(st.close)
        embed.drop_space(st, STALE)
        st.commit()
        self.assertEqual(embed.vector_count(st, LIVE), 2)

    def test_it_deletes_no_prose(self) -> None:
        # The archive's first rule. Vectors are derived and regenerable, which
        # is the entire reason this delete path is allowed to exist; a chunk
        # that lost its text alongside its vector would be unrecoverable.
        st = open_store(self.db)
        self.addCleanup(st.close)
        embed.drop_space(st, STALE)
        st.commit()
        self.assertEqual(st.count_chunks(), 1)
        self.assertIn("auth redirect", st.session_text("s1"))


class QuotingTest(unittest.TestCase):
    """The printed command has to be runnable, which makes quoting a fact."""

    def test_a_model_id_is_printed_bare(self) -> None:
        self.assertEqual(cli._shell_arg(LIVE), LIVE)
        self.assertEqual(cli._shell_arg("BAAI/bge-small-en-v1.5+w512s64win"),
                         "BAAI/bge-small-en-v1.5+w512s64win")

    def test_a_name_a_shell_would_split_is_quoted(self) -> None:
        self.assertEqual(cli._shell_arg("two words"), "'two words'")
        self.assertEqual(cli._shell_arg(""), "''")


class DoctorTest(_Spaces):
    """The warning that started this must name the command that answers it."""

    def test_the_two_space_warning_names_the_command_that_can_act_on_it(self) -> None:
        st = open_store(self.db)
        self.addCleanup(st.close)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_embeddings_section(st)
        text = out.getvalue()
        self.assertIn("dead weight", text)
        self.assertIn("muninn spaces --drop", text)

    def test_one_space_draws_no_warning(self) -> None:
        st = open_store(self.db)
        self.addCleanup(st.close)
        embed.drop_space(st, STALE)
        st.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._print_embeddings_section(st)
        self.assertNotIn("WARNING", out.getvalue())


@unittest.skipUnless(HAVE_NUMPY, "numpy is in the optional [semantic] extra")
class CacheTest(_Spaces):
    """A dropped space must not keep answering out of memory."""

    def test_a_dropped_space_is_not_still_served_from_the_matrix_cache(self) -> None:
        # `load_matrix` caches per space, stamped on the archive's
        # data_version — which does not move for a write made by the same
        # connection. A long-lived process that dropped a space and then
        # searched it would rank vectors that no longer exist, with nothing
        # anywhere to suggest why.
        st = open_store(self.db)
        self.addCleanup(st.close)
        self.assertEqual(len(embed.load_matrix(st, LIVE)), 2)
        embed.drop_space(st, LIVE)
        st.commit()
        embed.clear_cache()
        self.assertEqual(len(embed.load_matrix(st, LIVE)), 0)

    def test_the_command_clears_it_rather_than_leaving_it_to_the_caller(self) -> None:
        st = open_store(self.db)
        self.addCleanup(st.close)
        embed.load_matrix(st, STALE)
        with self._with_provider():
            self._run("spaces", "--drop", STALE)
        self.assertEqual(len(embed.load_matrix(st, STALE)), 0)


if __name__ == "__main__":   # pragma: no cover
    unittest.main()
