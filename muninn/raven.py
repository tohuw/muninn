"""The raven side of the shared menubar: descriptor, menu payload, sanitising.

Normative sources: docs/specs/009-raven-descriptor-menu.md and
.valholl/articles/shared-menubar.md. The wire contract itself is
``SPEC.md`` ("The Raven Protocol", version 1) in the host's repository,
https://github.com/tohuw/roost; this module is Muninn's implementation of the
*raven* half of it, and the shapes here must match that document rather than
seeming reasonable on their own.

**A note on the host's name, because this file and its neighbours say "Appistry"
throughout.** The public shared menubar is now **Roost** — the repository was
renamed ``tohuw/appistry`` to ``tohuw/roost``, and its runtime was already
``roost``. "Appistry" in these docstrings means that host under its old name and
nothing else; the *internal* Cisco app launcher of the same name is unrelated to
the protocol and is not what any of them refer to. The old references are left in
place rather than swept, because none of them is a URL or an import and rewriting
prose in eight files would obscure the two changes that matter here. Nothing about
the protocol changed with the name.

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
  mistake: adding one action "just to open a session" would put a mutating op
  on this surface for the rest of the project's life, and the same row works as
  a ``url`` the host resolves against this raven's own address instead.
- **No ``token_path`` on POSIX.** See ravenserve.py, which is where that
  decision is actually load-bearing, and docs/specs/009 and 021 for the
  reasoning. Windows carries one — not a choice, a consequence of Windows
  having no Unix domain socket, spelled out in 021.

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
from typing import Any, Mapping

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

#: The two transports this raven can publish. Chosen by ravenserve.py at
#: startup, by platform capability rather than preference: POSIX gets a Unix
#: domain socket (TRANSPORT_UNIX), because Python's socket module has no
#: AF_UNIX on Windows, which gets a named pipe (TRANSPORT_PIPE) instead. See
#: docs/specs/021-unix-socket-transport.md for why the two need different
#: security treatment rather than sharing one.
TRANSPORT_UNIX = "unix"
TRANSPORT_PIPE = "pipe"

#: Environment override for the shared descriptor directory, named by the
#: protocol so a test harness (or a user who relocates state wholesale) can
#: point every participant at one alternate location.
STATE_DIR_ENV = "RAVENS_STATE_DIR"

#: Request ops, named rather than repeated as literals because the descriptor
#: advertises them and the server routes them, and those two must not be able
#: to disagree. These used to be HTTP paths (``/api/menu``,
#: ``/api/menu/action``); a Unix socket or named pipe has no URL space, so a
#: request now names which op it wants in the JSON body instead of the path,
#: and these constants are that op name rather than a path.
MENU_OP = "menu"
ACTION_OP = "action"

#: The two action ids Muninn publishes. They are ordinary ids and that is the
#: whole design: the host draws the label and posts the id back exactly as it
#: does for a link row, and it does not know that one of these ends the process
#: it is talking to. Nothing in the protocol reserves these words, so adding them
#: needed no version bump and ``MAX_API`` above is unchanged.
#:
#: There is deliberately **no start id.** A stopped daemon has withdrawn its
#: descriptor, so there is no menu for a "Start Muninn" row to live in and no
#: process to serve it. Starting at login is ``muninn install-agent``'s job,
#: which puts the exec path in the OS supervisor rather than in a menu bar.
QUIT = "quit"
RESTART = "restart"

#: A posted action id is truncated to this before it is compared or logged. Both
#: ids Muninn publishes are far shorter, so nothing legitimate is affected, and
#: an unbounded string never reaches a log line.
MAX_ACTION_ID = 128

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

def state_dir(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
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

    ``env``/``home`` default to the live process and exist for one caller:
    ``agent_install`` asks where this resolves for a login session that does not
    inherit the installing shell. The resolution order above is unaffected —
    only *which* environment it reads.
    """
    env = os.environ if env is None else env
    home = Path.home() if home is None else home
    override = (env.get(STATE_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        local = (env.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "Ravens"
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg) if xdg else home / ".local" / "state"
    return base / "ravens"


def descriptor_path() -> Path:
    """Return the path Muninn's own descriptor is published at."""
    return state_dir() / f"{NAME}.json"


def socket_path(directory: Path | None = None) -> Path:
    """Where the POSIX Unix domain socket is bound. Unused on Windows."""
    return (state_dir() if directory is None else Path(directory)) / f"{NAME}.sock"


def pages_dir(directory: Path | None = None) -> Path:
    """Where the static pages a menu link opens are rendered.

    Separate from the descriptor directory's top level rather than a sibling
    of ``muninn.json``: pages are per-session render output, not protocol
    state, and giving them their own subdirectory means a future raven-side
    cleanup pass can rm one directory without a pattern match against
    everything else ``ravens/`` holds for other ravens.
    """
    return (state_dir() if directory is None else Path(directory)) / NAME / "pages"


def token_path(directory: Path | None = None) -> Path:
    """Where the Windows named-pipe authkey is written. Unused on POSIX.

    POSIX needs no credential because the socket file's own 0600 mode is the
    entire trust boundary. A named pipe's default security descriptor does
    not give that same same-user-only guarantee, so the Windows transport
    additionally hands ``multiprocessing.connection`` an authkey, and this is
    where the file naming it lives so Roost can find it from the descriptor.
    """
    return (state_dir() if directory is None else Path(directory)) / f"{NAME}.token"


def _process_started(pid: int) -> float:
    """``pid``'s start time per the OS, falling back to the wall clock."""
    from .store import process_start_time

    actual = process_start_time(pid)
    return float(actual) if actual else time.time()


def descriptor(address: str, transport: str, pages_dir: str, *,
               token_path: str | None = None,
               pid: int | None = None,
               started: float | None = None,
               actions: bool = False) -> dict[str, Any]:
    """Build the descriptor document for a listener bound at ``address``.

    ``transport`` is one of :data:`TRANSPORT_UNIX` or :data:`TRANSPORT_PIPE`.
    ``address`` is a filesystem path for the former, a named-pipe path
    (``\\\\.\\pipe\\...``) for the latter — Roost dispatches on ``transport``
    rather than sniffing the address shape, because the two are visually
    similar on POSIX and guessing would be exactly the kind of implicit
    coupling this protocol otherwise avoids.

    ``pages_dir`` is where this raven has rendered (or will render) every
    file a ``url`` in the menu payload can point to. Roost resolves a link
    row's ``url`` against it and refuses anything whose realpath escapes it —
    see the client-side containment rule in Roost's ``SPEC.md``.

    ``token_path``, when given, names a file holding the authkey a Windows
    named-pipe client must present. Omitted on POSIX, where the socket's own
    file mode already is the credential.

    ``started`` is this process's start time in epoch seconds. Supplying it is
    not optional in practice even though the protocol marks it optional: the host
    cross-checks it against the OS's own record of when ``pid`` began, and
    without it a recycled PID passes as a live raven — so the user sees a Muninn
    section that is not backed by anything running.

    Read from the OS rather than from the wall clock. This used to be
    ``time.time()``, on the reasoning that publish happens within milliseconds of
    the process starting so the two-second slack absorbs the difference. That
    reasoning had one path wrong, and it is a path the user reaches from the
    menu: a **Restart** does not start a new process. ``cli._run_ingest_loop``
    loops in place, so the second pass republished with a fresh timestamp on a
    process the OS says began minutes or hours earlier. The cross-check then
    failed and the host declared a perfectly healthy Muninn gone — the empty
    "its recorded process is gone" section, with the daemon still running.

    Cold starts were the latent half of the same bug: publish comes after the
    lock, the store open and the raven bind, so a slow disk could put it past
    two seconds on a first run too.

    ``store.process_start_time`` returns None where the OS cannot answer, and
    then this falls back to the wall clock — a descriptor with no usable
    ``started`` is still better than no descriptor. Naming the mistake:
    ``time.monotonic()`` here would be worse than useless — it is not
    epoch-based, so the cross-check would fail against every live process rather
    than only against recycled PIDs.

    No ``token_path`` on POSIX, which is a decision rather than an omission —
    see ravenserve.py and docs/specs/009 and 021. Windows carries one, for the
    reason given above.

    ``actions`` advertises the action endpoint. It defaults to **False** so that
    every caller that does not wire an action handler keeps saying "there is
    nothing here to click" — the descriptor must not promise an op that answers
    "publishes no actions", because the host would draw a row and the click
    would fail. Only the daemon, which can honour Quit and Restart, passes True.
    """
    endpoints: dict[str, str] = {"menu": MENU_OP}
    if actions:
        endpoints["action"] = ACTION_OP
    process_id = os.getpid() if pid is None else pid
    return {
        "api_version": API_VERSION,
        "min_api": MIN_API,
        "max_api": MAX_API,
        "name": NAME,
        "display": DISPLAY,
        "pid": process_id,
        "transport": transport,
        "address": address,
        "pages_dir": pages_dir,
        **({"token_path": token_path} if token_path else {}),
        "started": _process_started(process_id) if started is None else started,
        "host_priority": HOST_PRIORITY,
        # Omitting "action" is how a raven says it has nothing to be clicked;
        # Appistry renders link rows identically either way.
        "endpoints": endpoints,
        # How a shared host may ask this platform's supervisor to start Muninn
        # again. An identifier, never a command: the host must never execute a
        # path named in a file anything running as this user can write. Omitted
        # where the platform has no such mechanism, so the host draws no Start
        # row rather than one that cannot work.
        **({"launch": block} if (block := _launch_block()) else {}),
    }


def _launch_block() -> dict | None:
    """The descriptor's ``launch`` block, or None if it cannot be derived.

    corvidae owns this mapping and Huginn already uses its helper, but corvidae
    ships on its own cadence and the version Muninn's floor allows predates it.
    So: use the shared helper when it is there, and derive the same three cases
    locally when it is not. Forcing a corvidae release and a floor bump to land
    one optional descriptor field would be the tail wagging the dog, and this
    converges on the shared helper the moment the floor moves.

    Never fatal. A raven that cannot say how to restart itself still publishes a
    usable descriptor, and the host degrades to a reason with no Start row.
    """
    try:
        from . import agent_install

        spec = agent_install.spec()
        try:
            from corvidae.login_agent import launch_descriptor
        except ImportError:
            pass
        else:
            return launch_descriptor(spec)

        if sys.platform == "darwin":
            return {"kind": "launchd", "id": spec.label}
        if sys.platform.startswith("linux"):
            return {"kind": "systemd", "id": spec.unit_name}
        if os.name == "nt":
            return {"kind": "windows-run", "id": spec.run_value}
        return None
    except Exception:  # noqa: BLE001 - a diagnostic must not cost the descriptor
        return None


def publish(address: str, transport: str, pages_dir: str, *,
            token_path: str | None = None,
            directory: Path | None = None,
            started: float | None = None,
            actions: bool = False) -> Path:
    """Write the descriptor atomically and owner-only. Returns its path.

    Call this **after** the listener is bound, never before: a descriptor
    naming an address nothing is listening on makes the host report a
    healthy Muninn as unreachable during startup.

    The temp file is staged in the same directory (so ``os.replace`` cannot
    cross a filesystem boundary and fall back to a non-atomic copy) and its mode
    is set *before* the replace. That ordering is the point, and it matches
    store.py's 0600 discipline for the archive: creating the final file first and
    chmodding after leaves a window in which it is world-readable, and this file
    names an address a local process can act on unauthenticated (POSIX) or
    names where the credential to do so lives (Windows).

    The directory is 0700 for the same reason. Note that it is *shared* with
    other ravens owned by the same user, not with other users — one user's
    Huginn has no business reading another user's descriptors.
    """
    directory = state_dir() if directory is None else Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    _restrict(directory, 0o700)

    target = directory / f"{NAME}.json"
    doc = descriptor(address, transport, pages_dir, token_path=token_path,
                     started=started, actions=actions)
    payload = json.dumps(doc, indent=2, sort_keys=True) + "\n"
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
    DACL with. Every file this function is ever called on names an address, a
    directory, or a page render — never a credential — so on Windows each is as
    sensitive as the fact that Muninn is running, which is already visible in
    the task list. The one Windows file that *is* a credential (the named-pipe
    authkey) is deliberately never passed to this function — see
    ravenserve.py's stdlib ctypes ACL helper, which is not a best-effort mode
    bit because this file cannot afford to be.
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

#: Unfinished threads shown in the menu. Deliberately smaller than RECENT_LIMIT:
#: this section is a prompt to act, and a list long enough to scroll is one a
#: reader treats as a backlog to ignore rather than a handful to pick up.
UNFINISHED_LIMIT = 3

#: A session id reaches a URL path, so it is constrained rather than escaped.
#: Covers Claude Code / Codex uuid-shaped ids and export ids alike. Refusing an
#: id that is not a plain token is simpler to be sure of than quoting one, and
#: costs nothing: Muninn's ids are transcript filename stems and export uuids,
#: a shape that is already this narrow.
#:
#: Public rather than a module-private name: ravenserve.py re-checks a session
#: id against this before turning it into a page filename under pages_dir, and
#: that check has to use the exact same pattern build_menu already applied —
#: not a copy that could quietly drift from it.
SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


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
               pending_jobs: int = 0,
               unfinished: list[dict[str, Any]] | None = None,
               unfinished_repo: str | None = None,
               lifecycle: bool = False) -> dict[str, Any]:
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

    3. **Lifecycle** — Quit and Restart, when ``lifecycle`` is set.

    No ``badge``. A badge is a count of things wanting *attention*, and nothing
    in a history archive is waiting on the user — Huginn owns that. Naming the
    mistake: putting the session count in the badge would add Muninn's whole
    corpus size to Huginn's approval count, since the host sums badges across
    ravens, and the menubar would read as thousands of pending decisions.

    ``lifecycle`` defaults to **False** and the daemon is what turns it on. A row
    is only drawn where the click can be honoured: this function is also called
    with no server behind it (the payload is checked against the host's parser
    with no database and no port), and a Quit row in a payload nobody can POST to
    is a row that lies.
    """
    sections: list[dict[str, Any]] = []

    recent_items: list[dict[str, Any]] = []
    for row in recent[:RECENT_LIMIT]:
        label = _session_label(row)
        if not label:
            continue
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
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

    # Before "Recent", because it is the only section here that is *about the
    # user* rather than about the archive. Everything else in this menu reports
    # state; this reports an obligation -- work started in the repository being
    # worked in now, and never finished. Nothing else surfaces it, and nobody
    # goes looking for a thread they have forgotten they left open.
    #
    # Silent when empty rather than saying "nothing unfinished". This section
    # earns its place by being rare, and a permanent reassuring row is how a
    # menu teaches people to stop reading it.
    unfinished_items: list[dict[str, Any]] = []
    for row in (unfinished or [])[:UNFINISHED_LIMIT]:
        label = _session_label(row)
        session_id = row.get("session_id")
        if not label or not isinstance(session_id, str):
            continue
        if not SESSION_ID_RE.fullmatch(session_id):
            continue
        detail_parts = [p for p in (
            safe_label(row.get("outcome"), 16),
            _relative_when(row.get("started_at")),
        ) if p]
        unfinished_items.append({
            "label": label,
            "detail": safe_label(" · ".join(detail_parts), MAX_DETAIL),
            "url": f"/session/{session_id}",
            "style": "muted",
        })
    if unfinished_items:
        where = safe_label(unfinished_repo, 40) if unfinished_repo else None
        sections.append({
            "id": "unfinished",
            "title": f"Unfinished in {where}" if where else "Unfinished",
            "items": unfinished_items,
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

    if lifecycle:
        # Last, because it is the destructive part of the menu and belongs below
        # everything a user opens the menu to read. Restart is a plain row rather
        # than an Option-click alternate the way the native menu-bar apps did it:
        # the host renders labels and has no modifier-key vocabulary to hide one
        # behind, and a menu item nobody can discover is not a replacement for
        # one they could.
        #
        # "muted" for both. These are not warnings and styling them as attention
        # would compete with the one row in Archive that genuinely wants action.
        sections.append({
            "id": "lifecycle",
            "items": [
                {"label": f"Quit {DISPLAY}", "id": QUIT, "style": "muted"},
                {"label": f"Restart {DISPLAY}", "id": RESTART, "style": "muted"},
            ],
        })

    return {
        "api_version": API_VERSION,
        "title": DISPLAY,
        "sections": sections,
    }


def perform_action(daemon: Any, action_id: str) -> tuple[dict[str, Any], Any]:
    """Dispatch one posted action id. Returns ``(reply, followup)``.

    The two-part return is the substance of this function, so it is stated before
    anything else: ``followup`` is a callable the *server* invokes after the reply
    has been written and flushed, or ``None``. Quit cannot happen before the reply
    reaches the host. Roost is holding an open request with a short budget, and a
    connection dropped mid-response is indistinguishable from a wedged raven — so
    a successful quit would render as an action that failed, which is precisely
    the bug Huginn's issue #43 is about one layer down.

    Muninn's stop routes through ``SIGTERM`` to its own pid rather than through a
    shutdown path of its own. That is reuse of the one path this daemon has
    already hardened: ``daemon.install_termination_handlers`` turns that signal
    into ``SystemExit`` on the main thread, which unwinds ``indexer.watch`` and
    lets ``Daemon.run``'s ``finally`` withdraw the descriptor, stop the embedder,
    remove the state file and release the lock — in that order. A hard exit from
    this request thread would skip all four and orphan every one of them.

    An unknown id is reported, not ignored. The host only posts ids Muninn
    published, so an unknown one means the two have drifted, and that is worth
    seeing rather than swallowing.
    """
    if action_id == QUIT or action_id == RESTART:
        restart = action_id == RESTART
        if not daemon.request_stop(restart=restart):
            return {"ok": False, "error": "daemon is not running a stoppable loop"}, None
        return ({"ok": True, "restarting" if restart else "stopping": True},
                daemon.deliver_stop_signal)
    return {"ok": False, "error": "unknown action"}, None


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
