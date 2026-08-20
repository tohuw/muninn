"""The listener that answers ``menu``/``action`` requests, and its lifecycle.

Normative sources: docs/specs/009-raven-descriptor-menu.md and
docs/specs/017-menu-lifecycle-actions.md for the payload and this surface's
prior HTTP shape, docs/specs/010-daemon.md for who runs it, and
docs/specs/021-unix-socket-transport.md for the transport this module now
actually speaks. Read ``muninn/raven.py`` first — it owns the descriptor and
the payload; this module owns the listener and the publish/withdraw lifecycle
around it.

## From a loopback HTTP port to a Unix socket or a named pipe

This surface used to be a TCP listener on ``127.0.0.1``, guarded by ``Host``
and ``Origin`` checks because the actual threat was a web page open in the
user's browser reaching a loopback port no different from any other. Those
checks are gone from this file, not hardened: a Unix domain socket cannot be
opened by anything without filesystem permission on the socket's own inode,
and no browser API can open one at all. The socket's 0600 mode *is* the
security model POSIX gets here — see docs/specs/021 for the full argument
against keeping the HTTP-era checks "just in case."

Windows has no ``AF_UNIX`` in Python's ``socket`` module, so it gets a named
pipe instead, via :mod:`multiprocessing.connection` (``family="AF_PIPE"``).
A named pipe's default security descriptor does not give the same
same-user-only guarantee a 0600 socket file does — this is documented
upstream as the reason ``multiprocessing.connection`` supports an ``authkey``
challenge at all — so the Windows path additionally generates a fresh key
per run, writes it to a file named by the descriptor's ``token_path``, and
locks that one file down with a real per-user ACL rather than a best-effort
mode bit. See :func:`_restrict_to_current_user`.

## ``muninn serve`` runs this, and ``muninn index --watch`` deliberately does not

Spec 009 bolted this onto ``muninn index --watch``, because Muninn had no daemon
and the watcher was the only process that ran for any length of time. Spec 010
gave it one, and the owner decision that closed the question was **not** about the
menubar — it was about ingest, which is Muninn's whole durability claim. The menu
row was always a consequence of something being up, never a reason for it to be.
So ``daemon.Daemon`` calls :func:`attach` and the foreground watcher publishes
nothing: two publishers of one descriptor path means the **loser's** teardown
deletes the **winner's** file, and a healthy raven silently drops out of the
menubar (see daemon.py, "Why there is a single-instance lock").

The consequence, stated plainly rather than hidden: **when the daemon is not
running, Muninn is absent from the menubar.** No descriptor exists, so the host
shows nothing for Muninn — the same as a Muninn that was never installed. If a
descriptor is left behind by a crash, the host checks the recorded PID and
``started`` and renders "Not running (its recorded process is gone)." with the
reason on screen. Both remain legitimate steady states; a raven that lied about
being reachable would be worse.

## The token decision, restated for two transports

**POSIX still advertises no ``token_path``, and the request handler still
takes anything that connects.** The reasoning in docs/specs/009 for why that
is fine — the only mutation available is stopping this process, which
``kill`` already lets any local process owned by this user do — is unchanged
by the transport swap. What changed is *why no browser-reachability argument
is needed to get there*: a 0600 socket file is unreachable from a browser
before the question of a token even comes up, where the HTTP port needed
``Host``/``Origin`` checks specifically to manufacture that same
unreachability. Docs/specs/021 has the full argument.

**Windows is a real exception, not a platform quirk to paper over.** A named
pipe's ACL does not default to same-user-only, so an authkey is the only
thing standing between this process and any other local process on some
Windows configurations. Losing the file that holds it is losing the whole
guarantee, which is why it gets a real ACL instead of the best-effort mode bit
every other file in this protocol uses.

## Every rule the surface follows now

- POSIX binds a Unix domain socket under the shared ravens state directory,
  0600, in a 0700 directory. Never a filesystem path a second local user's
  process could traverse into.
- Windows binds a named pipe and requires the matching authkey on every
  connection; the file naming that key is ACL-restricted to the current user
  by :func:`_restrict_to_current_user`, not by :func:`raven._restrict`'s
  Windows no-op.
- One request per connection: read one length-prefixed message, dispatch,
  write one length-prefixed reply, close. No keep-alive, no pipelining — the
  only client is Roost's bird_client, and it has never asked for either.
- A request names its op in the body (``{"op": "menu"}`` or
  ``{"op": "action", "id": ...}``) because there is no URL space to route on
  here the way there was on HTTP.
- The followup call is still the reason handling an action is not simply
  "handle, then reply". A Quit action stops this process, and it must not do
  so until the reply has been written: the host is holding an open request,
  and a connection that drops before answering reads as a wedged raven rather
  than a successful quit.
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import sys
import tempfile
import threading
from multiprocessing.connection import Listener
from pathlib import Path
from typing import Any, Callable

from . import raven

logger = logging.getLogger("muninn.raven")

#: The cap on one request message, read before anything parses it. It was 512
#: bytes when it bounded an HTTP body holding only ``{"id": "<action>"}``; the
#: cap is unchanged because a ``{"op": "menu"}`` or ``{"op": "action", ...}``
#: request is smaller still, and no legitimate caller comes close to it.
MAX_REQUEST_BODY = 512

#: Fixed rather than derived from a pid. ``daemon.SingleInstance`` already
#: guarantees at most one Muninn daemon runs per user session, so there is
#: exactly one legitimate owner of this name at a time — the same reasoning
#: that already justifies one fixed ``muninn.sock``.
PIPE_NAME = r"\\.\pipe\muninn-raven"


def free_socket_path(directory: Path | None = None) -> Path:
    """Return the path a POSIX listener would bind. Exists only for tests
    that need the address without a listener bound at it."""
    return raven.socket_path(directory)


# ── Windows named-pipe ACL ──────────────────────────────────────────────────
#
# Deliberately not raven._restrict. That function is a best-effort mode bit
# and a documented no-op on Windows, which was the right call for every file
# it protects: a descriptor, a directory, a rendered page — none of them are
# a secret. The pipe authkey is, and "best effort" is not an acceptable
# posture for the one file whose disclosure hands out the whole credential.
#
# UNVERIFIED ON REAL WINDOWS. This is raw ctypes over advapi32/kernel32,
# written and reviewed without access to a Windows machine. It must be
# exercised by Windows CI — or by hand, on Windows — before the token file it
# protects is trusted. Failure is deliberately made to look like any other
# publish failure (see _listen): if this cannot prove the ACL was set, no
# descriptor advertising a token_path is published at all.

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _ACL_REVISION = 2
    _FILE_ALL_ACCESS = 0x1F01FF

    class _SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _TOKEN_USER_STRUCT(ctypes.Structure):
        _fields_ = [("User", _SID_AND_ATTRIBUTES)]

    def _current_user_sid() -> Any:
        """Return a buffer holding the calling process's own SID, or None."""
        htoken = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(
                _kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(htoken)):
            return None
        try:
            size = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(
                htoken, _TOKEN_USER, None, 0, ctypes.byref(size))
            if size.value == 0:
                return None
            buf = (ctypes.c_byte * size.value)()
            if not _advapi32.GetTokenInformation(
                    htoken, _TOKEN_USER, buf, size, ctypes.byref(size)):
                return None
            token_user = ctypes.cast(
                buf, ctypes.POINTER(_TOKEN_USER_STRUCT)).contents
            sid_len = _advapi32.GetLengthSid(token_user.User.Sid)
            if not sid_len:
                return None
            sid_buf = (ctypes.c_byte * sid_len)()
            if not _advapi32.CopySid(sid_len, sid_buf, token_user.User.Sid):
                return None
            return sid_buf
        finally:
            _kernel32.CloseHandle(htoken)

    def _restrict_to_current_user(path: Path) -> bool:
        """Best-effort **result reported honestly**: True only if the ACL is
        now actually current-user-only. See the module-level warning above —
        this is unverified on real Windows, and a False here must cost the
        raven its menu row rather than publish a token nothing protects.
        """
        sid = _current_user_sid()
        if sid is None:
            return False
        sid_len = len(sid)
        # ACL header (8 bytes) + one ACE (header + access mask + SID), with
        # generous slack rather than computing the exact ACE size: this runs
        # once per daemon start, not on a hot path, and slack costs nothing
        # a wrong-by-one-byte computation could not also cost.
        acl_size = 64 + sid_len
        acl_buf = (ctypes.c_byte * acl_size)()
        if not _advapi32.InitializeAcl(acl_buf, acl_size, _ACL_REVISION):
            return False
        if not _advapi32.AddAccessAllowedAce(
                acl_buf, _ACL_REVISION, _FILE_ALL_ACCESS, sid):
            return False
        result = _advapi32.SetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl_buf, None)
        return result == 0  # ERROR_SUCCESS


def _restrict(path: Path, mode: int) -> None:
    """A deliberate duplicate of ``raven._restrict``, not an import of it —
    see daemon.py's comment on the same duplication for why this file does
    not reach across the module boundary for it. A no-op on Windows; nothing
    this function is called on is a credential (see the ACL helper above for
    the one file that is).
    """
    if sys.platform == "win32":
        return
    try:
        path.chmod(mode)
    except OSError:
        pass


# ── Binding ──────────────────────────────────────────────────────────────────

def _listen(directory: Path | None) -> tuple[Listener, str, str, Path | None]:
    """Bind and start listening. Returns (listener, address, transport, token_path).

    ``token_path`` is None on POSIX. On Windows a fresh authkey is generated
    for this run — never reused across a restart — written to
    ``raven.token_path(directory)``, and ACL-restricted before the listener
    is even created, so there is no window in which the key exists
    unprotected.

    Raises ``OSError`` on any failure, including a Windows ACL that could not
    be proven correct, so every caller's existing "no descriptor rather than
    a broken one" handling covers this without a separate branch.
    """
    if sys.platform == "win32":
        token = secrets.token_bytes(32)
        tpath = raven.token_path(directory)
        tpath.parent.mkdir(parents=True, exist_ok=True)
        tpath.write_bytes(token)
        if not _restrict_to_current_user(tpath):
            tpath.unlink(missing_ok=True)
            raise OSError("could not restrict the Windows raven token file "
                          "to the current user")
        listener = Listener(PIPE_NAME, family="AF_PIPE", authkey=token)
        return listener, PIPE_NAME, raven.TRANSPORT_PIPE, tpath

    path = raven.socket_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict(path.parent, 0o700)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    listener = Listener(str(path), family="AF_UNIX")
    _restrict(path, 0o600)
    return listener, str(path), raven.TRANSPORT_UNIX, None


# ── Server ───────────────────────────────────────────────────────────────────

class _Server:
    """A threaded accept loop over a :class:`Listener`.

    ``multiprocessing.connection`` gives a bind/accept primitive that works
    identically over a Unix socket and a Windows named pipe, but no
    ``socketserver``-style dispatch loop on top of it — this is that loop,
    kept deliberately small because it only ever needs to do one thing: hand
    each accepted connection to its own thread and let :meth:`_handle` run
    the whole request/reply/followup sequence there.

    ``menu_provider``/``action_handler`` are per-instance, not module
    globals, for the same reason the old HTTP handler kept them on a
    per-server subclass: two servers in one process — exactly what the tests
    do — must not end up answering from one archive.
    """

    def __init__(self, listener: Listener,
                 menu_provider: Callable[[], dict[str, Any]],
                 action_handler: Callable[[str], tuple[dict[str, Any], Any]] | None
                 ) -> None:
        self._listener = listener
        self.menu_provider = menu_provider
        self.action_handler = action_handler
        self._thread: threading.Thread | None = None

    def start(self) -> "_Server":
        self._thread = threading.Thread(
            target=self._serve_forever, name="muninn-raven", daemon=True)
        self._thread.start()
        return self

    def _serve_forever(self) -> None:
        while True:
            try:
                conn = self._listener.accept()
            except OSError:
                # shutdown() closed the listener out from under us. Not every
                # platform is guaranteed to unblock a pending accept() this
                # way, so this thread is a daemon thread precisely so a
                # platform where it does not unblock cannot outlive the
                # process either.
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: Any) -> None:
        try:
            raw = conn.recv_bytes(maxlength=MAX_REQUEST_BODY)
        except OSError:
            conn.close()
            return
        try:
            reply_bytes, followup = self._dispatch(raw)
        except Exception:
            # A failure building the menu or running an action must produce a
            # reply, not a dropped connection: Roost reports a closed
            # connection as "is not answering", which points the user at the
            # wrong problem entirely. The exception class is logged, never
            # its message — a query error can embed transcript text, which is
            # the same rule receipt.py follows.
            logger.warning("muninn raven: request failed")
            reply_bytes = _encode({"ok": False, "error": "internal error"})
            followup = None
        try:
            conn.send_bytes(reply_bytes)
        except OSError:
            # The host hung up. Still run the followup: the user clicked
            # Quit, and whether they are still listening does not change what
            # they asked for.
            pass
        conn.close()
        if followup is not None:
            followup()

    def _dispatch(self, raw: bytes) -> tuple[bytes, Any]:
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _encode({"ok": False, "error": "body is not JSON"}), None
        if not isinstance(request, dict):
            return _encode({"ok": False, "error": "body is not a JSON object"}), None
        op = request.get("op")
        if op == raven.MENU_OP:
            return _encode({"ok": True, "body": self.menu_provider()}), None
        if op == raven.ACTION_OP:
            return self._dispatch_action(request)
        return _encode({"ok": False, "error": "unknown op"}), None

    def _dispatch_action(self, request: dict[str, Any]) -> tuple[bytes, Any]:
        if self.action_handler is None:
            return _encode({"ok": False, "error": "this raven publishes no actions"}), None
        action_id = request.get("id")
        if not isinstance(action_id, str) or not action_id:
            return _encode({"ok": False, "error": "no action id"}), None
        # Bounded before it is compared or logged, same as the HTTP handler
        # did: an id longer than any Muninn publishes cannot match one.
        action_id = action_id[:raven.MAX_ACTION_ID]
        reply, followup = self.action_handler(action_id)
        return _encode(reply), followup

    def shutdown(self) -> None:
        try:
            self._listener.close()
        except OSError:
            # multiprocessing.connection's SocketListener.close() unlinks the
            # socket path unconditionally and does not itself tolerate the
            # file already being gone (a stale-state cleanup, a deleted
            # ravens/ directory). This is the daemon's teardown path — see
            # Daemon.run's finally, which stops this service *before*
            # releasing the single-instance lock — so an uncaught exception
            # here must not be allowed to skip the rest of that teardown and
            # orphan the lock.
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class RavenService:
    """A bound listener plus the descriptor that advertises it.

    Use as a context manager. The ordering inside is the whole point and is
    easy to get backwards: **bind, then publish; withdraw, then close.**
    Publishing before the bind advertises an address nothing is listening on,
    which the host reports as an unreachable Muninn during startup. Closing
    before withdrawing leaves a descriptor pointing at a dead address for as
    long as the removal takes.
    """

    def __init__(self, server: _Server, descriptor: Path,
                 address: str, transport: str, token_path: Path | None) -> None:
        self.server = server
        self.descriptor = descriptor
        self.address = address
        self.transport = transport
        self._token_path = token_path

    def start(self) -> "RavenService":
        self.server.start()
        return self

    def stop(self) -> None:
        """Withdraw the descriptor and shut the listener down. Idempotent."""
        raven.withdraw(self.descriptor)
        self.server.shutdown()
        if self.transport == raven.TRANSPORT_UNIX:
            Path(self.address).unlink(missing_ok=True)
        if self._token_path is not None:
            self._token_path.unlink(missing_ok=True)

    def __enter__(self) -> "RavenService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def serve(menu_provider: Callable[[], dict[str, Any]], *,
          directory: Path | None = None,
          action_handler: Callable[[str], tuple[dict[str, Any], Any]] | None = None
          ) -> RavenService:
    """Bind a listener, publish the descriptor, and start serving.

    ``menu_provider`` is called per request and must return the payload
    ``raven.build_menu`` produces. It is a callable rather than a fixed payload
    because the menu has to reflect the archive *now* — a payload captured at
    startup would show a session count that stops moving while the indexer that
    serves it keeps importing.

    ``action_handler``, when given, is what makes an ``action`` op dispatch
    instead of answering "publishes no actions", and the descriptor advertises
    the action op only when it is. Those two facts are set from the same
    argument on purpose: a descriptor that promises an action op the server
    refuses would have the host draw a row whose click fails.
    """
    pages = raven.pages_dir(directory)
    pages.mkdir(parents=True, exist_ok=True)
    _restrict(pages, 0o700)

    listener, address, transport, token_path = _listen(directory)
    server = _Server(listener, menu_provider, action_handler)
    try:
        descriptor = raven.publish(
            address, transport, str(pages),
            token_path=str(token_path) if token_path else None,
            directory=directory, actions=action_handler is not None)
    except OSError:
        # A descriptor that cannot be written means Muninn cannot be
        # discovered, so there is no point holding the listener open. Not
        # fatal to the indexer though — see attach(), which is what decides
        # that.
        listener.close()
        if token_path is not None:
            token_path.unlink(missing_ok=True)
        raise
    logger.info("muninn raven: serving over %s (%s), descriptor %s",
                address, transport, descriptor)
    return RavenService(server, descriptor, address, transport, token_path).start()


def menu_provider_for(db_path: str | Path,
                      roots: dict[str, Path] | None = None, *,
                      lifecycle: bool = False,
                      pages_dir: Path | None = None) -> Callable[[], dict[str, Any]]:
    """Return a provider that queries the archive fresh on every menu fetch.

    Opens its own short-lived ``Store`` per request rather than sharing the
    indexer's connection. That is deliberate and the reason is not politeness:
    ``sqlite3`` connections are not safe to use across threads, and this runs on
    a request thread while the indexer is mid-sweep on its own. WAL mode (see
    ``store._connect_and_prepare``) makes a concurrent reader cheap.

    Index lag is *not* recomputed here. ``ingest.index_lag`` stats every
    transcript in every root — thousands of files — and a menu fetch has a
    short budget, so putting it on this path would turn a menu poll into a
    full corpus walk. The queue depth stands in for it: it is one directory
    listing, and a wedged drain is the failure that actually makes the index
    fall behind.

    ``lifecycle`` is passed straight through to ``build_menu``, and the caller
    that sets it is the same one that supplies an action handler — see attach().

    ``pages_dir``, when given, additionally renders every link row's target
    to a static file there before returning the payload — see
    :func:`_write_pages`. Omitted by callers (mainly tests) that build a
    payload with no listener and nowhere for pages to live.
    """
    from . import queue, recall, store

    def provide() -> dict[str, Any]:
        st = store.open_store(db_path)
        try:
            recent = st.log(limit=raven.RECENT_LIMIT)
            # Two indexed queries, and no embeddings. ``recall.recall`` would
            # also offer related-work-from-elsewhere, which loads the whole
            # vector matrix -- fine for a CLI call, far outside the budget
            # this menu fetch has. The unfinished list is the part worth
            # having here anyway: it is the only thing in this menu that asks
            # the user for something rather than reporting state.
            where = recall.current_repo(st)
            unfinished = [r.to_dict() for r in
                          recall.unfinished(st, where, raven.UNFINISHED_LIMIT)]
            payload = raven.build_menu(
                recent=recent,
                sessions=st.count_sessions(),
                chunks=st.count_chunks(),
                last_sweep=st.last_sweep_at(),
                pending_jobs=queue.pending_count(),
                unfinished=unfinished,
                unfinished_repo=where,
                lifecycle=lifecycle,
            )
            if pages_dir is not None:
                _write_pages(pages_dir, payload, st)
            return payload
        finally:
            st.close()

    _ = roots  # accepted for symmetry with the indexer's own signature
    return provide


def attach(db_path: str | Path, *,
           action_handler: Callable[[str], tuple[dict[str, Any], Any]] | None = None
           ) -> RavenService | None:
    """Start the raven surface for a long-running indexer, or return None.

    Returns None rather than raising on any failure to bind or publish. The
    reason is a priority ordering, not laziness: the indexer's job is to not
    lose transcripts, and a menubar section is worth nothing beside that. A
    listener that cannot be bound (already in use, a sandbox with no local
    IPC) or a state directory that cannot be written must cost Muninn its menu
    row, never its ingest. The failure is logged as a warning so it is visible
    rather than silent.

    One argument controls three things — whether the action op routes,
    whether the descriptor advertises it, and whether the menu draws the
    Quit/Restart rows — because all three are the same claim, and any two of
    them disagreeing produces a row that lies about what clicking it will do.
    """
    try:
        return serve(menu_provider_for(db_path,
                                       lifecycle=action_handler is not None,
                                       pages_dir=raven.pages_dir()),
                     action_handler=action_handler)
    except OSError as exc:
        logger.warning("muninn raven: not publishing to the menubar (%s)", type(exc).__name__)
        return None


# ── Page rendering ───────────────────────────────────────────────────────────
#
# The HTTP-era stub deliberately did not render a session's contents: the
# threat was a browser page reaching an unauthenticated network port, and
# "no prose, no transcript text" was this surface's whole answer to that.
# A Unix socket or an ACL-protected named pipe cannot be reached that way at
# all, so docs/specs/021 revisits that decision on the record and this is
# where the reversal actually lives — real session content, rendered to a
# file only the same OS user (POSIX) or the ACL'd token holder (Windows) can
# open in the first place.

def _write_pages(pages_dir: Path, payload: dict[str, Any], st: Any) -> None:
    """Render every link row's target in ``payload`` to a static file.

    Only pages the payload just referenced are written, which keeps this in
    lockstep with what the host is about to be told exists: a ``url`` this
    menu build did not emit has no file waiting for it, and the client-side
    containment check Roost applies treats "no file" the same as "refused."
    """
    pages_dir.mkdir(parents=True, exist_ok=True)
    _restrict(pages_dir, 0o700)
    _write_index_page(pages_dir, payload)
    session_dir = pages_dir / "session"
    session_dir.mkdir(exist_ok=True)
    _restrict(session_dir, 0o700)
    seen: set[str] = set()
    for section in payload.get("sections", []):
        for item in section.get("items", []):
            url = item.get("url")
            if not isinstance(url, str) or not url.startswith("/session/"):
                continue
            session_id = url[len("/session/"):]
            if session_id in seen or not raven.SESSION_ID_RE.fullmatch(session_id):
                continue
            seen.add(session_id)
            _write_session_page(session_dir, session_id, st)


def _write_index_page(pages_dir: Path, payload: dict[str, Any]) -> None:
    """The page behind every ``"url": "/"`` row: the Archive section, restated.

    Deliberately just the Archive section's own labels rather than a second
    query: this stays in sync with the menu by construction, and a summary
    page earning its keep by disagreeing with the menu that links to it would
    be a worse outcome than one that says less.
    """
    lines = ["<p>Muninn — agent history. Use <code>muninn search</code> "
             "to query the archive.</p>"]
    for section in payload.get("sections", []):
        if section.get("id") != "archive":
            continue
        lines.append("<ul>")
        for item in section.get("items", []):
            label = item.get("label")
            if not label:
                continue
            detail = item.get("detail")
            text = f"{label} — {detail}" if detail else label
            lines.append(f"<li>{html.escape(text)}</li>")
        lines.append("</ul>")
    _atomic_write_html(pages_dir / "index.html", _page("Muninn", "\n".join(lines)))


def _write_session_page(session_dir: Path, session_id: str, st: Any) -> None:
    """The page behind a ``"url": "/session/<id>"`` row: the real transcript.

    ``session_id`` is re-checked against ``raven.SESSION_ID_RE`` by the only
    caller, :func:`_write_pages`, before this runs — the same pattern
    ``build_menu`` used to decide whether to emit the row in the first place,
    so a filename derived from it carries no path-traversal risk (the
    pattern cannot match ``..`` or ``/``) independent of anything this
    function does.
    """
    rec = st.get_session(session_id) or {}
    if not rec:
        return
    lines = [f"<p><strong>session:</strong> {html.escape(session_id)}</p>", "<ul>"]
    for key in ("source", "provenance", "cwd", "branch", "model",
                "started_at", "ended_at", "words"):
        value = rec.get(key)
        if value in (None, ""):
            continue
        lines.append(f"<li>{html.escape(key)}: {html.escape(str(value))}</li>")
    lines.append("</ul>")
    if not rec.get("source_present"):
        lines.append("<p><em>the original transcript no longer exists on disk</em></p>")
    lines.append(f"<pre>{html.escape(st.session_text(session_id))}</pre>")
    page = _page(f"Muninn session {session_id[:12]}", "\n".join(lines))
    _atomic_write_html(session_dir / f"{session_id}.html", page)


def _page(title: str, body_html: str) -> str:
    safe_title = html.escape(title)
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{safe_title}</title></head><body><h1>{safe_title}</h1>"
            f"{body_html}</body></html>")


def _atomic_write_html(target: Path, content: str) -> None:
    """Same discipline as ``raven.publish``: stage in the same directory,
    chmod before the replace, never a window in which the file is readable
    by more than intended.
    """
    directory = target.parent
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
