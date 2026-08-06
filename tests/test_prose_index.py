"""Backfilling the predecessors' indexes, which hold the only copy of a lot.

`~/.claudex/index` covers sessions whose raw transcripts Claude Code swept
months ago. Archiving the repositories is harmless; concluding that the
*indexes* are therefore disposable would destroy the oldest part of the corpus.
So this suite is really one question asked several ways: **after a backfill, is
every word still there, and is the archive honest about what it now holds?**

The fixtures below reproduce the three real shapes byte for byte — claudex's
``kind``/``parent``/``branch`` headers, its ``cloud/index`` sub-index with
``name`` and no ``cwd``, and codexdex's ``source``/``path``/``model``/``title``
— because a fixture that agreed with the parser rather than with the tools would
pass while the importer silently dropped a field. No test reads a real
``~/.claudex`` or ``~/.codexdex``.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from muninn import prose_index
from muninn.receipt import Outcome, SkipReason
from muninn.store import open_store

CLAUDEX_SESSION = """\
# session: {sid}
# kind: session
# cwd: {cwd}
# start: 2026-05-05T04:20:46.974Z
# end: 2026-05-05T04:21:12.564Z
# branch: HEAD
# turns: user={user} assistant={asst}

[USER 2026-05-05T04:20:46.974Z]
{prose}

[ASSISTANT 2026-05-05T04:21:12.564Z]
and the assistant answered at length
"""

CLAUDEX_SUBAGENT = """\
# session: agent-a02c488097593422f
# kind: subagent
# parent: 3b3cb1b5-696a-496e-8ea0-7ccaa0430921
# cwd: /Users/x/Projects/eddic
# start: 2026-07-19T15:21:17.934Z
# end: 2026-07-19T15:22:18.509Z
# branch: master
# turns: user=1 assistant=2

[USER 2026-07-19T15:21:17.934Z]
{prose}
"""

CLAUDEX_CLOUD = """\
# session: {sid}
# kind: cloud
# name: Eastern to Bangalore time conversion
# start: 2026-04-28T02:13:17.874139Z
# end: 2026-04-28T02:13:25.452825Z
# turns: user=1 assistant=1

[NAME]
Eastern to Bangalore time conversion

[USER 2026-04-28T02:13:17.874139Z]
{prose}
"""

CODEXDEX_SESSION = """\
# session: {sid}
# source: codex
# title: {title}
# cwd: /Users/x/Projects/orlog
# path: /Users/x/.codex/sessions/2026/07/16/rollout-2026-07-16T22-44-28-{sid}.jsonl
# start: 2026-07-17T02:44:28.077Z
# end: 2026-07-17T02:44:28.086Z
# model: gpt-5
# turns: user=3 assistant=242

[USER 2026-07-17T02:44:28.077Z]
{prose}
"""


class _Index(unittest.TestCase):
    """A tempdir predecessor index plus a fresh archive."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-prose-"))
        self.root = self.tmp / ".claudex"
        (self.root / "index").mkdir(parents=True)
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        try:
            self.st.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name: str, content: str, *, sub: str = "index") -> Path:
        path = self.root / sub / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def session(self, sid: str, prose: str, *, cwd: str = "/Users/x/Projects/muninn",
                user: int = 1, asst: int = 1) -> Path:
        return self.write(f"{sid}.txt", CLAUDEX_SESSION.format(
            sid=sid, prose=prose, cwd=cwd, user=user, asst=asst))

    def backfill(self, **kwargs):
        return prose_index.import_prose_index(self.st, self.root, **kwargs)


class LosslessnessTest(_Index):
    """The contract that matters: a backfill cannot lose a word.

    Kept here rather than appended to tests/test_losslessness.py, which
    CLAUDE.md pins as a contract file that must pass unmodified. The guarantee
    is the same one and this file states it for the prose-index origin.
    """

    def test_every_word_of_prose_survives(self) -> None:
        sentinel = "the raven remembers the auth redirect decision nobody else kept"
        self.session("aaa", sentinel)
        self.backfill()
        self.assertIn(sentinel, self.st.session_text("aaa"))

    def test_the_body_is_stored_verbatim_including_the_old_markers(self) -> None:
        # Normalising `[USER <ts>]` to Muninn's `[USER]` would be a lossy rewrite
        # of the only surviving copy of that text, to make it cosmetically match
        # text that is not at risk.
        self.session("aaa", "hello")
        self.backfill()
        text = self.st.session_text("aaa")
        self.assertIn("[USER 2026-05-05T04:20:46.974Z]", text)
        self.assertNotIn("# session:", text, "a header leaked into the prose")

    def test_the_prose_is_searchable_afterwards(self) -> None:
        # Stored but unchunked would be a silent half-import: the words are in
        # the archive and nothing can find them.
        self.session("aaa", "unmistakable phrase about hydraulic couplings")
        self.backfill()
        hits = self.st.search("hydraulic couplings")
        self.assertEqual([h["session_id"] for h in hits], ["aaa"])

    def test_a_second_pass_changes_nothing(self) -> None:
        self.session("aaa", "idempotence is the whole point")
        self.backfill()
        before = self.st.session_text("aaa")
        second = self.backfill()
        self.assertIs(second.outcome, Outcome.DUPLICATE)
        self.assertEqual(self.st.session_text("aaa"), before)
        self.assertEqual(self.st.count_sessions(), 1)

    def test_a_vanished_index_never_deletes_what_it_contributed(self) -> None:
        # The predecessor's directory going away is the expected end state, not
        # a signal to clean up. This importer never reconciles at all.
        self.session("aaa", "still here")
        self.backfill()
        shutil.rmtree(self.root / "index")
        (self.root / "index").mkdir()
        self.backfill()
        self.assertEqual(self.st.session_text("aaa"), self.st.session_text("aaa"))
        self.assertIn("still here", self.st.session_text("aaa"))


class PrecedenceTest(_Index):
    """A prose file must never overwrite a richer raw-derived session."""

    def _raw(self, sid: str, text: str) -> None:
        self.st.upsert_session({
            "session_id": sid, "source": "claude", "provenance": "human",
            "text": text, "words": len(text.split()), "user_turns": 4,
            "assistant_turns": 4, "tool_uses": 12, "tool_results": 12,
            "origin": "raw", "source_present": 1, "model": "claude-opus-5",
            "source_path": "/real/path.jsonl", "ended_at": "2026-05-05T04:21:12.564Z",
        })
        self.st.commit()

    def test_a_raw_session_is_left_completely_alone(self) -> None:
        self._raw("aaa", "the full raw transcript with tool calls")
        self.session("aaa", "the thinner prose-index copy")
        self.backfill()
        row = self.st.get_session("aaa")
        self.assertEqual(row["origin"], "raw")
        self.assertEqual(row["text"], "the full raw transcript with tool calls")
        self.assertEqual(row["tool_uses"], 12)
        self.assertEqual(row["source_present"], 1)

    def test_the_decision_is_recorded_as_an_enumerated_skip(self) -> None:
        # Silently passing over it would make "why is this session not from the
        # backfill" an inference. The reason exists for exactly this and was
        # reserved before any importer needed it.
        self._raw("aaa", "raw")
        self.session("aaa", "prose")
        receipt = self.backfill()
        self.assertEqual([s.reason for s in receipt.skips],
                         [SkipReason.SUPERSEDED_BY_RICHER_ORIGIN])
        self.assertEqual(receipt.delta.skipped, 1)

    def test_the_skip_is_queryable_from_the_ledger(self) -> None:
        self._raw("aaa", "raw")
        self.session("aaa", "prose")
        receipt = self.backfill()
        rows = self.st.conn.execute(
            "SELECT item_id, disposition, reason FROM import_items WHERE ledger_id = ?",
            (receipt.ledger_id,)).fetchall()
        self.assertIn(("aaa", "skipped", "superseded-by-richer-origin"),
                      [(r["item_id"], r["disposition"], r["reason"]) for r in rows])

    def test_a_later_raw_ingest_overwrites_the_backfill(self) -> None:
        # Precedence runs one way only. The backfill is the floor, not a lock.
        self.session("aaa", "the thin copy")
        self.backfill()
        self.assertEqual(self.st.get_session("aaa")["origin"], "prose-index")
        self._raw("aaa", "the full raw transcript")
        row = self.st.get_session("aaa")
        self.assertEqual(row["origin"], "raw")
        self.assertEqual(row["source_present"], 1)


class HonestyTest(_Index):
    """The archive must not overstate what survives."""

    def test_backfilled_sessions_are_recorded_as_having_no_raw_transcript(self) -> None:
        # These are the irreplaceable ones by construction: a prose entry exists
        # because a predecessor archived a transcript the vendor has since swept.
        self.session("aaa", "hello")
        self.backfill()
        self.assertEqual(self.st.get_session("aaa")["source_present"], 0)

    def test_they_name_no_source_path(self) -> None:
        # Which also keeps them out of the local sweep's reconciler — it only
        # considers rows naming a path — so nothing flaps them back and forth.
        self.session("aaa", "hello")
        self.backfill()
        self.assertIsNone(self.st.get_session("aaa")["source_path"])

    def test_the_origin_is_recorded(self) -> None:
        self.session("aaa", "hello")
        self.backfill()
        self.assertEqual(self.st.get_session("aaa")["origin"], "prose-index")

    def test_the_ledger_names_the_source_kind(self) -> None:
        self.session("aaa", "hello")
        receipt = self.backfill()
        self.assertEqual(receipt.source.kind, "prose-index")
        row = self.st.conn.execute("SELECT source_kind, windowed FROM import_ledger "
                                   "WHERE ledger_id = ?", (receipt.ledger_id,)).fetchone()
        self.assertEqual(row["source_kind"], "prose-index")
        # Not windowed: a prose index is a complete archive of what its tool saw.
        self.assertEqual(row["windowed"], 0)


class FormatTest(_Index):
    """The three real shapes, parsed as the predecessors actually wrote them."""

    def test_claudex_headers(self) -> None:
        self.session("aaa", "hello", cwd="/Users/x/Projects/muninn", user=3, asst=7)
        self.backfill()
        row = self.st.get_session("aaa")
        self.assertEqual(row["cwd"], "/Users/x/Projects/muninn")
        self.assertEqual(row["branch"], "HEAD")
        self.assertEqual(row["user_turns"], 3)
        self.assertEqual(row["assistant_turns"], 7)
        self.assertEqual(row["started_at"], "2026-05-05T04:20:46.974Z")

    def test_a_subagent_keeps_its_class_and_its_parent(self) -> None:
        # Provenance stays structural. Mistrusting subagent transcripts once
        # dropped 251 files and 725,706 words.
        self.write("agent-a02c488097593422f.txt",
                   CLAUDEX_SUBAGENT.format(prose="subagent work product"))
        self.backfill()
        row = self.st.get_session("agent-a02c488097593422f")
        self.assertEqual(row["provenance"], "subagent")
        self.assertEqual(row["parent_id"], "3b3cb1b5-696a-496e-8ea0-7ccaa0430921")

    def test_the_cloud_sub_index_is_found_and_attributed_to_claude_cloud(self) -> None:
        # Easy to miss: a second directory holding a different session class.
        # Missing it would silently drop every claude.ai conversation the
        # predecessor archived — the older and less replaceable half.
        self.write("bbb.txt", CLAUDEX_CLOUD.format(sid="bbb", prose="cloud conversation"),
                   sub="cloud/index")
        self.backfill()
        row = self.st.get_session("bbb")
        self.assertEqual(row["source"], "claude-cloud")
        self.assertEqual(row["title"], "Eastern to Bangalore time conversion")

    def test_codexdex_headers_including_the_source_override(self) -> None:
        self.write("ccc.txt", CODEXDEX_SESSION.format(sid="ccc", prose="codex work",
                                                      title="orlog M5"))
        self.backfill(default_source="claude")  # the file's own header must win
        row = self.st.get_session("ccc")
        self.assertEqual(row["source"], "codex")
        self.assertEqual(row["model"], "gpt-5")
        self.assertEqual(row["title"], "orlog M5")

    def test_a_tool_invoked_cwd_is_still_classified_structurally(self) -> None:
        # Deciding provenance from prose length here would reintroduce exactly
        # the length-based classification that skewed measurements by ~40x.
        self.session("ddd", "one line", cwd="/Users/x/.local/state/huginn/cache")
        self.backfill()
        self.assertEqual(self.st.get_session("ddd")["provenance"], "tool-invoked")

    def test_a_hash_inside_the_prose_is_not_read_as_a_header(self) -> None:
        # Markdown headings are extremely common in transcripts. The header
        # block ends at the first non-`#` line, which is what makes this safe.
        self.session("eee", "intro\n\n# Design notes\n\nbody text")
        self.backfill()
        text = self.st.session_text("eee")
        self.assertIn("# Design notes", text)
        self.assertIn("body text", text)


class SkipTest(_Index):
    """Enumerate, do not count. Every dropped file gets an id and a reason."""

    def test_an_empty_body_is_a_no_content_skip(self) -> None:
        self.write("empty.txt", "# session: empty\n# kind: session\n# turns: user=0\n\n")
        receipt = self.backfill()
        self.assertEqual([(s.item_id, s.reason) for s in receipt.skips],
                         [("empty", SkipReason.NO_CONTENT)])

    def test_a_file_with_no_headers_is_an_unknown_schema_skip(self) -> None:
        self.write("junk.txt", "this is not a prose index file at all")
        receipt = self.backfill()
        self.assertEqual([s.reason for s in receipt.skips], [SkipReason.UNKNOWN_SCHEMA])

    def test_the_arithmetic_closes(self) -> None:
        # added + updated + unchanged + skipped == item_count. A silent gap here
        # is the exact hole claudex's own predecessor had.
        self.session("aaa", "one")
        self.session("bbb", "two")
        self.write("junk.txt", "no headers")
        receipt = self.backfill()
        d = receipt.delta
        self.assertEqual(d.added + d.updated + d.unchanged + d.skipped,
                         receipt.source.item_count)
        self.assertEqual(receipt.source.item_count, 3)

    def test_a_partial_import_says_partial(self) -> None:
        self.session("aaa", "one")
        self.write("junk.txt", "no headers")
        self.assertIs(self.backfill().outcome, Outcome.PARTIAL)

    def test_an_index_of_only_junk_is_rejected_not_reported_as_imported(self) -> None:
        # "Imported, 0 added" reads as "nothing new". This is "nothing worked".
        self.write("junk.txt", "no headers")
        self.assertIs(self.backfill().outcome, Outcome.REJECTED)


class DiscoveryTest(unittest.TestCase):
    """Which directories count as a predecessor index."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-prose-home-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_an_empty_predecessor_directory_is_not_offered(self) -> None:
        # A real state on this corpus: codexdex was never run on the development
        # machine before Muninn's design work. Reporting it as a source holding
        # nothing would be indistinguishable from a parse failure.
        (self.tmp / ".codexdex" / "index").mkdir(parents=True)
        self.assertEqual(prose_index.find_prose_indexes(self.tmp), [])

    def test_both_predecessors_are_found_with_their_own_sources(self) -> None:
        for name in (".claudex", ".codexdex"):
            (self.tmp / name / "index").mkdir(parents=True)
            (self.tmp / name / "index" / "a.txt").write_text("# session: a\n\nbody\n")
        found = prose_index.find_prose_indexes(self.tmp)
        self.assertEqual([(c.path.name, c.default_source) for c in found],
                         [(".claudex", "claude"), (".codexdex", "codex")])

    def test_discovery_is_ordered(self) -> None:
        root = self.tmp / ".claudex"
        (root / "index").mkdir(parents=True)
        for name in ("c.txt", "a.txt", "b.txt"):
            (root / "index" / name).write_text("# session: x\n\nbody\n")
        self.assertEqual([p.name for p in prose_index.discover(root)],
                         ["a.txt", "b.txt", "c.txt"])

    def test_a_missing_root_is_not_an_error(self) -> None:
        self.assertEqual(prose_index.discover(self.tmp / "nope"), [])
        self.assertEqual(prose_index.find_prose_indexes(self.tmp), [])


class HeaderParsingTest(unittest.TestCase):
    def test_headers_end_at_the_first_non_comment_line(self) -> None:
        headers, body = prose_index.parse_headers(
            "# session: a\n# kind: session\n\n# not a header\nbody\n")
        self.assertEqual(headers, {"session": "a", "kind": "session"})
        self.assertEqual(body, "# not a header\nbody")

    def test_a_valueless_line_is_ignored_rather_than_fatal(self) -> None:
        headers, _ = prose_index.parse_headers("# session: a\n# nonsense\n\nbody\n")
        self.assertEqual(headers, {"session": "a"})

    def test_unrecognised_keys_are_kept(self) -> None:
        # A format nobody maintains. Discarding the evidence of other keys would
        # make the next surprise harder to diagnose.
        headers, _ = prose_index.parse_headers("# session: a\n# whatever: 7\n\nbody\n")
        self.assertEqual(headers["whatever"], "7")

    def test_turn_counts_survive_a_malformed_value(self) -> None:
        self.assertEqual(prose_index._int_after("user=x assistant=4", "assistant"), 4)
        self.assertEqual(prose_index._int_after("user=x assistant=4", "user"), 0)
        self.assertEqual(prose_index._int_after("", "user"), 0)


if __name__ == "__main__":
    unittest.main()
