"""The raven side of the shared menubar: descriptor, menu payload, sanitising.

Normative sources: docs/specs/009-raven-descriptor-menu.md and
.valholl/articles/shared-menubar.md. The wire contract itself is Appistry's
``SPEC.md`` ("The Raven Protocol", version 1); this module is Muninn's
implementation of the *raven* half of it, and the shapes here must match that
document rather than seeming reasonable on their own.

Three things live here and nothing else: where the descriptor goes, what it
says, and what ``/api/menu`` returns. The HTTP surface is ``muninn/ravenserve.py``
so that a reader can check the payload against the spec without also reading a
request handler, and so a test can build the payload with no port bound.

## Muninn is the companion raven, deliberately

Huginn is Thought, Muninn is Memory (.valholl/articles/what-muninn-is.md), and
the menubar reflects that:

- ``HOST_PRIORITY`` is 50 against Huginn's 100, so Huginn leads when both are
  running and Muninn's section sorts first — alone — when it is not. Neither
  raven knows the other exists; these two numbers are the whole of the ordering.
- **Every row is a link.** Muninn publishes no ``action`` endpoint, because a
  history console has nothing that should be mutated from a menu. Naming the
  mistake: adding one action "just to open a session" would put a POST endpoint
  on this port for the rest of the project's life, and the same row works as a
  ``url`` that the host opens against Muninn's own port.
- **No ``token_path``.** See ravenserve.py, which is where that decision is
  actually load-bearing, and docs/specs/009 for the reasoning.

## Everything in a label is attacker-influenceable

Session titles, ``cwd`` paths, and topics all come from transcripts, which
contain whatever a user pasted and whatever a tool printed. They reach a desktop
menu through this module. So nothing becomes a label without going through
:func:`safe_label`, which strips ANSI escapes, control characters, and bidi
overrides, collapses whitespace, and caps the length.

Appistry sanitises host-side too (its ``sanitize.py``). That is not a reason to
skip it here, and the reader who assumes it is has the threat model backwards:
Appistry defends *itself* from a hostile raven, whereas this defends Muninn's
users from hostile *transcript content* Muninn is the one that read. Both are
needed, and the raven's is the one that knows which strings are untrusted.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# ── Protocol version ──────────────────────────────────────────────────────────

#: The raven protocol version Muninn primarily speaks.
API_VERSION = 1

#: The inclusive window Muninn accepts, declared as a *range*. Comparing a
#: version for equality is the bug behind tohuw/huginn#38: one routine bump
#: silently disabled every participant with nothing on screen to explain it.
#: Widening what Muninn accepts is a one-line change to MAX_API.
MIN_API = 1
MAX_API = 1

NAME = "muninn"
DISPLAY = "Muninn"

#: Lower than Huginn's 100. Ordering is data the ravens supply; the host has no
#: list of known ravens and no opinion about which should lead.
HOST_PRIORITY = 50

#: Environment override for the shared descriptor directory, named by the
#: protocol so a test harness (or a user who relocates state wholesale) can
#: point every participant at one alternate location.
STATE_DIR_ENV = "RAVENS_STATE_DIR"

# ── Sanitising ────────────────────────────────────────────────────────────────

# CSI/OSC sequences and the short two-character escapes, matched before the
# control-character strip below. Order matters: stripping controls first would
# leave the printable tail of "\x1b[31m" behind as the literal text "[31m".
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]"   # CSI ... final byte
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"     # OSC ... BEL or ST
    r"|[@-Z\\-_])"                        # two-character escapes
)

# C0 minus the whitespace handled separately, DEL, and C1. C1 is included
# because a lone 0x9b is an alternate CSI introducer on some terminals, so
# stripping ESC alone is not enough.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

# Explicit bidi controls (LRE/RLE/PDF/LRO/RLO and the isolates) plus the
# invisible formatting characters used to disguise text. A label without these
# still renders as the bytes that produced it; with them, "Quit" can render as
# "tiuQ" and a menu row can read as something it is not.
_SPOOF_RE = re.compile(
    "["
    "​-‏"      # zero-width space/joiners, LRM/RLM
    "‪-‮"      # LRE, RLE, PDF, LRO, RLO
    "⁠-⁤"      # word joiner and invisible operators
    "⁦-⁩"      # LRI, RLI, FSI, PDI
    "﻿"             # BOM / zero-width no-break space
    "]"
)

_WHITESPACE_RE = re.compile(r"[\s   -     　]+")

#: Appistry's own caps (its ``menu_spec.py``). Muninn truncates to the same
#: numbers rather than sending longer strings and letting the host cut them: a
#: label that is trimmed host-side loses its end mid-word with no ellipsis, and
#: the raven is the side that knows where a sensible break is.
MAX_LABEL = 120
MAX_DETAIL = 80

_ELLIPSIS = "…"


def safe_label(value: object, limit: int = MAX_LABEL) -> str:
    """Reduce ``value`` to one bounded, control-free line, or ``""``.

    Non-strings become ``""`` rather than being coerced. Naming the mistake a
    reader might make here: ``str(value)`` looks harmless and is not — a title
    that arrived as a dict would put ``repr()``'s attacker-chosen punctuation
    and quoting on screen, which is exactly the kind of thing a spoofed menu row
    is built out of.

    A string that sanitises to nothing (all escapes, say) returns ``""``, and
    every caller in this module treats that as "no label", which drops the row.
    That is deliberate: Appistry drops an unlabelled item too, so a row that
    cannot be described must not be rendered as a clickable blank.
    """
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPOOF_RE.sub("", cleaned)
    # Any ESC left over was not part of a recognised sequence.
    cleaned = cleaned.replace("\x1b", "")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + _ELLIPSIS
    return cleaned


# ── Where the descriptor goes ─────────────────────────────────────────────────

def state_dir() -> Path:
    """Return the *shared* raven descriptor directory.

    Resolution order, which every participant must implement identically:

    1. ``$RAVENS_STATE_DIR``, if set and non-empty.
    2. Windows: ``%LOCALAPPDATA%\\Ravens`` (``~\\AppData\\Local\\Ravens`` when
       ``LOCALAPPDATA`` is unset).
    3. POSIX: ``$XDG_STATE_HOME/ravens``, else ``~/.local/state/ravens``.

    **This is not muninn/paths.py's STATE_DIR and must never be replaced by
    it.** That is the mistake to name: ``paths.STATE_DIR`` resolves to
    ``.../muninn``, and a descriptor written there is a descriptor the host never
    looks at. The failure is completely silent — an empty menubar with nothing on
    screen to explain it — because a raven with no descriptor is
    indistinguishable from a raven that was never installed.

    The rule mirrors Appistry's ``paths``/``ravens`` resolution byte for byte,
    and that identity is the contract rather than either project's preference.
    """
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "Ravens"
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "ravens"


def descriptor_path() -> Path:
    """Return the path Muninn's own descriptor is published at."""
    return state_dir() / f"{NAME}.json"


def descriptor(port: int, *, pid: int | None = None,
               started: float | None = None) -> dict[str, Any]:
    """Build the descriptor document for a server listening on ``port``.

    ``started`` is this process's start time in epoch seconds. Supplying it is
    not optional in practice even though the protocol marks it optional: the host
    cross-checks it against the OS's own record of when ``pid`` began, and
    without it a recycled PID passes as a live raven — so the user sees a Muninn
    section that is not backed by anything running.

    ``time.time()`` rather than a real process-start reading, and the two-second
    slack in Appistry's check is what makes that acceptable: this value is taken
    within milliseconds of the process starting in every path that calls it.
    Naming the mistake: ``time.monotonic()`` here would be worse than useless —
    it is not epoch-based, so the cross-check would fail against every live
    process rather than only against recycled PIDs.

    No ``token_path`` and no ``token_header``, which is a decision rather than an
    omission — see ravenserve.py and docs/specs/009.
    """
    return {
        "api_version": API_VERSION,
        "min_api": MIN_API,
        "max_api": MAX_API,
        "name": NAME,
        "display": DISPLAY,
        "pid": os.getpid() if pid is None else pid,
        "port": port,
        "started": time.time() if started is None else started,
        "host_priority": HOST_PRIORITY,
        # Only "menu". Omitting "action" is how a raven says it has nothing to
        # be clicked; Appistry renders link rows identically either way.
        "endpoints": {"menu": "/api/menu"},
    }


def publish(port: int, *, directory: Path | None = None,
            started: float | None = None) -> Path:
    """Write the descriptor atomically and owner-only. Returns its path.

    Call this **after** the port is bound, never before: a descriptor naming a
    port that is not yet listening makes the host report a healthy Muninn as
    "Is not answering on its recorded port." during startup.

    The temp file is staged in the same directory (so ``os.replace`` cannot
    cross a filesystem boundary and fall back to a non-atomic copy) and its mode
    is set *before* the replace. That ordering is the point, and it matches
    store.py's 0600 discipline for the archive: creating the final file first and
    chmodding after leaves a window in which it is world-readable, and this file
    names a loopback port that accepts unauthenticated requests.

    The directory is 0700 for the same reason. Note that it is *shared* with
    other ravens owned by the same user, not with other users — one user's
    Huginn has no business reading another user's descriptors.
    """
    directory = state_dir() if directory is None else Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _restrict(directory, 0o700)

    target = directory / f"{NAME}.json"
    payload = json.dumps(descriptor(port, started=started), indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{NAME}.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def withdraw(path: Path | None = None) -> None:
    """Remove the descriptor so a stopped Muninn leaves nothing behind.

    Best-effort, and deliberately not more than that. A ``SIGKILL`` skips this
    and the host copes — it checks the recorded PID and ``started`` before
    trusting the file, so a stale descriptor renders as "Not running (its
    recorded process is gone)." with a visible reason. Adding machinery to
    guarantee removal would buy nothing that the liveness check does not already
    provide, and would run in the shutdown path where it can only make things
    worse.
    """
    target = descriptor_path() if path is None else Path(path)
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _restrict(path: Path, mode: int) -> None:
    """Best-effort owner-only mode. A no-op on Windows, which uses ACLs.

    Windows is not silently ignored so much as deliberately left alone: NTFS
    does not honour mode bits, and Muninn has no pywin32 dependency to set a
    DACL with. The descriptor names a port, not a credential, and the endpoint
    it points at requires nothing — so on Windows this file is as sensitive as
    the fact that Muninn is running, which is already visible in the task list.
    """
    if sys.platform == "win32":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


# ── The menu payload ──────────────────────────────────────────────────────────

#: Rows of recent history. Kept well under Appistry's 50-per-section and
#: 200-total caps, because a menu is read at a glance: the budget is what stops
#: a hostile payload hanging the host's menu build, not a target to fill.
RECENT_LIMIT = 8

#: A session id reaches a URL path, so it is constrained rather than escaped.
#: Covers Claude Code / Codex uuid-shaped ids and export ids alike. Refusing an
#: id that is not a plain token is simpler to be sure of than quoting one, and
#: costs nothing: Muninn's ids are transcript filename stems and export uuids,
#: a shape that is already this narrow.
_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _relative_when(started_at: str | None, now: float | None = None) -> str:
    """Render an ISO8601 timestamp as a short "12m ago", or "" if unparseable.

    Muninn stores ``started_at`` as whatever the transcript claimed, so this
    parses defensively and returns ``""`` rather than raising or guessing. An
    absent relative time costs a row its ``detail``; a raised exception would
    cost the whole menu.
    """
    if not isinstance(started_at, str) or not started_at:
        return ""
    text = started_at.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        # A naive timestamp is treated as UTC, matching how ingest normalises.
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    seconds = (time.time() if now is None else now) - parsed.timestamp()
    # A negative age is a clock skew or a transcript timestamped in the future,
    # and it collapses into "just now" with everything under 90 seconds. Not
    # signed arithmetic on purpose: "in 3 hours" in a history menu reads as a
    # bug in Muninn rather than as a fact about the transcript.
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days}d ago"


def _plural(count: int, noun: str) -> str:
    """``noun`` or ``noun + "s"``. A menubar row is prose the user reads.

    "1 sessions" is the kind of detail that makes a tool look unfinished, and the
    fix is cheaper than the impression. Only regular nouns are needed here.
    """
    return noun if count == 1 else f"{noun}s"


def _session_label(row: dict[str, Any]) -> str:
    """Build one recent-session row's label from the best text available.

    Preference order is topic, then title, then the basename of ``cwd``. All
    three come from a transcript and all three go through :func:`safe_label`;
    the ordering is about usefulness, not trust, and there is no "trusted"
    branch here for exactly that reason.
    """
    for key in ("topic", "title"):
        label = safe_label(row.get(key))
        if label:
            return label
    cwd = row.get("cwd")
    if isinstance(cwd, str) and cwd:
        # Basename only. A full path in a menu row is both unreadable at that
        # width and a gratuitous disclosure of the user's directory layout to
        # anything that can screenshot the menubar.
        label = safe_label(os.path.basename(cwd.rstrip("/\\")))
        if label:
            return label
    # Last resort: the id prefix. Never an empty label, which Appistry drops —
    # a session that exists should be visible even when nothing named it.
    session_id = row.get("session_id")
    return safe_label(session_id)[:12] if isinstance(session_id, str) else ""


def build_menu(*, recent: list[dict[str, Any]], sessions: int, chunks: int,
               lag: dict[str, dict[str, Any]] | None = None,
               last_sweep: str | None = None,
               pending_jobs: int = 0) -> dict[str, Any]:
    """Build the ``/api/menu`` payload from already-queried archive facts.

    Takes plain data rather than a ``Store`` so the payload can be built and
    checked against Appistry's parser with no database and no port — and so the
    one function that decides what the menubar says has no I/O in it at all.

    Two sections, in the order a reader of a *history* console wants them:

    1. **Recent** — the last few sessions, each a link into ``muninn show``'s
       web equivalent on this raven's own port.
    2. **Archive** — counts, index freshness, and queue depth. This is where
       staleness becomes visible, which is the same principle ``muninn doctor``
       is built on (docs/specs/003): an index that silently stopped keeping up
       is the failure this project has already been bitten by.

    No ``badge``. A badge is a count of things wanting *attention*, and nothing
    in a history archive is waiting on the user — Huginn owns that. Naming the
    mistake: putting the session count in the badge would add Muninn's whole
    corpus size to Huginn's approval count, since the host sums badges across
    ravens, and the menubar would read as thousands of pending decisions.
    """
    sections: list[dict[str, Any]] = []

    recent_items: list[dict[str, Any]] = []
    for row in recent[:RECENT_LIMIT]:
        label = _session_label(row)
        if not label:
            continue
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
            continue
        detail_parts = [p for p in (
            safe_label(row.get("source"), 16),
            _relative_when(row.get("started_at")),
        ) if p]
        recent_items.append({
            "label": label,
            "detail": safe_label(" · ".join(detail_parts), MAX_DETAIL),
            "url": f"/session/{session_id}",
            "style": "muted",
        })

    if recent_items:
        recent_items.append({"separator": True})
        recent_items.append({"label": "Search history…", "url": "/"})
        sections.append({"id": "recent", "title": "Recent sessions", "items": recent_items})

    archive_items: list[dict[str, Any]] = [{
        "label": f"{sessions:,} {_plural(sessions, 'session')} · "
                 f"{chunks:,} {_plural(chunks, 'chunk')}",
        "url": "/",
        "style": "muted",
    }]

    behind = _lag_summary(lag)
    if behind:
        # "attention" rather than "muted": an index that is behind is the one
        # thing in this section a user might need to act on, and the whole
        # reason index lag is surfaced at all rather than left to be discovered.
        archive_items.append({"label": behind, "url": "/", "style": "attention"})

    if pending_jobs > 0:
        archive_items.append({
            "label": f"{pending_jobs:,} {_plural(pending_jobs, 'session')} queued to index",
            "url": "/",
            "style": "muted",
        })

    swept = safe_label(last_sweep, MAX_DETAIL)
    archive_items.append({
        "label": "Last full scan",
        # An unindexed archive says so rather than showing a blank detail: the
        # difference between "never swept" and "swept, detail missing" is
        # exactly the invisible staleness doctor exists to prevent.
        "detail": _relative_when(swept) or (swept or "never"),
        "url": "/",
        "style": "muted",
    })

    sections.append({"id": "archive", "title": "Archive", "items": archive_items})

    return {
        "api_version": API_VERSION,
        "title": DISPLAY,
        "sections": sections,
    }


def _lag_summary(lag: dict[str, dict[str, Any]] | None) -> str:
    """Return a one-line "N file(s) not yet indexed", or "" when up to date.

    Shape matches ``ingest.index_lag``'s return value. A source whose count is
    missing or non-integer contributes nothing rather than raising: this is on
    the menu-build path, and a malformed lag reading must cost one line, not the
    whole menu.
    """
    if not isinstance(lag, dict):
        return ""
    total = 0
    for info in lag.values():
        if not isinstance(info, dict):
            continue
        pending = info.get("unindexed_or_grown_files")
        if isinstance(pending, bool) or not isinstance(pending, int):
            continue
        total += max(0, pending)
    if total <= 0:
        return ""
    return f"{total:,} {_plural(total, 'file')} not yet indexed"
