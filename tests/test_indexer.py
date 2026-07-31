"""The background indexer contract: hook-drain, sweep, watch, rewrite handling.

Acceptance criteria 5-10 from docs/specs/003-background-indexer.md. See
.valholl/articles/session-lifecycle-facts.md for why the hook may only
enqueue and why the sweep is not optional — these tests exercise exactly the
gaps that design implies: a hook whose job never gets drained, a hook that
never fired at all, and a rewritten transcript (/compact, /clear, rotation).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from muninn import indexer, ingest, queue, store
from muninn.receipt import Outcome


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


class IndexerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-indexer-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"
        self.qdir = self.tmp / "queue"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class HookPathTest(IndexerTestCase):
    """Acceptance 5: enqueue a job for a real transcript, drain_once imports it."""

    def test_drain_once_imports_enqueued_transcript(self) -> None:
        path = self.src / "-tmp-p" / "hookish.jsonl"
        write_claude_transcript(path, "hookish", [("user", "hook question"), ("assistant", "hook answer")])

        queue.enqueue(
            {"v": 1, "kind": "session-end", "session_id": "hookish",
             "transcript_path": str(path), "cwd": "/tmp/p", "reason": "clear",
             "enqueued_at": "2026-07-31T00:00:00Z"},
            queue_dir=self.qdir,
        )

        st = store.open_store(self.db)
        receipts = indexer.drain_once(st, queue_dir=self.qdir)

        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].outcome, Outcome.IMPORTED)
        rec = st.get_session("hookish")
        self.assertIsNotNone(rec)
        self.assertIn("hook question", st.session_text("hookish"))
        st.close()


class SweepCatchesMissedHookTest(IndexerTestCase):
    """Acceptance 6: a transcript with no job is still caught by sweep()."""

    def test_sweep_indexes_transcript_with_no_queued_job(self) -> None:
        path = self.src / "-tmp-p" / "missed.jsonl"
        write_claude_transcript(path, "missed", [("user", "no hook fired"), ("assistant", "ok")])

        st = store.open_store(self.db)
        self.assertIsNone(st.get_session("missed"))

        receipts = indexer.sweep(st, {"claude": self.src})
        self.assertTrue(any(r.outcome == Outcome.IMPORTED for r in receipts))
        self.assertIsNotNone(st.get_session("missed"))
        self.assertIn("no hook fired", st.session_text("missed"))
        # The sweep timestamp doctor reports on must be recorded.
        self.assertIsNotNone(st.last_sweep_at())
        st.close()


class SweepBeforeWatchTest(IndexerTestCase):
    """Acceptance 7: watch() sweeps before its first event wait."""

    def test_watch_sweeps_before_first_event(self) -> None:
        path = self.src / "-tmp-p" / "prewatch.jsonl"
        write_claude_transcript(path, "prewatch", [("user", "already here before watch started"),
                                                   ("assistant", "ok")])

        st = store.open_store(self.db)

        def fake_watch_fn(paths, *, interval_s, force_polling):
            # Never yields real changes; watch() should have already swept
            # by the time this generator is even asked for its first value.
            yield set()

        indexer.watch(st, {"claude": self.src}, queue_dir=self.qdir,
                      max_iterations=1, watch_fn=fake_watch_fn)

        # If watch() had NOT swept before entering the loop, this session
        # (which has no queued job and no file event) would be absent.
        self.assertIsNotNone(st.get_session("prewatch"))
        st.close()

    def test_watch_calls_sweep_exactly_once_before_the_loop(self) -> None:
        st = store.open_store(self.db)
        calls: list[str] = []
        original_sweep = indexer.sweep

        def spy_sweep(st_arg, roots, **kwargs):
            calls.append("sweep")
            return original_sweep(st_arg, roots, **kwargs)

        def fake_watch_fn(paths, *, interval_s, force_polling):
            calls.append("watch")
            yield set()

        indexer.sweep = spy_sweep
        try:
            indexer.watch(st, {"claude": self.src}, queue_dir=self.qdir,
                          max_iterations=1, watch_fn=fake_watch_fn)
        finally:
            indexer.sweep = original_sweep

        self.assertEqual(calls[0], "sweep", "sweep must happen before the first watch event")
        st.close()


class RewriteDetectionTest(IndexerTestCase):
    """Acceptance 8: a truncated/rewritten file is re-parsed whole, not tailed."""

    def test_truncated_transcript_reparsed_no_stale_text(self) -> None:
        path = self.src / "-tmp-p" / "rewritten.jsonl"
        write_claude_transcript(
            path, "rewritten",
            [("user", "original long question that will not survive compaction"),
             ("assistant", "original long answer that will not survive compaction")],
        )

        st = store.open_store(self.db)
        indexer.sweep(st, {"claude": self.src})
        self.assertIn("original long question", st.session_text("rewritten"))

        # Simulate /compact: the file shrinks below its previously recorded
        # offset and its content is entirely different.
        write_claude_transcript(path, "rewritten", [("user", "compacted summary"), ("assistant", "ack")])

        indexer.sweep(st, {"claude": self.src})
        text = st.session_text("rewritten")
        self.assertIn("compacted summary", text)
        self.assertNotIn("original long question", text,
                         "stale prose from before the rewrite must not linger")
        st.close()


class IdempotenceTest(IndexerTestCase):
    """Acceptance 9: drain_once twice for the same job yields `duplicate` the
    second time — never a second `imported` that could misread as new data.
    """

    def test_second_drain_of_unchanged_transcript_is_duplicate(self) -> None:
        path = self.src / "-tmp-p" / "idem.jsonl"
        write_claude_transcript(path, "idem", [("user", "q"), ("assistant", "a")])

        st = store.open_store(self.db)
        job = {"v": 1, "kind": "session-end", "session_id": "idem",
               "transcript_path": str(path), "cwd": "/tmp/p", "reason": "clear",
               "enqueued_at": "2026-07-31T00:00:00Z"}

        queue.enqueue(job, queue_dir=self.qdir)
        receipts_1 = indexer.drain_once(st, queue_dir=self.qdir)
        self.assertEqual(receipts_1[0].outcome, Outcome.IMPORTED)

        # Re-enqueue the identical job (as if the watcher also reacted, or
        # the hook fired twice) and drain again with no change to the file.
        queue.enqueue(job, queue_dir=self.qdir)
        receipts_2 = indexer.drain_once(st, queue_dir=self.qdir)
        self.assertEqual(len(receipts_2), 1)
        self.assertEqual(receipts_2[0].outcome, Outcome.DUPLICATE)
        self.assertEqual(receipts_2[0].duplicate_of, receipts_1[0].ledger_id)

        self.assertEqual(st.count_sessions(), 1, "no duplicate session row was created")
        st.close()


class ConcurrentImportTest(IndexerTestCase):
    """The watcher + hook-drain + sweep design makes concurrent imports of the
    same source routine rather than a rare human-racing-themselves accident,
    so the ledger's serialization must hold up under real thread concurrency,
    not just the single-threaded lock-already-held simulation in
    tests/test_ledger.py's LockSerializesTest.

    Regression coverage for two bugs a 4-thread stress test surfaced:

    1. ``store.open_store()`` had no busy timeout, so a sweep and a drain
       opening the database in the same instant could crash the whole process
       with "database is locked" rather than waiting the few milliseconds a
       competing writer needed.
    2. ``find_import_by_digest()`` was read BEFORE the import lock was
       acquired, so two racing imports could both observe "no prior completed
       row" and both report ``imported`` — one of them with
       added=0/updated=0/unchanged=N, the exact "0 written, 61 cached"
       ambiguity deterministic-imports.md exists to eliminate, just produced
       by a race instead of a stale re-run.
    """

    def test_n_concurrent_imports_yield_one_imported_rest_duplicate(self) -> None:
        n_threads = 6
        for i in range(20):
            write_claude_transcript(
                self.src / "-tmp-p" / f"concurrent{i}.jsonl", f"concurrent{i}",
                [("user", f"question {i}"), ("assistant", f"answer {i}")],
            )

        results: dict[str, str] = {}
        errors: list[tuple[str, BaseException]] = []

        def worker(actor: str) -> None:
            try:
                st = store.open_store(self.db)
                result = ingest.ingest_path(st, self.src, source="claude", actor=actor)
                results[actor] = result.receipt.outcome.value
                st.close()
            except BaseException as exc:  # pragma: no cover - this is what must not happen
                errors.append((actor, exc))

        threads = [threading.Thread(target=worker, args=(f"agent:{i}",))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"no import may raise under concurrency: {errors}")

        outcomes = list(results.values())
        self.assertEqual(outcomes.count("imported"), 1,
                         f"exactly one thread should report imported, got {outcomes}")
        # Every OTHER thread must be `duplicate` (it observed the winner's
        # completed row once serialized) or `rejected` (it lost the lock race
        # outright) -- never a second `imported`, which is the ambiguity bug.
        for actor, outcome in results.items():
            if outcome == "imported":
                continue
            self.assertIn(outcome, ("duplicate", "rejected"),
                         f"{actor} reported {outcome!r}, expected duplicate or rejected")

        st = store.open_store(self.db)
        self.assertEqual(st.count_sessions(), 20, "no session was lost or duplicated")
        lock_rows = st.conn.execute("SELECT COUNT(*) n FROM import_lock").fetchone()["n"]
        self.assertEqual(lock_rows, 0, "the import lock must be released, not orphaned")
        st.close()


class HookIsCheapTest(unittest.TestCase):
    """Acceptance 10: muninn.hooks.cli must never import sqlite3 or muninn.store.

    This is THE test that keeps the SessionEnd 1.5s shared budget safe (see
    .valholl/articles/session-lifecycle-facts.md). It runs main() in a
    subprocess and inspects sys.modules afterward so a future edit that adds
    "just one" heavy import to muninn/hooks/cli.py fails loudly here rather
    than silently degrading everyone's SessionEnd hooks.
    """

    def test_hook_process_never_imports_sqlite3_or_store(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muninn-hookcheap-"))
        try:
            qdir = tmp / "queue"
            script = (
                "import sys, json, io\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / 'muninn')!r})\n"
                "from muninn.hooks import cli as hookcli\n"
                "payload = json.dumps({'session_id': 's', 'transcript_path': '/dev/null', "
                "'cwd': '/tmp', 'reason': 'clear'})\n"
                "sys.stdin = io.StringIO(payload)\n"
                f"rc = hookcli.main(['session-end', '--queue-dir', {str(qdir)!r}])\n"
                "bad = [m for m in ('sqlite3', 'muninn.store', 'muninn.ingest', 'muninn.indexer') "
                "if m in sys.modules]\n"
                "print(json.dumps({'rc': rc, 'bad_modules': bad}))\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
            out = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(out["rc"], 0, "the hook must exit 0")
            self.assertEqual(out["bad_modules"], [],
                             "muninn.hooks.cli pulled in a heavy module the SessionEnd "
                             "1.5s budget cannot afford")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_self_test_creates_a_job_and_exits_zero(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muninn-selftest-"))
        try:
            qdir = tmp / "queue"
            proc = subprocess.run(
                [sys.executable, "-m", "muninn.hooks.cli", "session-end",
                 "--self-test", "--queue-dir", str(qdir)],
                capture_output=True, text=True, timeout=10, input="",
            )
            self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}")
            jobs = list(qdir.glob("*.json"))
            self.assertEqual(len(jobs), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hook_exits_zero_on_malformed_stdin(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muninn-malformed-"))
        try:
            qdir = tmp / "queue"
            proc = subprocess.run(
                [sys.executable, "-m", "muninn.hooks.cli", "session-end",
                 "--queue-dir", str(qdir)],
                capture_output=True, text=True, timeout=10,
                input="this is not json{{{",
            )
            self.assertEqual(proc.returncode, 0, "a failing hook must never disrupt the session")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_hook_exits_zero_on_empty_stdin(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="muninn-empty-"))
        try:
            qdir = tmp / "queue"
            proc = subprocess.run(
                [sys.executable, "-m", "muninn.hooks.cli", "session-end",
                 "--queue-dir", str(qdir)],
                capture_output=True, text=True, timeout=10, input="",
            )
            self.assertEqual(proc.returncode, 0)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class InstallerIdempotenceTest(unittest.TestCase):
    """The installer never clobbers settings.json and never duplicates on re-run."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-installer-"))
        self.settings_path = self.tmp / "settings.json"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_twice_yields_one_entry_and_preserves_unrelated_keys(self) -> None:
        from muninn.hooks import install as hooks_install

        original = {
            "alwaysThinkingEnabled": True,
            "hooks": {
                "CwdChanged": [{"hooks": [{"type": "command", "command": "some-other-hook.sh"}]}],
                "SessionEnd": [{"hooks": [{"type": "command", "command": "some-other-tools-hook"}]}],
            },
            "env": {"FOO": "bar"},
        }
        self.settings_path.write_text(json.dumps(original, indent=2))

        r1 = hooks_install.install(settings_path=self.settings_path, command="muninn-hook session-end")
        self.assertTrue(r1.changed)
        self.assertIsNotNone(r1.backup_path)
        self.assertTrue(r1.backup_path.exists())

        after_first = json.loads(self.settings_path.read_text())
        self.assertEqual(after_first["alwaysThinkingEnabled"], True)
        self.assertEqual(after_first["env"], {"FOO": "bar"})
        session_end = after_first["hooks"]["SessionEnd"]
        self.assertEqual(len(session_end), 2, "the other tool's SessionEnd entry must survive")
        cwd_changed = after_first["hooks"]["CwdChanged"]
        self.assertEqual(cwd_changed, original["hooks"]["CwdChanged"])

        r2 = hooks_install.install(settings_path=self.settings_path, command="muninn-hook session-end")
        self.assertFalse(r2.changed, "a second run with nothing new to add must be a no-op")

        after_second = json.loads(self.settings_path.read_text())
        self.assertEqual(after_second, after_first, "re-running must not duplicate the entry")

        muninn_entries = [
            h for block in after_second["hooks"]["SessionEnd"] for h in block["hooks"]
            if h.get("command") == "muninn-hook session-end"
        ]
        self.assertEqual(len(muninn_entries), 1)

    def test_check_only_never_writes(self) -> None:
        from muninn.hooks import install as hooks_install

        original_text = json.dumps({"env": {"X": "1"}}, indent=2)
        self.settings_path.write_text(original_text)
        before_mtime = self.settings_path.stat().st_mtime_ns

        result = hooks_install.install(
            settings_path=self.settings_path, command="muninn-hook session-end", check_only=True)
        self.assertFalse(result.changed)

        after_mtime = self.settings_path.stat().st_mtime_ns
        self.assertEqual(before_mtime, after_mtime, "--check must never write")
        self.assertEqual(self.settings_path.read_text(), original_text)
        self.assertFalse((self.settings_path.parent / "settings.json.muninn-bak").exists())

    def test_missing_settings_file_creates_one(self) -> None:
        from muninn.hooks import install as hooks_install

        self.assertFalse(self.settings_path.exists())
        result = hooks_install.install(
            settings_path=self.settings_path, command="muninn-hook session-end")
        self.assertTrue(result.changed)
        self.assertTrue(self.settings_path.exists())
        data = json.loads(self.settings_path.read_text())
        self.assertEqual(
            data["hooks"]["SessionEnd"][0]["hooks"][0]["command"], "muninn-hook session-end")


if __name__ == "__main__":
    unittest.main()
