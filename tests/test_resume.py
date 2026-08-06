"""`muninn resume` — and the refusals, which are the important half.

Muninn is an archive of record. Claude Code sweeps transcripts after
``cleanupPeriodDays``, so the *majority* of what this command can find, it
cannot resume — on the development corpus, every one of 3,730 backfilled
sessions. That is the normal end state, not a malfunction, and the command has
to be honest about it rather than emit `claude --resume <id>` and let the vendor
produce an error that explains nothing.

So most of this file is about the cases where there is no command to print, and
that every one of them still points at `muninn show`, because the prose is the
thing that survived.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muninn import cli, resume
from muninn.store import open_store


def _row(**over) -> dict:
    base = {"session_id": "a7efca23-0000-0000-0000-000000000000", "source": "claude",
            "provenance": "human", "cwd": "/Users/x/Projects/muninn", "origin": "raw",
            "source_present": 1, "parent_id": None}
    return {**base, **over}


class PlanTest(unittest.TestCase):
    """Pure decisions: no filesystem, no subprocess, no Claude install needed."""

    def test_a_live_claude_session_resumes(self) -> None:
        plan = resume.plan(_row())
        self.assertEqual(plan.command,
                         ["claude", "--resume", "a7efca23-0000-0000-0000-000000000000"])
        self.assertIsNone(plan.refusal)

    def test_codex_gets_codexs_own_verb(self) -> None:
        # `codex resume`, not `codex --resume`. Guessing one shape for both
        # would produce a command that parses as something else entirely.
        plan = resume.plan(_row(source="codex"))
        self.assertEqual(plan.command[:2], ["codex", "resume"])

    def test_the_command_runs_from_the_sessions_own_directory(self) -> None:
        # Resuming the right session in the wrong repo is worse than not
        # resuming: the tool comes up with a working tree that does not match
        # anything in the transcript.
        self.assertIn("cd /Users/x/Projects/muninn && ", resume.plan(_row()).shell())

    def test_a_directoryless_session_still_gets_a_command(self) -> None:
        # Codex rollouts may carry no cwd at all.
        plan = resume.plan(_row(source="codex", cwd=None))
        self.assertFalse(plan.shell().startswith("cd "))

    def test_a_path_with_spaces_is_quoted(self) -> None:
        plan = resume.plan(_row(cwd="/Users/x/Mobile Documents/Projects"))
        self.assertIn("'/Users/x/Mobile Documents/Projects'", plan.shell())

    # -- the refusals ------------------------------------------------------

    def test_a_swept_transcript_is_refused_not_attempted(self) -> None:
        # The common case, and the whole reason the archive exists.
        plan = resume.plan(_row(source_present=0))
        self.assertIsNone(plan.command)
        self.assertIn("only copy", plan.refusal)

    def test_a_backfilled_session_is_refused(self) -> None:
        # A prose-index session came from a predecessor's archive, so its
        # transcript was swept long ago by construction.
        plan = resume.plan(_row(origin="prose-index"))
        self.assertIsNone(plan.command)
        self.assertIn("prose-index", plan.refusal)

    def test_a_subagent_is_refused_and_names_its_parent(self) -> None:
        # A subagent has no resumable identity at all, which is why it is
        # answered before presence — that one is merely true today.
        plan = resume.plan(_row(provenance="subagent", parent_id="3b3cb1b5"))
        self.assertIsNone(plan.command)
        self.assertIn("3b3cb1b5", plan.refusal)

    def test_a_subagent_is_refused_even_when_its_transcript_survives(self) -> None:
        plan = resume.plan(_row(provenance="subagent", source_present=1))
        self.assertIsNone(plan.command)
        self.assertIn("subagent", plan.refusal)

    def test_cloud_conversations_are_refused_by_name(self) -> None:
        # They live in a web UI; there is no local invocation to print, and
        # guessing one would be inventing a claim.
        for source in ("claude-cloud", "chatgpt-cloud"):
            with self.subTest(source=source):
                plan = resume.plan(_row(source=source))
                self.assertIsNone(plan.command)
                self.assertIn(source, plan.refusal)

    def test_a_plan_never_carries_both_a_command_and_a_refusal(self) -> None:
        # A caveat alongside a command invites printing the command anyway.
        for rec in (_row(), _row(source_present=0), _row(provenance="subagent"),
                    _row(source="claude-cloud"), _row(origin="prose-index")):
            plan = resume.plan(rec)
            with self.subTest(refusal=plan.refusal):
                self.assertNotEqual(plan.command is None, plan.refusal is None)


class CliTest(unittest.TestCase):
    """Exit codes are the contract: an agent runs this."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-resume-"))
        self.db = self.tmp / "archive.db"
        self.st = open_store(self.db)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        with contextlib.suppress(Exception):
            self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, session_id: str, **over) -> None:
        rec = _row(session_id=session_id, **over)
        self.st.upsert_session({**rec, "text": "prose", "words": 1, "user_turns": 1,
                                "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
                                "started_at": "2026-06-14T00:00:00Z"})
        self.st.commit()

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--db", str(self.db), "resume", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_a_resumable_session_exits_zero_and_prints_the_command(self) -> None:
        self.add("a7efca23")
        code, out, _ = self.run_cli("a7efca23")
        self.assertEqual(code, 0)
        self.assertIn("claude --resume a7efca23", out)

    def test_a_prefix_is_enough(self) -> None:
        self.add("a7efca23-0000")
        self.assertEqual(self.run_cli("a7ef")[0], 0)

    def test_no_match_exits_one(self) -> None:
        self.assertEqual(self.run_cli("zzzz")[0], 1)

    def test_an_ambiguous_prefix_lists_the_candidates(self) -> None:
        self.add("aaa-1")
        self.add("aaa-2")
        code, _, err = self.run_cli("aaa")
        self.assertEqual(code, 1)
        self.assertIn("aaa-1", err)

    def test_not_resumable_exits_three_not_one(self) -> None:
        # Separate from "no match" on purpose: "I could not find it" and "I
        # found it and its transcript is gone" lead to different next moves.
        self.add("gone", source_present=0)
        code, _, err = self.run_cli("gone")
        self.assertEqual(code, 3)
        self.assertIn("not resumable", err)

    def test_a_refusal_points_at_the_prose_that_did_survive(self) -> None:
        # The whole point of having kept it.
        self.add("gone", source_present=0)
        self.assertIn("muninn show", self.run_cli("gone")[2])

    def test_a_refusal_emits_no_command_at_all(self) -> None:
        self.add("gone", source_present=0)
        _, out, _ = self.run_cli("gone")
        self.assertNotIn("--resume", out)

    def test_exec_is_off_by_default(self) -> None:
        self.add("a7efca23")
        with patch("subprocess.call") as called:
            self.run_cli("a7efca23")
        called.assert_not_called()

    def test_exec_runs_the_command_in_the_sessions_directory(self) -> None:
        self.add("a7efca23", cwd=str(self.tmp))
        with patch("subprocess.call", return_value=0) as called:
            code, _, _ = self.run_cli("a7efca23", "--exec")
        self.assertEqual(code, 0)
        self.assertEqual(called.call_args.kwargs["cwd"], str(self.tmp))

    def test_exec_relays_the_tools_exit_code(self) -> None:
        self.add("a7efca23", cwd=str(self.tmp))
        with patch("subprocess.call", return_value=42):
            self.assertEqual(self.run_cli("a7efca23", "--exec")[0], 42)

    def test_exec_refuses_when_the_directory_is_gone(self) -> None:
        # Letting the tool start in whatever directory this happens to be would
        # resume the right session in the wrong repo.
        self.add("a7efca23", cwd="/nonexistent/path/for/this/test")
        with patch("subprocess.call") as called:
            code, _, err = self.run_cli("a7efca23", "--exec")
        self.assertEqual(code, 3)
        called.assert_not_called()
        self.assertIn("no longer exists", err)

    def test_a_missing_tool_is_reported_by_class_not_by_message(self) -> None:
        # No exception messages in surfaced output: they can embed paths or
        # credentials, per the rule the ledger already follows.
        self.add("a7efca23", cwd=str(self.tmp))
        with patch("subprocess.call", side_effect=FileNotFoundError("no such file: /x")):
            code, _, err = self.run_cli("a7efca23", "--exec")
        self.assertEqual(code, 3)
        self.assertIn("FileNotFoundError", err)
        self.assertNotIn("/x", err)

    def test_json_is_parseable_for_both_answers(self) -> None:
        import json

        self.add("live")
        self.add("gone", source_present=0)
        code, out, _ = self.run_cli("live", "--json")
        self.assertEqual((code, json.loads(out)["resumable"]), (0, True))
        code, out, _ = self.run_cli("gone", "--json")
        payload = json.loads(out)
        self.assertEqual((code, payload["resumable"]), (3, False))
        self.assertIsNotNone(payload["refusal"])


if __name__ == "__main__":
    unittest.main()
