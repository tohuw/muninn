"""Provider subprocesses must speak UTF-8 on their pipes.

Transcripts are full of characters the Windows locale encoding (cp1252) cannot
represent — arrows, box drawing, em dashes, emoji. With ``text=True`` and no
explicit encoding, writing the prompt raised UnicodeEncodeError on subprocess's
writer thread; the parent then blocked until its timeout, the child saw a
truncated prompt, and enrichment recorded ``invalid-json`` while exiting 0.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from muninn import providers

TRICKY = "arrow → box └─ dash — emoji \U0001f600"


class ProviderPipeEncodingTest(unittest.TestCase):
    def test_pipes_are_utf8_not_the_locale_encoding(self) -> None:
        self.assertEqual(providers._PIPE_TEXT["encoding"], "utf-8")

    def test_a_transcript_survives_the_round_trip(self) -> None:
        """The regression, end to end through a real subprocess."""
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
            input=TRICKY, capture_output=True, timeout=60, **providers._PIPE_TEXT,
        )
        self.assertEqual(proc.stdout, TRICKY)

    def test_undecodable_output_degrades_instead_of_raising(self) -> None:
        """errors='replace': one mangled glyph beats losing the session."""
        proc = subprocess.run(
            [sys.executable, "-c",
             r"import sys; sys.stdout.buffer.write(b'{\"ok\": \"\xff\xfe\"}')"],
            input="", capture_output=True, timeout=60, **providers._PIPE_TEXT,
        )
        self.assertIn("ok", proc.stdout)

    def test_both_provider_call_sites_use_it(self) -> None:
        """A new provider added without these kwargs reintroduces the bug."""
        import inspect

        for provider in (providers.ClaudeCLIProvider, providers.CodexCLIProvider):
            source = inspect.getsource(provider)
            self.assertIn("_PIPE_TEXT", source, f"{provider.__name__} bypasses the pipe encoding")
            self.assertNotIn("text=True", source, f"{provider.__name__} still uses the locale encoding")
            self.assertIn("_NO_WINDOW", source, f"{provider.__name__} can open a console window")


class ProviderWindowSuppressionTest(unittest.TestCase):
    """`muninn serve` runs under pythonw, with no console to lend a child.

    The provider CLIs are console-subsystem programs, so Windows gave each
    invocation a console of its own — one terminal window per model call, and
    automatic enrichment made dozens appear unbidden. A daemon must never put a
    window on the user's screen.

    Both branches are asserted on every OS rather than skipped off Windows: the
    flag is the entire point of the fix, and a test that only runs on the broken
    platform leaves it unguarded on the machines it is usually developed on.
    """

    def test_the_flag_is_set_on_windows(self) -> None:
        with patch.object(providers.os, "name", "nt"):
            flags = providers._no_window_kwargs()
        self.assertEqual(flags["creationflags"] & 0x08000000, 0x08000000,
                         "CREATE_NO_WINDOW missing — provider calls will open windows")

    def test_nothing_is_passed_off_windows(self) -> None:
        """creationflags is not a POSIX concept; passing it raises there."""
        with patch.object(providers.os, "name", "posix"):
            self.assertEqual(providers._no_window_kwargs(), {})

    def test_the_module_constant_matches_this_platform(self) -> None:
        self.assertEqual(providers._NO_WINDOW, providers._no_window_kwargs())


if __name__ == "__main__":
    unittest.main()
