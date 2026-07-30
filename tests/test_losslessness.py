"""The losslessness contract.

Muninn's index is an archive of record. Claude Code deletes session transcripts
after ``cleanupPeriodDays`` (default 30), so for much of a corpus the index is
the ONLY surviving copy. These tests define what "ingest never loses data"
means. They are the contract the ingest layer must satisfy, and they were
written before the ingest code existed.

See .valholl/articles/archive-of-record.md and
.valholl/articles/unstable-jsonl-format.md.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import ingest, store


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


class LosslessnessTest(unittest.TestCase):
    """Round-trip fidelity: what goes in must come out."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-lossless-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prose_survives_round_trip(self) -> None:
        """Every word of user/assistant prose is recoverable after ingest."""
        sentinel_user = "the raven remembers the auth redirect decision"
        sentinel_asst = "we dropped the proxy and trusted the sdk chain"
        write_claude_transcript(
            self.src / "-tmp-project" / "aaa.jsonl", "aaa",
            [("user", sentinel_user), ("assistant", sentinel_asst)],
        )

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        text = st.session_text("aaa")
        self.assertIn(sentinel_user, text)
        self.assertIn(sentinel_asst, text)

    def test_metadata_survives_round_trip(self) -> None:
        """Structured metadata is preserved, not just prose."""
        write_claude_transcript(
            self.src / "-tmp-widget" / "bbb.jsonl", "bbb",
            [("user", "hello"), ("assistant", "hi")],
            cwd="/tmp/widget", branch="feature/xyz", model="claude-opus-5",
        )

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        rec = st.get_session("bbb")
        self.assertEqual(rec["cwd"], "/tmp/widget")
        self.assertEqual(rec["branch"], "feature/xyz")
        self.assertEqual(rec["model"], "claude-opus-5")
        self.assertEqual(rec["source"], "claude")
        self.assertEqual(rec["user_turns"], 1)
        self.assertEqual(rec["assistant_turns"], 1)
        self.assertGreater(rec["words"], 0)

    def test_unicode_and_long_lines_survive(self) -> None:
        """Non-ASCII and very long single messages are not truncated or mangled."""
        weird = "emoji \U0001f426 and éèê and 中文 plus a NUL: \x00 byte"
        long_text = "lorem ipsum " * 20_000  # ~240 KB single message
        write_claude_transcript(
            self.src / "-tmp-uni" / "ccc.jsonl", "ccc",
            [("user", weird), ("assistant", long_text)],
        )

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        text = st.session_text("ccc")
        self.assertIn("\U0001f426", text)
        self.assertIn("中文", text)
        self.assertIn("lorem ipsum", text)
        # the long message must be present in full, not clipped
        self.assertGreaterEqual(text.count("lorem ipsum"), 20_000)


class IdempotenceTest(unittest.TestCase):
    """Re-ingesting must be a no-op, never a duplicate and never a loss."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-idem-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"
        write_claude_transcript(
            self.src / "-tmp-p" / "ddd.jsonl", "ddd",
            [("user", "first question"), ("assistant", "first answer")],
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_double_ingest_does_not_duplicate(self) -> None:
        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")
        first_sessions = st.count_sessions()
        first_chunks = st.count_chunks()

        ingest.ingest_path(st, self.src, source="claude")

        self.assertEqual(st.count_sessions(), first_sessions)
        self.assertEqual(st.count_chunks(), first_chunks)

    def test_append_then_reingest_captures_new_turns(self) -> None:
        """An appended transcript gains the new prose without losing the old."""
        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        path = self.src / "-tmp-p" / "ddd.jsonl"
        with path.open("a") as fh:
            fh.write(json.dumps({
                "type": "user", "timestamp": "2026-07-11T00:00:00.000Z",
                "sessionId": "ddd", "cwd": "/tmp/project",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": "second question"}]},
            }) + "\n")

        ingest.ingest_path(st, self.src, source="claude")

        text = st.session_text("ddd")
        self.assertIn("first question", text)   # old prose retained
        self.assertIn("second question", text)  # new prose captured
        self.assertEqual(st.count_sessions(), 1)


class SourceDeletionTest(unittest.TestCase):
    """The archive outlives its source. This is the whole point."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-sweep-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archive_survives_source_deletion(self) -> None:
        """After the source JSONL is swept, the prose is still searchable."""
        sentinel = "irreplaceable knowledge about the reconnect storm"
        path = self.src / "-tmp-p" / "eee.jsonl"
        write_claude_transcript(path, "eee",
                                [("user", sentinel), ("assistant", "understood")])

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        path.unlink()  # Claude Code's 30-day sweep
        self.assertFalse(path.exists())

        self.assertIn(sentinel, st.session_text("eee"))
        hits = st.search("reconnect storm")
        self.assertTrue(any(h["session_id"] == "eee" for h in hits))

    def test_reingest_after_deletion_does_not_erase(self) -> None:
        """A sweep pass over a vanished source must not remove archived rows.

        This is the most dangerous failure mode: a reconciling indexer that
        "cleans up" sessions it can no longer see on disk would silently
        destroy the only surviving copy.
        """
        path = self.src / "-tmp-p" / "fff.jsonl"
        write_claude_transcript(path, "fff",
                                [("user", "precious"), ("assistant", "ok")])

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")
        path.unlink()

        ingest.ingest_path(st, self.src, source="claude")  # source now empty

        self.assertEqual(st.count_sessions(), 1)
        self.assertIn("precious", st.session_text("fff"))


class SubagentTest(unittest.TestCase):
    """Subagents are a distinct population and must not collapse onto parents.

    Regression: subagent transcripts carry the PARENT's ``sessionId``. Trusting
    that field made every subagent upsert onto the parent row, silently dropping
    251 of 384 real transcripts with no error.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-subagent-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_subagents_are_separate_sessions(self) -> None:
        parent_dir = self.src / "-tmp-p"
        write_claude_transcript(parent_dir / "parent1.jsonl", "parent1",
                                [("user", "parent prose"), ("assistant", "ok")])
        # Both subagents carry the parent's sessionId, as Claude Code writes them.
        for name, prose in (("agent-aaa", "first subagent prose"),
                            ("agent-bbb", "second subagent prose")):
            write_claude_transcript(
                parent_dir / "parent1" / "subagents" / f"{name}.jsonl",
                "parent1", [("user", prose), ("assistant", "done")])

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        self.assertEqual(st.count_sessions(), 3, "parent + 2 subagents")
        self.assertIn("parent prose", st.session_text("parent1"))
        self.assertIn("first subagent prose", st.session_text("agent-aaa"))
        self.assertIn("second subagent prose", st.session_text("agent-bbb"))

        sub = st.get_session("agent-aaa")
        self.assertEqual(sub["provenance"], "subagent")
        self.assertEqual(sub["parent_id"], "parent1")
        self.assertEqual(st.get_session("parent1")["provenance"], "human")


class ArchivalScopeTest(unittest.TestCase):
    """The archive-of-record guarantee scopes to human and subagent sessions.

    Tool-invoked sessions are reproducible byproducts, and some are outright bug
    residue: 3,534 on the development machine came from Huginn writing
    transcripts into a cache directory it then deleted. Those are prunable
    without loss. Keeping them would tie archive growth to another tool's call
    volume rather than to actual work.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-scope-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_tool_invoked_is_classified_and_prunable(self) -> None:
        # A cache-dir session: exactly the Huginn blurb-call shape.
        write_claude_transcript(
            self.src / "-cache" / "tool1.jsonl", "tool1",
            [("user", "summarize this session"), ("assistant", "a blurb")],
            cwd="/Users/x/.local/state/huginn/cache",
        )
        write_claude_transcript(
            self.src / "-tmp-p" / "real1.jsonl", "real1",
            [("user", "genuine question"), ("assistant", "genuine answer")],
        )

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")

        self.assertEqual(st.get_session("tool1")["provenance"], "tool-invoked")
        self.assertEqual(st.get_session("real1")["provenance"], "human")

        pruned = st.prune_tool_invoked()
        self.assertEqual(pruned, 1)

        # The human session is untouched, prose intact.
        self.assertIn("genuine question", st.session_text("real1"))
        # The tool-invoked row is countable but its prose is gone.
        rec = st.get_session("tool1")
        self.assertIsNotNone(rec, "counts are retained for statistics")
        self.assertEqual(rec["text"], "")
        self.assertEqual(rec["user_turns"], 1, "metadata survives pruning")

    def test_prune_never_touches_human_or_subagent(self) -> None:
        parent = self.src / "-tmp-p"
        write_claude_transcript(parent / "p.jsonl", "p",
                                [("user", "human prose here"), ("assistant", "ok")])
        write_claude_transcript(
            parent / "p" / "subagents" / "agent-x.jsonl", "p",
            [("user", "subagent prose here"), ("assistant", "ok")])

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")
        st.prune_tool_invoked()

        self.assertIn("human prose here", st.session_text("p"))
        self.assertIn("subagent prose here", st.session_text("agent-x"))


class DegradationTest(unittest.TestCase):
    """Malformed input degrades; it never aborts or loses good data."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-degrade-"))
        self.src = self.tmp / "projects"
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_malformed_lines_do_not_lose_good_lines(self) -> None:
        path = self.src / "-tmp-p" / "ggg.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {"type": "user", "timestamp": "2026-07-10T00:00:00.000Z",
                "sessionId": "ggg", "cwd": "/tmp/p",
                "message": {"role": "user",
                            "content": [{"type": "text", "text": "salvageable prose"}]}}
        with path.open("w") as fh:
            fh.write("{not json at all\n")
            fh.write(json.dumps(good) + "\n")
            fh.write("\n")                      # blank line
            fh.write('{"type":"user"}\n')       # missing everything
            fh.write("partial line without newline")

        st = store.open_store(self.db)
        result = ingest.ingest_path(st, self.src, source="claude")

        self.assertIn("salvageable prose", st.session_text("ggg"))
        self.assertGreater(result.parse_failures, 0)

    def test_unknown_fields_are_tolerated(self) -> None:
        """Upstream may add or rename fields at any release; we must not crash."""
        path = self.src / "-tmp-p" / "hhh.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"type": "user", "timestamp": "2026-07-10T00:00:00.000Z",
                 "sessionId": "hhh", "cwd": "/tmp/p",
                 "someFutureField": {"nested": [1, 2, 3]},
                 "message": {"role": "user", "futureKey": True,
                             "content": [{"type": "text", "text": "forward compatible"},
                                         {"type": "brand_new_block", "payload": "ignored"}]}}
        with path.open("w") as fh:
            fh.write(json.dumps(entry) + "\n")

        st = store.open_store(self.db)
        ingest.ingest_path(st, self.src, source="claude")
        self.assertIn("forward compatible", st.session_text("hhh"))


if __name__ == "__main__":
    unittest.main()
