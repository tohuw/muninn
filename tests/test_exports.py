"""Export importer contract: claude.ai and ChatGPT vendor data exports.

Each test below maps to one acceptance criterion in
docs/specs/002-export-importers.md ("Acceptance criteria"). See
.valholl/articles/deterministic-imports.md for the incident this whole
module exists to prevent: an agent reading "0 written, 61 cached" concluded
"the export contained nothing new" when the truth was "already imported by
another actor minutes ago." Criterion 2 reproduces that incident directly
against the export importer and asserts it cannot happen.

No real ``~/Downloads`` is touched anywhere in this file; every fixture is
built fresh under a tempdir per test.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from muninn import digest, exports, store
from muninn.receipt import Outcome, SkipReason

# See tests/test_queue.py for why this exists: subprocess and thread-fan-out
# tests wedge the Windows CI runner rather than failing, taking down the whole
# job. Skipped there; equivalent in-process coverage runs everywhere.
requires_subprocess = unittest.skipIf(
    sys.platform == "win32",
    "subprocess/thread fan-out wedges the Windows CI runner; the same "
    "properties are covered in-process on all platforms",
)



# -- fixture builders --------------------------------------------------------


def claude_conversation(uuid: str, *, name: str = "Test conversation",
                        created_at: str = "2026-06-01T00:00:00Z",
                        updated_at: str = "2026-06-02T00:00:00Z",
                        messages: list[dict] | None = None) -> dict:
    if messages is None:
        messages = [
            {"sender": "human", "created_at": created_at, "text": "hello there"},
            {"sender": "assistant", "created_at": updated_at, "text": "hello back"},
        ]
    return {"uuid": uuid, "name": name, "created_at": created_at,
            "updated_at": updated_at, "chat_messages": messages}


def chatgpt_conversation(conv_id: str, *, title: str = "Test conversation",
                         create_time: float = 1_700_000_000.0,
                         update_time: float = 1_700_000_100.0,
                         mapping: dict | None = None) -> dict:
    if mapping is None:
        mapping = {
            "n1": {"message": {"author": {"role": "user"},
                               "content": {"parts": ["hello there"]},
                               "create_time": create_time}},
            "n2": {"message": {"author": {"role": "assistant"},
                               "content": {"parts": ["hello back"]},
                               "create_time": update_time}},
        }
    return {"id": conv_id, "title": title, "create_time": create_time,
            "update_time": update_time, "mapping": mapping}


def write_claude_export_dir(root: Path, conversations: list[dict]) -> Path:
    export_dir = root / "data-2026-07-30-batch-0000"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "conversations.json").write_text(json.dumps(conversations))
    return export_dir


def write_chatgpt_export_file(root: Path, conversations: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "conversations.json"
    path.write_text(json.dumps(conversations))
    return path


class ExportsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-exports-"))
        self.db = self.tmp / "muninn.db"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


# -- criterion 1: detection ---------------------------------------------------


class DetectionTest(ExportsTestCase):
    def test_claude_shaped_detects_as_claude_export(self) -> None:
        payload = [claude_conversation("c1")]
        self.assertEqual(exports.detect_kind(payload), "claude-export")

    def test_chatgpt_shaped_detects_as_chatgpt_export(self) -> None:
        payload = [chatgpt_conversation("g1")]
        self.assertEqual(exports.detect_kind(payload), "chatgpt-export")

    def test_garbage_detects_as_none(self) -> None:
        self.assertIsNone(exports.detect_kind([{"foo": "bar"}]))
        self.assertIsNone(exports.detect_kind([]))
        self.assertIsNone(exports.detect_kind({"not": "a list"}))

    def test_claude_shaped_is_never_detected_as_chatgpt(self) -> None:
        payload = [claude_conversation("c1")]
        self.assertNotEqual(exports.detect_kind(payload), "chatgpt-export")


# -- criterion 2: the incident, reproduced and fixed -------------------------


class IncidentReproducedTest(ExportsTestCase):
    def test_second_import_by_different_actor_is_duplicate_with_attribution(self) -> None:
        conversations = [
            claude_conversation("c1"), claude_conversation("c2"), claude_conversation("c3"),
        ]
        export_dir = write_claude_export_dir(self.tmp, conversations)

        st = store.open_store(self.db)
        receipt_a = exports.import_export(st, export_dir, actor="actor-a")
        self.assertEqual(receipt_a.outcome, Outcome.IMPORTED)
        self.assertEqual(receipt_a.source.item_count, 3)

        receipt_b = exports.import_export(st, export_dir, actor="actor-b")
        st.close()

        self.assertEqual(receipt_b.outcome, Outcome.DUPLICATE)
        self.assertEqual(receipt_b.duplicate_of, receipt_a.ledger_id)
        self.assertEqual(receipt_b.source.item_count, 3)
        self.assertIsNotNone(receipt_b.attribution)
        self.assertEqual(receipt_b.attribution.actor, "actor-a")

        # No field on this receipt can be read as "the export contained
        # nothing": outcome says duplicate, item_count is positive, and the
        # attribution names a real prior import.
        as_dict = receipt_b.to_dict()
        self.assertEqual(as_dict["outcome"], "duplicate")
        self.assertGreater(as_dict["source"]["item_count"], 0)
        self.assertEqual(as_dict["attribution"]["actor"], "actor-a")
        rendered = json.dumps(as_dict)
        self.assertNotIn("nothing new", rendered)


# -- criterion 3: windowed safety --------------------------------------------


class WindowedSafetyTest(ExportsTestCase):
    def test_omitted_session_stays_present_after_a_windowed_reimport(self) -> None:
        first_dir = write_claude_export_dir(self.tmp / "first", [claude_conversation("keep-me")])
        st = store.open_store(self.db)
        r1 = exports.import_export(st, first_dir, actor="cli")
        self.assertEqual(r1.outcome, Outcome.IMPORTED)
        self.assertTrue(r1.source.windowed)

        before = st.get_session("keep-me")
        self.assertEqual(before["source_present"], 1)

        # A second, later export that simply does not mention "keep-me" --
        # the normal shape of a rolling ~30-day window.
        second_dir = write_claude_export_dir(self.tmp / "second", [claude_conversation("other")])
        r2 = exports.import_export(st, second_dir, actor="cli")

        self.assertTrue(r2.source.windowed)
        after = st.get_session("keep-me")
        st.close()
        self.assertEqual(after["source_present"], 1,
                         "absence from a windowed export must never mark a session missing")


# -- criterion 4: conservation ------------------------------------------------


class ConservationTest(ExportsTestCase):
    def test_one_good_one_idless_one_empty_one_multimodal_conserves(self) -> None:
        good = claude_conversation("good-1")
        idless = {"name": "no id", "created_at": "2026-06-01T00:00:00Z",
                  "updated_at": "2026-06-02T00:00:00Z", "chat_messages": []}
        # No "uuid" key at all.
        empty = claude_conversation("empty-1", messages=[])
        multimodal = claude_conversation(
            "multimodal-1",
            messages=[{"sender": "human", "created_at": "2026-06-01T00:00:00Z",
                      "content": [{"type": "image", "source": "..."}]}],
        )
        payload = [good, idless, empty, multimodal]
        export_dir = write_claude_export_dir(self.tmp, payload)

        st = store.open_store(self.db)
        receipt = exports.import_export(st, export_dir, actor="cli")
        st.close()

        self.assertEqual(receipt.source.item_count, 4)
        self.assertEqual(receipt.delta.added, 1)
        self.assertEqual(receipt.delta.skipped, 3)
        total = (receipt.delta.added + receipt.delta.updated
                + receipt.delta.unchanged + receipt.delta.skipped)
        self.assertEqual(total, receipt.source.item_count)

        reasons = {s.item_id: s.reason for s in receipt.skips}
        self.assertEqual(reasons["no id"] if "no id" in reasons else None, None)  # sanity
        by_reason = {s.reason for s in receipt.skips}
        self.assertIn(SkipReason.MISSING_ITEM_ID, by_reason)
        self.assertIn(SkipReason.NO_CONTENT, by_reason)
        self.assertIn(SkipReason.UNSUPPORTED_CONTENT_TYPE, by_reason)


# -- criterion 5: no id coercion ----------------------------------------------


class NoIdCoercionTest(ExportsTestCase):
    def test_two_idless_conversations_yield_two_skips_zero_sessions(self) -> None:
        idless_1 = {"name": "a", "created_at": "2026-06-01T00:00:00Z",
                    "updated_at": "2026-06-02T00:00:00Z", "chat_messages": []}
        idless_2 = {"name": "b", "created_at": "2026-06-01T00:00:00Z",
                    "updated_at": "2026-06-03T00:00:00Z", "chat_messages": []}
        export_dir = write_claude_export_dir(self.tmp, [idless_1, idless_2])

        st = store.open_store(self.db)
        receipt = exports.import_export(st, export_dir, actor="cli")
        self.assertEqual(st.count_sessions(), 0)
        st.close()

        self.assertEqual(receipt.delta.skipped, 2)
        self.assertEqual(receipt.delta.added, 0)
        for skip in receipt.skips:
            self.assertEqual(skip.reason, SkipReason.MISSING_ITEM_ID)
        # Each id-less item keeps a distinct identity in the receipt -- never
        # coalesced onto a shared placeholder id.
        self.assertEqual(len({s.item_id for s in receipt.skips}), 2)


# -- criterion 6: digest ignores byte layout ---------------------------------


class DigestIgnoresByteLayoutTest(ExportsTestCase):
    def test_reserialized_payload_same_items_digest_different_file_digest(self) -> None:
        payload = [claude_conversation("c1"), claude_conversation("c2")]

        path1 = self.tmp / "one.json"
        path1.write_text(json.dumps(payload, sort_keys=True, indent=None))
        path2 = self.tmp / "two.json"
        path2.write_text(json.dumps(payload, sort_keys=False, indent=4))

        payload1, file_digest1 = exports.load_payload(path1)
        payload2, file_digest2 = exports.load_payload(path2)

        _sessions1, _skips1, pairs1 = exports.parse_claude_export(payload1)
        _sessions2, _skips2, pairs2 = exports.parse_claude_export(payload2)

        self.assertEqual(digest.digest_items("claude-export", pairs1),
                         digest.digest_items("claude-export", pairs2))
        self.assertNotEqual(file_digest1, file_digest2,
                            "different byte layout must change the file digest")


# -- criterion 7: digest includes kind ---------------------------------------


class DigestIncludesKindTest(ExportsTestCase):
    def test_identical_pairs_different_kind_different_digest(self) -> None:
        pairs = [("shared-id", "2026-06-01T00:00:00Z")]
        d_claude = digest.digest_items("claude-export", pairs)
        d_chatgpt = digest.digest_items("chatgpt-export", pairs)
        self.assertNotEqual(d_claude, d_chatgpt)


# -- criterion 8: epoch floats not normalized pre-hash -----------------------


class EpochFloatNotNormalizedTest(ExportsTestCase):
    @requires_subprocess
    def test_float_update_time_digests_identically_across_processes(self) -> None:
        payload = [chatgpt_conversation("g1", update_time=1785400000.5)]
        _sessions, _skips, pairs = exports.parse_chatgpt_export(payload)
        self.assertEqual(pairs, [("g1", "1785400000.5")],
                         "raw float must be str()'d verbatim, never converted")
        in_process = digest.digest_items("chatgpt-export", pairs)

        script = (
            "from muninn import digest; "
            "print(digest.digest_items('chatgpt-export', [('g1', '1785400000.5')]))"
        )
        out = subprocess.run([sys.executable, "-c", script], capture_output=True,
                             text=True, check=True, cwd=Path(__file__).resolve().parent.parent)
        other_process = out.stdout.strip()
        self.assertEqual(in_process, other_process)


# -- criterion 9: zip handling ------------------------------------------------


class ZipHandlingTest(ExportsTestCase):
    def test_shallowest_conversations_json_is_chosen_over_a_decoy(self) -> None:
        shallow_payload = [chatgpt_conversation("shallow-1")]
        decoy_payload = [chatgpt_conversation("decoy-1")]

        zip_path = self.tmp / "chatgpt-export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("conversations.json", json.dumps(shallow_payload))
            zf.writestr("export/backup/old/conversations.json", json.dumps(decoy_payload))

        payload, file_digest = exports.load_payload(zip_path)
        self.assertEqual(payload, shallow_payload)
        self.assertIsNotNone(file_digest)

    def test_zip_without_conversations_json_is_rejected_not_a_crash(self) -> None:
        zip_path = self.tmp / "chatgpt-export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("other.json", json.dumps([1, 2, 3]))

        st = store.open_store(self.db)
        receipt = exports.import_export(st, zip_path, actor="cli")
        st.close()

        self.assertEqual(receipt.outcome, Outcome.REJECTED)
        self.assertIsNotNone(receipt.error)


# -- criterion 10: graph flattening -------------------------------------------


class GraphFlatteningTest(ExportsTestCase):
    def test_out_of_order_nodes_flatten_to_timestamp_order(self) -> None:
        # Inserted into the mapping dict in reverse-timestamp order -- the
        # parser must sort by message.create_time, never trust dict order or
        # walk parent/child pointers.
        mapping = {
            "late": {"message": {"author": {"role": "assistant"},
                                 "content": {"parts": ["third"]},
                                 "create_time": 300.0}},
            "early": {"message": {"author": {"role": "user"},
                                  "content": {"parts": ["first"]},
                                  "create_time": 100.0}},
            "mid": {"message": {"author": {"role": "user"},
                                "content": {"parts": ["second"]},
                                "create_time": 200.0}},
        }
        payload = [chatgpt_conversation("g1", mapping=mapping)]
        sessions, skips, _pairs = exports.parse_chatgpt_export(payload)

        self.assertEqual(skips, [])
        self.assertEqual(len(sessions), 1)
        text = sessions[0].text
        self.assertLess(text.index("first"), text.index("second"))
        self.assertLess(text.index("second"), text.index("third"))


# -- criterion 11: attachment truncation --------------------------------------


class AttachmentTruncationTest(ExportsTestCase):
    def test_oversized_attachment_is_truncated_and_not_skipped(self) -> None:
        huge = "x" * 50_000
        conv = claude_conversation(
            "att-1",
            messages=[{"sender": "human", "created_at": "2026-06-01T00:00:00Z",
                      "attachments": [{"file_name": "notes.txt", "extracted_content": huge}]}],
        )
        payload = [conv]
        sessions, skips, _pairs = exports.parse_claude_export(payload)

        self.assertEqual(skips, [])
        self.assertEqual(len(sessions), 1)
        self.assertLessEqual(len(sessions[0].text), exports.ATTACHMENT_LIMIT + 200)
        self.assertIn("x" * 100, sessions[0].text)


# -- criterion 12: skip reasons are distinguishable ---------------------------


class SkipReasonsDistinguishableTest(ExportsTestCase):
    def test_voice_only_conversation_is_unsupported_content_type_not_no_content(self) -> None:
        mapping = {
            "n1": {"message": {"author": {"role": "user"},
                               "content": {"parts": [{"content_type": "audio_transcription",
                                                      "asset_pointer": "file-abc"}]},
                               "create_time": 100.0}},
        }
        payload = [chatgpt_conversation("voice-1", mapping=mapping)]
        sessions, skips, _pairs = exports.parse_chatgpt_export(payload)

        self.assertEqual(sessions, [])
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0].reason, SkipReason.UNSUPPORTED_CONTENT_TYPE)
        self.assertNotEqual(skips[0].reason, SkipReason.NO_CONTENT)


# -- additional coverage: discovery, chatgpt e2e, unknown format -------------


class DiscoveryTest(ExportsTestCase):
    def test_find_exports_prefers_newest_and_recognizes_all_three_shapes(self) -> None:
        import os
        import time

        write_claude_export_dir(self.tmp, [claude_conversation("c1")])
        time.sleep(0.01)
        write_chatgpt_export_file(self.tmp / "bare", [chatgpt_conversation("g1")])
        time.sleep(0.01)
        zip_path = self.tmp / "my-chatgpt-export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("conversations.json", json.dumps([chatgpt_conversation("g2")]))
        newest = zip_path.stat().st_mtime + 10
        os.utime(zip_path, (newest, newest))

        candidates = exports.find_exports(self.tmp)
        paths = [c.path for c in candidates]
        self.assertIn(self.tmp / "bare" / "conversations.json", paths)
        self.assertIn(zip_path, paths)
        self.assertEqual(candidates[0].path, zip_path, "newest by mtime must sort first")


class ChatgptEndToEndTest(ExportsTestCase):
    def test_chatgpt_export_imports_end_to_end(self) -> None:
        payload = [chatgpt_conversation("g1"), chatgpt_conversation("g2")]
        export_path = write_chatgpt_export_file(self.tmp, payload)

        st = store.open_store(self.db)
        receipt = exports.import_export(st, export_path, actor="cli")
        self.assertEqual(receipt.outcome, Outcome.IMPORTED)
        self.assertEqual(receipt.source.kind, "chatgpt-export")
        self.assertEqual(receipt.delta.added, 2)
        self.assertTrue(receipt.source.windowed)
        rec = st.get_session("g1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["source"], "chatgpt-cloud")
        st.close()


class UnknownFormatTest(ExportsTestCase):
    def test_garbage_json_is_rejected_not_a_crash(self) -> None:
        path = self.tmp / "conversations.json"
        path.write_text(json.dumps([{"foo": "bar"}]))

        st = store.open_store(self.db)
        receipt = exports.import_export(st, path, actor="cli")
        st.close()

        self.assertEqual(receipt.outcome, Outcome.REJECTED)

    def test_unparseable_json_is_rejected_not_a_crash(self) -> None:
        path = self.tmp / "conversations.json"
        path.write_text("{not valid json")

        st = store.open_store(self.db)
        receipt = exports.import_export(st, path, actor="cli")
        st.close()

        self.assertEqual(receipt.outcome, Outcome.REJECTED)
        self.assertIsNotNone(receipt.error)


class VerifySafeToDeleteTest(ExportsTestCase):
    def test_refuses_before_import_and_confirms_after(self) -> None:
        export_dir = write_claude_export_dir(self.tmp, [claude_conversation("c1")])

        st = store.open_store(self.db)
        safe_before, reason_before = exports.verify_safe_to_delete(st, export_dir)
        self.assertFalse(safe_before)
        self.assertTrue(reason_before)

        exports.import_export(st, export_dir, actor="cli")
        safe_after, reason_after = exports.verify_safe_to_delete(st, export_dir)
        st.close()

        self.assertTrue(safe_after)
        self.assertTrue(reason_after)


if __name__ == "__main__":
    unittest.main()
