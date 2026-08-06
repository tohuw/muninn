"""Reopen a past session in the tool that created it — while that is still possible.

The archive already holds everything this needs: ``source``, ``session_id`` and
``cwd``. So `muninn resume` is mostly a matter of printing the right invocation
from the right directory.

## The part that is not trivial: this command's usefulness decays

Muninn is an archive of record. Claude Code sweeps transcripts after
``cleanupPeriodDays`` (default 30), and for much of a corpus this archive is the
only surviving copy — which means the *majority* of what `muninn resume` can
find, it cannot resume. That is the normal end state, not a malfunction
(.valholl/articles/archive-of-record.md).

So the refusal is the important half of this module, and it is a refusal rather
than a warning. Emitting `claude --resume <id>` for a session whose transcript
was deleted months ago hands the user a command that fails with the vendor's own
error message, which says nothing about why — and the agent-facing contract in
CLAUDE.md says an agent should only ever transport claims the tool can prove.
"The transcript is gone; here is what the archive still has" is provable. "Try
this and see" is not.

Every refusal therefore names the alternative that does work — `muninn show` —
because the prose is still there. That is the whole point of having kept it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: How each source's own CLI reopens a session. Only sources that *have* such a
#: command appear here; the rest are refused by name rather than by a guess.
#:
#: ``claude-cloud`` and ``chatgpt-cloud`` are deliberately absent: those
#: conversations live in a web UI, and there is no local invocation to print.
RESUME_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "--resume"],
    "codex": ["codex", "resume"],
}


@dataclass(frozen=True)
class ResumePlan:
    """Either a command to run, or a reason there is none. Never both.

    ``refusal`` being the only other field is deliberate: a plan with a command
    *and* a caveat would invite a caller to print the command anyway, which is
    the exact behaviour this module exists to prevent.
    """

    session_id: str
    source: str
    cwd: str | None = None
    command: list[str] | None = None
    refusal: str | None = None

    def shell(self) -> str | None:
        """The invocation as a copy-pasteable line, ``cd``-prefixed if needed."""
        if self.command is None:
            return None
        import shlex

        line = shlex.join(self.command)
        return f"cd {shlex.quote(self.cwd)} && {line}" if self.cwd else line


def plan(rec: dict[str, Any]) -> ResumePlan:
    """What, if anything, would reopen the session in ``rec``.

    Pure: takes an archive row, returns a decision. Nothing here touches the
    filesystem or a subprocess, so every refusal below is testable without a
    Claude or Codex install present.

    The order of checks is the order of certainty, most certain first. A
    subagent has no resumable identity *at all* — that is true regardless of
    whether its transcript survives — so it is answered before the question of
    presence, which is merely true today.
    """
    session_id = rec.get("session_id") or ""
    source = rec.get("source") or "?"
    cwd = rec.get("cwd") or None
    origin = rec.get("origin") or "raw"

    if rec.get("provenance") == "subagent":
        return ResumePlan(session_id, source, cwd, refusal=(
            "this is a subagent transcript, which has no session of its own to "
            "reopen. Its parent may be resumable"
            + (f": try {rec['parent_id']}" if rec.get("parent_id") else "")))

    if source not in RESUME_COMMANDS:
        return ResumePlan(session_id, source, cwd, refusal=(
            f"{source!r} sessions have no local resume command — they were not "
            f"created by a CLI this can invoke"))

    if origin != "raw":
        # A prose-index session was recovered from a predecessor's archive. The
        # vendor swept its transcript long ago by construction, so this is the
        # same refusal as below and not a different one.
        return ResumePlan(session_id, source, cwd, refusal=(
            f"this session was recovered from a {origin} archive, not from a "
            f"transcript the tool can still reopen"))

    if not rec.get("source_present"):
        return ResumePlan(session_id, source, cwd, refusal=(
            "the original transcript no longer exists — the vendor swept it. "
            "The archive is now the only copy"))

    command = [*RESUME_COMMANDS[source], session_id]
    return ResumePlan(session_id, source, cwd, command=command)
