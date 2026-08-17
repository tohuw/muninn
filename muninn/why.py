"""Why is this file the way it is — decision-level blame.

``git blame`` answers *who changed this line, and when*. It has never been able
to answer the question people actually ask when they find a strange piece of
code, which is **why**. The reasoning that produced a line happened in a
conversation, and that conversation is exactly what this archive holds.

So this joins the two halves nobody has joined before: git knows which commits
touched a file, Muninn knows what was being attempted, argued about and decided
while those commits were being written. Attributing one to the other turns
`fix(auth): handle expired refresh` into the session that explains *what the
alternatives were and why this one won*.

## Matching a commit to a session

By **time overlap**: the session whose lifetime contains the commit's author
date. Crude-sounding, and it holds up — checked against a real repository, 25 of
25 recent commits landed inside exactly one session's window.

The obvious alternative, matching a session's ``cwd`` to the repository, is
**wrong**, and measuring it is what produced this module. On the archive it was
written against, *zero* sessions had a ``cwd`` under the muninn checkout while
that repository's entire recent history had been written from sessions rooted in
a sibling one. ``cwd`` records where an agent was launched, not where the work
landed; anyone who works across two repositories from one shell breaks it, and
the failure is silent — an empty result that reads like "no history".

Files touched are the honest signal, and where they disagree with ``cwd``, they
are right. They come from the tool calls the agent actually made.

## Overlap alone is not evidence, and the corpus settles it

The first version also offered any session that merely happened to be *open*
when a commit landed. It reads as reasonable and the measurement killed it.

Session length spans four orders of magnitude: the median is **12 minutes**, but
p90 is **7 days**, p95 is **27 days**, and the longest ran 271 days. 21% of
sessions stay open more than a day. A session parked for a month overlaps every
commit made that month, so at one arbitrary commit instant **8 sessions were
"open"** — of which at most one wrote it. That is not a weak signal to be
labelled and shown; it is noise that would bury the true attribution under
whatever else the person had left running. The first live run put three
Cyberpunk-modding sessions under a change to this very file.

So attribution requires the session to have actually **touched something**:

1. it edited **this file** — the strong claim
2. it edited **something else in this repository** — weaker, still a connection

A commit that matches neither is listed with no session, which is honest and
frequently correct: plenty of commits are written by hand.

**No model is called anywhere in this module.** It is git plus SQL.
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .store import Store

#: Provenance classes that describe work a person did. A `claude -p` byproduct
#: did not decide anything, and attributing a commit to one would be worse than
#: leaving the commit unattributed.
RECALLABLE = ("human", "subagent")

#: How many commits to walk by default. A file's recent history is what anyone
#: is asking about; the whole history of a long-lived file is a different and
#: much less useful report.
DEFAULT_COMMITS = 20

#: Confidence that a session explains a commit, strongest first. Reported rather
#: than collapsed into a boolean, because "edited this exact file" and "was
#: working elsewhere in this repo" are different claims to make about somebody's
#: work, and the reader should be told which one they are being offered.
TOUCHED_FILE = "touched-this-file"
TOUCHED_REPO = "touched-this-repo"

_RANK = {TOUCHED_FILE: 0, TOUCHED_REPO: 1}

#: Subprocess text decoding is pinned rather than left to the locale. `text=True`
#: decodes with the *locale* codec, which on a Windows console is cp1252, and a
#: commit subject containing an em dash or any non-Latin-1 byte then raises
#: UnicodeDecodeError — which is neither OSError nor SubprocessError, so it
#: sails past every handler written to catch a subprocess failing. This project
#: has been bitten by that exact class three times.
_GIT_TEXT = {"encoding": "utf-8", "errors": "replace", "text": True}


@dataclass(frozen=True)
class Attribution:
    """One session offered as an explanation for one commit."""

    session_id: str
    source: str
    confidence: str
    topic: str | None = None
    outcome: str | None = None
    summary: str | None = None
    decisions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "confidence": self.confidence,
            "topic": self.topic,
            "outcome": self.outcome,
            "summary": self.summary,
            "decisions": list(self.decisions),
        }


@dataclass(frozen=True)
class Change:
    """One commit, and whatever the archive can say about why it happened."""

    sha: str
    when: str
    subject: str
    author: str
    sessions: tuple[Attribution, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "when": self.when,
            "subject": self.subject,
            "author": self.author,
            "sessions": [s.to_dict() for s in self.sessions],
        }


@dataclass(frozen=True)
class Why:
    path: str
    repo: str | None
    changes: tuple[Change, ...] = ()
    #: Sessions that edited the file but produced no commit in the window —
    #: exploration, a reverted attempt, work still uncommitted. Invisible to git
    #: by construction, and often the interesting half.
    uncommitted: tuple[Attribution, ...] = ()
    unavailable: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "repo": self.repo,
            "changes": [c.to_dict() for c in self.changes],
            "uncommitted": [s.to_dict() for s in self.uncommitted],
            "unavailable": self.unavailable,
        }


# ── git ───────────────────────────────────────────────────────────────────────

def _git(repo: str, *args: str) -> str | None:
    """Run one read-only git command. None when git cannot answer.

    Never through a shell, and every caller passes a literal ``--`` before any
    path, so a filename can never be read as an option.
    """
    try:
        done = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, timeout=30, check=False, **_GIT_TEXT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def _comparable(path: str) -> str:
    """A path in the one form both sides can be compared in.

    ``git rev-parse --show-toplevel`` answers with forward slashes even on
    Windows, while the archive stores whatever the agent's tool call recorded —
    backslashes, there. Comparing the two raw matched nothing at all, so every
    session looked like it had touched no repository, and the whole
    repo-confidence tier silently reported empty.
    """
    return os.path.normpath(path).replace("\\", "/").casefold()


def repo_root(path: str) -> str | None:
    """The work tree containing ``path``, or None if it is not in one."""
    start = os.path.dirname(os.path.abspath(path)) or "."
    if not os.path.isdir(start):
        return None
    out = _git(start, "rev-parse", "--show-toplevel")
    if not out or not out.strip():
        return None
    return os.path.normpath(out.strip())


def commits_for(repo: str, path: str, *, limit: int = DEFAULT_COMMITS) -> list[Change]:
    """Recent commits touching ``path``, newest first.

    ``--follow`` so a rename does not truncate the history at the rename, which
    is often the exact moment the interesting decision was made.
    """
    out = _git(repo, "log", f"--max-count={limit}", "--follow",
               "--format=%H%x1f%aI%x1f%an%x1f%s", "--", path)
    if not out:
        return []
    changes = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        sha, when, author, subject = parts
        changes.append(Change(sha=sha, when=when, subject=subject, author=author))
    return changes


# ── matching ──────────────────────────────────────────────────────────────────

def _moment(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # A naive timestamp is read as UTC rather than discarded. Sources differ on
    # whether they record an offset, and dropping every naive session would
    # silently narrow this to whichever tools happen to be tz-aware.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _rows_for_file(st: Store, path: str, repo: str | None) -> dict[str, dict]:
    """Sessions that touched this file or this repository, keyed by session id.

    Matched on the absolute path when we have one, and on the basename as well
    because the same file is reachable by more than one path — a symlinked or
    renamed checkout, or a session that opened it through a relative path.
    """
    placeholders = ", ".join("?" for _ in RECALLABLE)
    basename = os.path.basename(path)
    absolute = os.path.abspath(path)
    # Fetched by basename or repo prefix and then filtered in Python, because
    # the comparison is case- and separator-insensitive and SQLite's LIKE is
    # neither in a way that can be relied on across the two spellings.
    like = f"%{os.sep}{basename}" if basename else "%"
    rows = st.conn.execute(
        f"SELECT s.session_id, s.source, s.started_at, s.ended_at, s.topic, "
        f"       s.outcome, s.summary, s.facets_json, f.path "
        f"FROM sessions s JOIN session_files f ON f.session_id = s.session_id "
        f"WHERE s.provenance IN ({placeholders}) "
        f"  AND (f.basename = ? OR f.path LIKE ? OR f.path LIKE ?)",
        (*RECALLABLE, basename, like,
         f"{repo}%" if repo else like)).fetchall()
    target = _comparable(absolute)
    root = _comparable(repo) + "/" if repo else None
    # Matched on the repo-relative tail **including the repository's own
    # directory name**, not the bare basename. A basename match would attribute
    # a different project's `cost.py` to this one, and that error is worse than
    # a miss: it arrives wearing the strongest confidence label the tool has.
    #
    # The repo name is part of the anchor because the tail alone is not
    # distinctive enough — `pkg/thing.py` exists in any number of checkouts, and
    # a test caught exactly that collision. Anchored this way the comparison
    # still survives a checkout rooted somewhere else, on another machine or
    # another platform, which an absolute path never does.
    tail = None
    if root and target.startswith(root):
        tail = os.path.basename(os.path.normpath(repo)).casefold() + "/" \
            + target[len(root):]
    found: dict[str, dict] = {}
    for row in rows:
        touched = _comparable(row["path"])
        in_file = touched == target or (tail is not None
                                        and touched.endswith("/" + tail))
        if in_file is False and tail is None:
            in_file = os.path.basename(touched) == basename.casefold()
        in_repo = bool(root) and touched.startswith(root)
        if not (in_file or in_repo):
            continue
        entry = found.setdefault(row["session_id"], dict(row))
        entry["_file"] = entry.get("_file", False) or in_file
        entry["_repo"] = entry.get("_repo", False) or in_repo
    return found


def _decisions(facets_json: Any) -> tuple[str, ...]:
    """Decisions recorded by enrichment, if any survived as a list of strings."""
    if not facets_json:
        return ()
    import json
    try:
        facets = json.loads(facets_json)
    except (TypeError, ValueError):
        return ()
    items = facets.get("decisions") if isinstance(facets, dict) else None
    if not isinstance(items, list):
        return ()
    return tuple(str(i) for i in items if isinstance(i, str) and i.strip())


def _attribution(row: dict, confidence: str) -> Attribution:
    return Attribution(
        session_id=row["session_id"],
        source=row["source"],
        confidence=confidence,
        topic=row["topic"],
        outcome=row["outcome"],
        summary=row["summary"],
        decisions=_decisions(row["facets_json"]),
    )


def explain(st: Store, path: str, *, limit: int = DEFAULT_COMMITS) -> Why:
    """Why ``path`` is the way it is: its commits, and the work behind them."""
    unavailable: dict[str, str] = {}
    repo = repo_root(path)
    if repo is None:
        unavailable["commits"] = (
            "not inside a git work tree, so there is no commit history to "
            "attribute — sessions that touched the file are still reported")
    changes = commits_for(repo, path, limit=limit) if repo else []
    if repo and not changes:
        unavailable["commits"] = "git records no commit touching this path"

    touching = _rows_for_file(st, path, repo)
    if not touching:
        unavailable["sessions"] = (
            "no archived session records touching this file; file lists come "
            "from an agent's own tool calls, so work done by hand leaves none")

    # Only sessions that touched the file or the repository are candidates.
    # Offering every session that merely overlapped in time put three unrelated
    # modding sessions under a change to this module on the first live run —
    # see the note at the top of this file for the numbers behind that.
    spans = []
    for row in touching.values():
        start, end = _moment(row["started_at"]), _moment(row["ended_at"])
        if start is None:
            continue
        spans.append((start, end or start, row))

    explained = []
    for change in changes:
        at = _moment(change.when)
        if at is None:
            explained.append(change)
            continue
        found = []
        for start, end, row in spans:
            if not (start <= at <= end):
                continue
            confidence = TOUCHED_FILE if row.get("_file") else TOUCHED_REPO
            found.append(_attribution(row, confidence))
        found.sort(key=lambda a: (_RANK[a.confidence], a.session_id))
        explained.append(Change(sha=change.sha, when=change.when,
                                subject=change.subject, author=change.author,
                                sessions=tuple(found)))

    committed = {a.session_id for c in explained for a in c.sessions}
    uncommitted = tuple(
        _attribution(row, TOUCHED_FILE)
        for sid, row in sorted(touching.items())
        if sid not in committed and row.get("_file")
    )
    return Why(path=path, repo=repo, changes=tuple(explained),
               uncommitted=uncommitted, unavailable=unavailable)
