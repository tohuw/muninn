"""The import ledger contract.

Muninn's import layer must make the claudex misreading structurally
impossible: an agent reading a re-import's output must never be able to
conclude "nothing new" when the truth is "already imported by someone else
three minutes ago." Each test below is one invariant from
.valholl/articles/import-ledger-schema.md ("Invariants (each one a test)"),
plus the digest-scheme and structured-output requirements from
.valholl/articles/deterministic-imports.md and docs/specs/001-import-ledger.md.

These tests are a contract, like tests/test_losslessness.py: they encode
guarantees about data provenance that cannot be reconstructed after the fact.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from muninn import cli, digest, ingest, sources, store
from muninn.ingest import ConservationError, assert_conservation
from muninn.receipt import Delta, ImportReceipt, Outcome, SkipReason, SourceFacts


def write_claude_transcript(path: Path, session_id: str, turns: list[tuple[str, str]],
                            cwd: str = "/tmp/project", branch: str = "main",
                            model: str = "claude-sonnet-5") -> None:
    """Write a minimal but realistic Claude Code JSONL transcript."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for i, (role, text) in enumerate(turns):
            entry = {
                "type": role,
                "timestamp": f"2026-07-{10 + i // 24:02d}T{i % 24:02d}:00:00.000Z",
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": branch,
                "message": {"role": role, "model": model,
                            "content": [{"type": "text", "text": text}]},
            }
            fh.write(json.dumps(entry) + "\n")


@contextlib.contextmanager
def patched_parser(name: str, replacement):
    """Temporarily swap ``sources.PARSERS[name]``, restoring it afterward.

    Several invariants (a forced parser exception, a missing item id) describe
    failure shapes that Muninn's real, deliberately defensive parsers do not
    hit through crafted JSONL alone — every field access is already
    isinstance-guarded, by design (unstable-jsonl-format.md). Substituting the
    parser callable is the direct way to exercise ingest_path's handling of
    those failures without weakening that defensiveness just to manufacture a
    crash.
    """
    original = sources.PARSERS[name]
    sources.PARSERS[name] = replacement
    try:
        yield
    finally:
        sources.PARSERS[name] = original


class LedgerTestCase(unittest.TestCase):
    """Common temp-dir/db plumbing shared by every ledger test."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-ledger-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class AppendOnlyTest(LedgerTestCase):
    """Invariant 1: no code path UPDATEs or DELETEs a completed ledger row."""

    def test_two_imports_append_two_rows_first_row_untouched(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "aaa.jsonl", "aaa",
            [("user", "q"), ("assistant", "a")],
        )
        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude", actor="actor-a")
        tail = st.ledger_tail(10)
        self.assertEqual(len(tail), 1)
        row1_before = tail[0]

        # A second run of the identical, unchanged source must append a new
        # row, never touch the first.
        ingest.ingest_path(st, self.src, source="claude", actor="actor-b")
        tail = st.ledger_tail(10)
        self.assertEqual(len(tail), 2, "each import call appends a row")

        row1_after = next(r for r in tail if r["ledger_id"] == row1_before["ledger_id"])
        self.assertEqual(row1_after["started_at"], row1_before["started_at"])
        self.assertEqual(row1_after["actor"], row1_before["actor"])
        self.assertEqual(row1_after["finished_at"], row1_before["finished_at"])
        self.assertEqual(row1_after["outcome"], row1_before["outcome"])


class DigestDeterminismTest(LedgerTestCase):
    """Invariant 2: same source content -> same digest, across processes and
    working directories; a changed field -> a different digest.
    """

    def test_same_pairs_same_digest_regardless_of_cwd(self) -> None:
        pairs = [("b", "2026-01-02T00:00:00Z"), ("a", "2026-01-01T00:00:00Z")]
        d1 = digest.digest_items("claude-export", pairs)

        cwd = os.getcwd()
        elsewhere = Path(tempfile.mkdtemp(prefix="muninn-elsewhere-"))
        try:
            os.chdir(elsewhere)
            d2 = digest.digest_items("claude-export", pairs)
        finally:
            os.chdir(cwd)
            shutil.rmtree(elsewhere, ignore_errors=True)

        self.assertEqual(d1, d2, "digest must not depend on the working directory")
        self.assertTrue(d1.startswith("items-sha256:"))

    def test_changed_updated_at_changes_the_digest(self) -> None:
        base = [("a", "2026-01-01T00:00:00Z")]
        changed = [("a", "2026-01-01T00:00:01Z")]
        self.assertNotEqual(
            digest.digest_items("claude-export", base),
            digest.digest_items("claude-export", changed),
        )


class DuplicateByIdentityTest(LedgerTestCase):
    """Invariant 3: a second import of an unchanged source is `duplicate`,
    never `imported` with zero counts — the exact claudex misreading.
    """

    def test_actor_b_scenario_yields_duplicate_with_attribution(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "bbb.jsonl", "bbb",
            [("user", "q"), ("assistant", "a")],
        )
        st = store.open_store(self.db)

        result_a = ingest.ingest_path(st, self.src, source="claude", actor="actor-a")
        receipt_a = result_a.receipt
        self.assertIsNotNone(receipt_a)
        self.assertEqual(receipt_a.outcome, Outcome.IMPORTED)

        result_b = ingest.ingest_path(st, self.src, source="claude", actor="actor-b")
        receipt_b = result_b.receipt
        self.assertIsNotNone(receipt_b)

        self.assertEqual(receipt_b.outcome, Outcome.DUPLICATE)
        self.assertEqual(receipt_b.duplicate_of, receipt_a.ledger_id)
        # The misreading this whole subsystem exists to prevent: an empty
        # source and a duplicate source must never look alike.
        self.assertGreater(receipt_b.source.item_count, 0,
                           "a duplicate must not be readable as an empty source")


class SeparationTest(unittest.TestCase):
    """Invariant 4: source facts and run deltas never merge into one object."""

    def test_to_dict_keeps_source_and_delta_disjoint(self) -> None:
        receipt = ImportReceipt(
            ledger_id=1,
            outcome=Outcome.IMPORTED,
            source=SourceFacts(kind="claude-transcripts", digest="tree-sha256:x",
                               item_count=5, span_earliest="2026-01-01", span_latest="2026-01-02"),
            delta=Delta(added=5),
        )
        as_dict = receipt.to_dict()
        self.assertIn("source", as_dict)
        self.assertIn("delta", as_dict)

        delta_keys = {"added", "updated", "unchanged", "skipped"}
        source_keys = {"kind", "digest", "item_count", "span", "windowed"}
        self.assertTrue(delta_keys.isdisjoint(as_dict["source"].keys()),
                        "no delta key may appear under source")
        self.assertTrue(source_keys.isdisjoint(as_dict["delta"].keys()),
                        "no source key may appear under delta")
        # Round-trips through json.dumps cleanly (structured output requirement).
        json.loads(receipt.to_json())


class SkipsEnumeratedTest(LedgerTestCase):
    """Invariant 5: `skipped` equals len(skips); every reason is a closed
    vocabulary member, never free text.
    """

    def test_skipped_count_matches_enumerated_skips(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "good.jsonl", "good",
            [("user", "q"), ("assistant", "a")],
        )
        bad_path = self.src / "-tmp-p" / "bad.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text('{"placeholder": true}\n')

        original = sources.PARSERS["claude"]

        def flaky(path: Path, start_offset: int = 0):
            if path.name == "bad.jsonl":
                raise ValueError("simulated parser bug")
            return original(path, start_offset)

        st = store.open_store(self.db)
        with patched_parser("claude", flaky):
            result = ingest.ingest_path(st, self.src, source="claude")

        receipt = result.receipt
        self.assertEqual(receipt.delta.skipped, len(receipt.skips))
        self.assertEqual(receipt.delta.skipped, 1)
        for skip in receipt.skips:
            self.assertIsInstance(skip.reason, SkipReason)


class DuplicateItemInSourceTest(LedgerTestCase):
    """Regression: the same session id under two different discovered files
    in one scan must not crash on the `import_items` primary key.

    Caught against the real corpus, not fixtures: Claude Code writes the same
    sessionId under two different encoded-cwd project directories when a repo
    is renamed or symlinked (and the equivalent happens to subagent
    transcripts sharing a parent id). ``SkipReason.DUPLICATE_ITEM_IN_SOURCE``
    exists precisely for this shape; this test is the one that would have
    caught the missing wiring, since every fixture elsewhere in this suite
    uses unique stems.
    """

    def test_same_session_id_two_paths_yields_one_session_and_one_skip(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-project-a" / "shared.jsonl", "shared-session",
            [("user", "from project a"), ("assistant", "ok")],
        )
        write_claude_transcript(
            self.src / "-tmp-project-b" / "shared.jsonl", "shared-session",
            [("user", "from project b (should be skipped)"), ("assistant", "ok")],
        )

        st = store.open_store(self.db)
        result = ingest.ingest_path(st, self.src, source="claude")

        self.assertEqual(st.count_sessions(), 1, "one session row, not two, not a crash")
        receipt = result.receipt
        self.assertEqual(receipt.delta.added, 1)
        self.assertEqual(receipt.delta.skipped, 1)
        self.assertEqual(len(receipt.skips), 1)
        self.assertEqual(receipt.skips[0].reason, SkipReason.DUPLICATE_ITEM_IN_SOURCE)

        # Conservation must still close: item_count counts FILES (2), and
        # added(1) + updated(0) + unchanged(0) + skipped(1) == 2.
        d = receipt.delta
        self.assertEqual(d.added + d.updated + d.unchanged + d.skipped,
                         receipt.source.item_count)
        self.assertEqual(receipt.source.item_count, 2)

        # First path in sorted order wins deterministically: "-tmp-project-a"
        # sorts before "-tmp-project-b".
        self.assertIn("from project a", st.session_text("shared-session"))


class WindowedSafetyTest(LedgerTestCase):
    """Invariant 6: a windowed source may never mark a session missing."""

    def test_windowed_reconcile_never_marks_missing(self) -> None:
        path = self.src / "-tmp-p" / "ccc.jsonl"
        write_claude_transcript(path, "ccc", [("user", "q"), ("assistant", "a")])

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")
        path.unlink()  # the file that vanished

        marked = ingest._reconcile_missing(st, "claude", "2026-07-31T00:00:00+00:00",
                                           windowed=True)
        self.assertEqual(marked, 0, "windowed sources cannot support deletion claims")
        rec = st.get_session("ccc")
        self.assertEqual(rec["source_present"], 1)


class LockSerializesTest(LedgerTestCase):
    """Invariant 7: a concurrent import gets rejected with the holder's
    identity, not a race decided by filesystem timing.
    """

    def test_second_import_rejected_while_lock_held(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "ddd.jsonl", "ddd",
            [("user", "q"), ("assistant", "a")],
        )
        st = store.open_store(self.db)
        # Take the lock as if some other actor's process holds it. Our own
        # pid is guaranteed alive, which is exactly the "live holder" case
        # the invariant is about.
        st.acquire_import_lock(999, "other-actor", os.getpid())

        result = ingest.ingest_path(st, self.src, source="claude", actor="cli")
        receipt = result.receipt
        self.assertEqual(receipt.outcome, Outcome.REJECTED)
        self.assertIsNotNone(receipt.attribution)
        self.assertEqual(receipt.attribution.actor, "other-actor")

        # The archive must be untouched — the loser never got to write.
        self.assertEqual(st.count_sessions(), 0)


class ErrorClassNameTest(LedgerTestCase):
    """Invariant 8: `error` is an exception class name only, never a message
    (which could embed transcript text or credentials).
    """

    def test_forced_parser_exception_records_class_name_only(self) -> None:
        bad_path = self.src / "-tmp-p" / "bad.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text('{"placeholder": true}\n')

        def always_raises(path: Path, start_offset: int = 0):
            raise ValueError("this message must never reach the ledger")

        st = store.open_store(self.db)
        with patched_parser("claude", always_raises):
            result = ingest.ingest_path(st, self.src, source="claude")

        tail = st.ledger_tail(1)
        error = tail[0]["error"]
        self.assertIsNotNone(error)
        self.assertRegex(error, r"^[A-Za-z_][A-Za-z0-9_]*$")
        self.assertNotIn(" ", error)
        self.assertEqual(error, "ValueError")
        self.assertNotIn("this message must never reach the ledger", error)
        self.assertIsNotNone(result.receipt)


class CrashVisibleTest(LedgerTestCase):
    """Invariant 9: a row with finished_at IS NULL is reported, never
    silently reaped.
    """

    def test_incomplete_import_appears_in_incomplete_imports_and_doctor(self) -> None:
        st = store.open_store(self.db)
        facts = SourceFacts(kind="claude-transcripts", digest="tree-sha256:deadbeef",
                            item_count=0)
        ledger_id = st.begin_import(actor="cli", source_kind="claude-transcripts",
                                    source_ref=str(self.src), source_digest="tree-sha256:deadbeef",
                                    facts=facts)
        # Deliberately never call finish_import: simulates a crash mid-import.
        st.commit()
        st.close()

        st = store.open_store(self.db)
        incomplete = st.incomplete_imports()
        self.assertTrue(any(r["ledger_id"] == ledger_id for r in incomplete))
        st.close()

        parser = cli.build_parser()
        args = parser.parse_args(["--db", str(self.db), "doctor"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            args.func(args)
        output = buf.getvalue()
        self.assertIn("incomplete import", output.lower())
        self.assertIn(f"#{ledger_id}", output)


class MigrationTest(LedgerTestCase):
    """Invariant 10 (acceptance criterion): a v1 archive gains ledger tables
    without losing existing data, and without requiring a rebuild.
    """

    def test_v1_archive_gains_ledger_tables_and_keeps_sessions(self) -> None:
        # Build a v1 archive by hand: the pre-ledger schema plus a
        # schema_version of "1", exactly what shipped before this spec.
        conn = sqlite3.connect(str(self.db))
        conn.executescript(store._SCHEMA)
        conn.execute(
            "INSERT INTO sessions (session_id, source, provenance, text) "
            "VALUES ('legacy1', 'claude', 'human', 'irreplaceable prose')")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
        conn.commit()
        conn.close()

        st = store.open_store(self.db)

        tables = {
            row["name"] for row in st.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for expected in ("import_ledger", "import_items", "import_lock"):
            self.assertIn(expected, tables)

        rec = st.get_session("legacy1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["text"], "irreplaceable prose")

        version = st.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()["value"]
        self.assertEqual(int(version), store.SCHEMA_VERSION)
        st.close()


class ConservationTest(LedgerTestCase):
    """Invariant 10 [schema doc numbering] / 11 [spec numbering]: the
    arithmetic must close, and a mismatch raises rather than being silently
    absorbed.
    """

    def test_matching_sum_does_not_raise(self) -> None:
        assert_conservation(added=1, updated=0, unchanged=0, skipped=1, item_count=2)

    def test_mismatched_sum_raises_conservation_error(self) -> None:
        with self.assertRaises(ConservationError):
            assert_conservation(added=1, updated=0, unchanged=0, skipped=0, item_count=2)

    def test_mixed_good_and_unparseable_item_conserves(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "good.jsonl", "good",
            [("user", "q"), ("assistant", "a")],
        )
        bad_path = self.src / "-tmp-p" / "bad.jsonl"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text('{"placeholder": true}\n')
        original = sources.PARSERS["claude"]

        def flaky(path: Path, start_offset: int = 0):
            if path.name == "bad.jsonl":
                raise RuntimeError("simulated")
            return original(path, start_offset)

        st = store.open_store(self.db)
        with patched_parser("claude", flaky):
            result = ingest.ingest_path(st, self.src, source="claude")

        d = result.receipt.delta
        total = d.added + d.updated + d.unchanged + d.skipped
        self.assertEqual(total, result.receipt.source.item_count)


class NoIdCoercionTest(LedgerTestCase):
    """Invariant 11 [schema doc] / 12 [spec]: a missing/empty item id is a
    named skip, never coerced into a shared placeholder id that silently
    overwrites the previous session.
    """

    def test_two_idless_items_produce_two_skips_not_one_overwritten_row(self) -> None:
        path1 = self.src / "-tmp-p" / "idless1.jsonl"
        path2 = self.src / "-tmp-p" / "idless2.jsonl"
        path1.parent.mkdir(parents=True, exist_ok=True)
        path1.write_text('{"placeholder": 1}\n')
        path2.write_text('{"placeholder": 2}\n')

        def blank_id_parser(path: Path, start_offset: int = 0):
            return sources.ParsedSession(session_id="", source="claude", text="x"), 0

        st = store.open_store(self.db)
        with patched_parser("claude", blank_id_parser):
            result = ingest.ingest_path(st, self.src, source="claude")

        receipt = result.receipt
        self.assertEqual(receipt.delta.skipped, 2, "two id-less items => two skips")
        self.assertEqual(receipt.delta.added, 0)
        self.assertEqual(receipt.delta.updated, 0)
        for skip in receipt.skips:
            self.assertEqual(skip.reason, SkipReason.MISSING_ITEM_ID)
        # No session row exists at all -- neither a coerced "unknown" row nor
        # any row for either file, since no valid id was ever produced.
        self.assertEqual(st.count_sessions(), 0)
        item_ids = {s.item_id for s in receipt.skips}
        self.assertEqual(len(item_ids), 2, "each id-less item keeps a distinct ledger identity")


class DigestStabilityTest(unittest.TestCase):
    """Acceptance criterion 13: digest_items is stable across working
    directories and sensitive to source_kind.
    """

    def test_stable_across_cwd_and_sensitive_to_source_kind(self) -> None:
        pairs = [("id1", "2026-01-01T00:00:00Z"), ("id2", "5000000000.123456")]
        d1 = digest.digest_items("claude-export", pairs)

        cwd = os.getcwd()
        elsewhere = Path(tempfile.mkdtemp(prefix="muninn-cwd-"))
        try:
            os.chdir(elsewhere)
            d2 = digest.digest_items("claude-export", pairs)
        finally:
            os.chdir(cwd)
            shutil.rmtree(elsewhere, ignore_errors=True)
        self.assertEqual(d1, d2)

        d3 = digest.digest_items("chatgpt-export", pairs)
        self.assertNotEqual(d1, d3, "source_kind must be part of the preimage")


class WhyReproductionTest(LedgerTestCase):
    """The exact reproduction from spec 001's "Why" section: two imports of
    the same source by different actors must not let the second read as
    "nothing new." The CLI's human-readable line must say "duplicate of
    import #<n>" explicitly.
    """

    def test_actor_b_cli_output_says_duplicate_of_import(self) -> None:
        write_claude_transcript(
            self.src / "-tmp-p" / "eee.jsonl", "eee",
            [("user", "q"), ("assistant", "a")],
        )
        st = store.open_store(self.db)
        result_a = ingest.ingest_path(st, self.src, source="claude", actor="actor-a")
        ledger_id_a = result_a.receipt.ledger_id

        result_b = ingest.ingest_path(st, self.src, source="claude", actor="actor-b")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_index_result("claude", result_b)
        output = buf.getvalue()

        self.assertIn(f"duplicate of import #{ledger_id_a}", output)
        # And the false claim from the incident must be structurally absent:
        # nothing in this receipt can be read as "the source contained
        # nothing new" -- item_count is still positive.
        self.assertGreater(result_b.receipt.source.item_count, 0)
        self.assertNotIn("nothing new", output)


if __name__ == "__main__":
    unittest.main()
