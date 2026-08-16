"""pid_alive decides whether the import lock has a live holder.

It is not a cosmetic check. ``Store.acquire_import_lock`` takes a lock over
whenever the recorded holder reads as dead, so a false "dead" lets a second
ingest loop run against an archive that is the only remaining copy of its data.
"""
from __future__ import annotations

import os
import unittest

from muninn import store


class PidLivenessTest(unittest.TestCase):
    def test_this_process_is_alive(self) -> None:
        """The regression. On Windows os.kill(pid, 0) raises for a *live* pid.

        CPython emulates os.kill there, and its errors do not carry the POSIX
        meanings: a live pid raised OSError/WinError 87 and a missing one
        WinError 11, so treating any OSError as "gone" reported every running
        process as dead.
        """
        self.assertTrue(store.pid_alive(os.getpid()))

    def test_a_pid_that_cannot_exist_is_dead(self) -> None:
        self.assertFalse(store.pid_alive(999_999_998))

    def test_a_reaped_child_is_dead(self) -> None:
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait()
        self.assertFalse(store.pid_alive(proc.pid))

    def test_unusable_pids_are_dead_rather_than_an_exception(self) -> None:
        # pid 0 is "the current process group" to POSIX kill and must never be
        # probed; a non-int reaches here from a hand-edited daemon.json.
        for value in (None, 0, -1, "1"):
            self.assertFalse(store.pid_alive(value))  # type: ignore[arg-type]

    @unittest.skipUnless(os.name == "nt", "Windows-only ownership case")
    def test_a_process_owned_by_another_user_counts_as_alive(self) -> None:
        # pid 4 is the System process: it exists, and OpenProcess is denied.
        # "Exists" is the answer a lock-holder check needs.
        self.assertTrue(store.pid_alive(4))


if __name__ == "__main__":
    unittest.main()
