"""``muninn-hook``: the SessionEnd hook entry point.

The single fact this module exists to respect (see
.valholl/articles/session-lifecycle-facts.md, "Design consequences for the
indexer", point 1): ``SessionEnd`` shares a **1.5-second budget across every
SessionEnd hook**, does not support ``async: true``, and cannot block session
exit. A hook that opens SQLite, imports a parser, or does anything more than
append a small file risks blowing that shared budget and delaying the user's
shell — for every hook registered, not just this one.

So this module imports only ``json``, ``sys``, ``time``, ``pathlib``, and
``muninn.queue`` (which itself is pure filesystem — no sqlite3, no store, no
parser). ``tests/test_indexer.py`` enforces this by asserting, after running
``main()`` in a subprocess, that neither ``sqlite3`` nor ``muninn.store``
appears in ``sys.modules``. That test is the thing keeping the 1.5s budget
safe against a future edit that adds a "just one" import here.

The entire body of ``main()`` is wrapped in ``try/except BaseException``: a
failing hook must never disrupt the user's session, so every code path here
— malformed stdin, an unwritable queue, a typo in argv — ends in exit code 0.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from ..queue import enqueue

# How long to wait for a payload before giving up. SessionEnd hooks share a
# 1.5-second budget, so this must stay well inside it: a hook that hangs is
# worse than a hook that does nothing, because it delays the user's shell.
STDIN_TIMEOUT_S = 0.5


def _now_iso() -> str:
    # time.strftime rather than datetime: one fewer stdlib module pulled into
    # this process's import graph. Not load-bearing for correctness, just
    # keeping the "minimal imports" contract honest in the file that most
    # needs it.
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _build_job(payload: dict) -> dict:
    """Shape the queued job. See docs/specs/003-background-indexer.md, Step 1.

    Only the fields the indexer needs are copied across — never the whole
    payload verbatim, so an unexpected/renamed vendor field cannot silently
    widen what this hook writes to disk.
    """
    return {
        "v": 1,
        "kind": "session-end",
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "cwd": payload.get("cwd"),
        "reason": payload.get("reason"),
        "enqueued_at": _now_iso(),
    }


def _read_stdin_payload() -> dict:
    """Read the SessionEnd payload from stdin.

    A blocking ``sys.stdin.read()`` is a hazard here, not a convenience: if the
    caller holds stdin open, this hook waits forever inside a 1.5-second shared
    budget (see .valholl/articles/session-lifecycle-facts.md). Windows CI caught
    exactly that — an empty-but-open stdin hung until the test's timeout, where
    POSIX had happened to deliver EOF immediately.

    So the read is bounded. On POSIX a select() with a short timeout tells us
    whether anything is there at all; on Windows, where select() does not accept
    pipes, fall back to the plain read and rely on the caller closing stdin as
    Claude Code actually does.
    """
    if sys.platform != "win32":
        import select
        ready, _, _ = select.select([sys.stdin], [], [], STDIN_TIMEOUT_S)
        if not ready:
            raise ValueError("no SessionEnd payload arrived on stdin")
    raw = sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("SessionEnd payload was not a JSON object")
    return payload


def _handle_session_end(queue_dir: Path | None) -> None:
    payload = _read_stdin_payload()
    job = _build_job(payload)
    kwargs = {} if queue_dir is None else {"queue_dir": queue_dir}
    # enqueue() itself never raises -- it returns None on any failure -- so
    # an unwritable queue dir is already silently absorbed here. This call
    # is not wrapped again; the outer try/except in main() is strictly for
    # failures in *this* function (a malformed payload, an argv typo).
    enqueue(job, **kwargs)


def _run_self_test(queue_dir: Path | None) -> bool:
    """Feed a synthetic payload through and confirm a job file landed.

    Used by ``install-hooks`` to verify the wiring end to end, and directly by
    an operator via ``muninn-hook session-end --self-test``. Diagnostics go to
    stderr only, matching the "never print to stdout on success" rule for the
    real hook path -- stdout may be interpreted by the harness invoking this
    process.
    """
    payload = {
        "session_id": "muninn-self-test",
        "transcript_path": "/dev/null",
        "cwd": "/tmp",
        "reason": "other",
    }
    job = _build_job(payload)
    kwargs = {} if queue_dir is None else {"queue_dir": queue_dir}
    written = enqueue(job, **kwargs)
    ok = written is not None and written.exists()
    if ok:
        sys.stderr.write(f"muninn-hook: self-test OK, wrote {written}\n")
    else:
        sys.stderr.write("muninn-hook: self-test FAILED, no job file was written\n")
    return ok


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``muninn-hook`` console script.

    Always returns 0 except when ``--self-test`` explicitly fails -- that is
    a diagnostic path meant to surface a wiring problem to the operator
    running it directly, never the live ``SessionEnd`` invocation. Everything
    else is wrapped in ``except BaseException`` on purpose: a
    ``KeyboardInterrupt`` or a bug in this module must degrade to "did
    nothing" rather than an unhandled traceback reaching the user's shell mid
    session-exit.
    """
    try:
        args = list(sys.argv[1:] if argv is None else argv)
        if not args or args[0] != "session-end":
            # Unknown/missing subcommand. Nothing to do; never disrupt exit.
            return 0

        rest = args[1:]
        self_test = "--self-test" in rest
        queue_dir: Path | None = None
        for i, tok in enumerate(rest):
            if tok == "--queue-dir" and i + 1 < len(rest):
                queue_dir = Path(rest[i + 1])

        if self_test:
            return 0 if _run_self_test(queue_dir) else 1

        _handle_session_end(queue_dir)
        return 0
    except BaseException as exc:  # intentional catch-all, see docstring
        try:
            sys.stderr.write(f"muninn-hook: {type(exc).__name__} (ignored)\n")
        except BaseException:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
