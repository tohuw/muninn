"""The loopback listener that serves ``/api/menu``, and its lifecycle.

Normative source: docs/specs/009-raven-descriptor-menu.md. Read
``muninn/raven.py`` first — it owns the descriptor and the payload; this module
owns the socket and the publish/withdraw lifecycle around it.

## Why this is bolted to ``muninn index --watch`` and nothing else

Muninn has no daemon. That is not an oversight to be corrected here: every other
entry point is a one-shot CLI invocation that exits in milliseconds, and the
whole archive design (``muninn/queue.py``, ``muninn/paths.py``) is built so that
even the ``SessionEnd`` hook touches nothing but a directory. The single process
that already runs for as long as the user's machine is up is
``muninn index --watch`` (docs/specs/003-background-indexer.md), so that is where
the descriptor is published and where this server listens.

The consequence, stated plainly rather than hidden: **when the watcher is not
running, Muninn is absent from the menubar.** No descriptor exists, so Appistry
shows nothing for Muninn — the same as a Muninn that was never installed. If a
descriptor is left behind by a crash, Appistry checks the recorded PID and
``started`` and renders "Not running (its recorded process is gone)." with the
reason on screen. Both are legitimate steady states, and the alternative
considered and rejected was inventing a second daemon whose only job is to answer
a menu fetch. That would be a new always-on subsystem, a new lifecycle to get
wrong, and a new loopback port on a machine that did not ask for one — to serve a
menu section. The tradeoff is real and belongs to the project's owner: if Muninn
should be present in the menubar regardless of the indexer, that is a decision to
make deliberately, not something this module should quietly assume by starting a
daemon of its own. See docs/specs/009, "The lifecycle question".

## The token decision, and it is a decision

**Muninn advertises no ``token_path``, so ``/api/menu`` is unauthenticated.**
Appistry documents that a raven publishing no token gets an unauthenticated
request and that whether to accept that is the raven's call (its
``raven_client.py``), so this is a choice being made here, on the record:

- The endpoint is read-only and exposes counts, relative times, and session
  labels — no prose, no transcript text, no paths beyond a basename.
- Any local process that could read a token file at 0600 could equally read
  ``muninn.db`` itself, which is also 0600 and holds the entire corpus. A token
  on this endpoint would defend against nothing that the archive's own file
  permissions do not already decide.
- **What a token would *not* buy is the important part.** The threat a loopback
  port actually faces is a web page in the user's browser reaching it, and a
  token does nothing about that — the page would simply be refused for lacking
  one, which is the same outcome the ``Host`` and ``Origin`` checks already
  produce. Those checks are therefore not optional here; with no credential they
  are the *only* thing between this port and any page the user has open, which is
  the opposite of the intuition that "no secret to steal" means less to defend.

If a future menu row ever carries prose, an action, or anything a caller could
mutate, this decision must be revisited in the same change — not afterwards.

## Every rule the surface follows

- Binds ``127.0.0.1`` only, on an ephemeral port. Never ``0.0.0.0``.
- ``Host`` must name a loopback address, or the request is refused. A page served
  from any other hostname carries that hostname here even when it resolves to
  127.0.0.1, which is what makes this the DNS-rebinding defence rather than a
  formality.
- **Any** ``Origin`` is refused, not just a foreign one. There is no page here for
  a script to be calling, and the only legitimate caller (Appistry's outbound
  client, or the user's own navigation) sends none.
- ``Content-Length`` is guarded before a byte is read, and a negative value is
  refused specifically: passed to ``read()`` it means "until EOF", i.e. no bound
  at all.
- Only ``GET`` is routed. ``POST`` answers 405 rather than succeeding quietly,
  because Muninn publishes no actions and a caller that thinks otherwise should
  find out.
- Response headers are built from a fixed set, never copied from the request, and
  every response carries ``nosniff``. HTML carries a CSP that forbids everything.
"""
from __future__ import annotations

import http.server
import json
import logging
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any, Callable

from . import raven

logger = logging.getLogger("muninn.raven")

#: Accepted in the ``Host`` header. ``::1`` is included because a caller may
#: reach an IPv4 listener through a dual-stack resolver and send the v6 literal.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

#: This surface takes no request bodies at all, so the cap is zero and a body is
#: refused rather than read and ignored.
MAX_REQUEST_BODY = 0

#: Forbids every source. The pages served here are a handful of escaped literals
#: with no assets, so there is nothing to allow.
_CSP = ("default-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'")


def free_port() -> int:
    """Return an unused loopback port.

    There is an unavoidable race between this and the bind that follows, which is
    why :func:`serve` binds to port 0 instead and asks the socket what it got.
    This function exists only for tests that need a port number without a
    listener.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── Request validation ────────────────────────────────────────────────────────

def host_is_loopback(headers: Any) -> bool:
    """True if ``Host`` names a loopback address.

    A missing ``Host`` is False, not True. HTTP/1.1 requires the header, and
    treating its absence as acceptable would hand every check below a bypass that
    costs one omitted line to use.
    """
    raw = headers.get("Host")
    if not raw:
        return False
    host = raw.strip()
    if host.startswith("["):        # bracketed IPv6 literal
        host = host[1:].partition("]")[0]
    else:
        host = host.partition(":")[0]
    return host.casefold() in _LOOPBACK_HOSTS


def has_origin(headers: Any) -> bool:
    """True if the request carries any ``Origin`` header at all.

    Deliberately not "an ``Origin`` that is not ours". Allowlisting
    ``http://127.0.0.1:{port}`` would mean a page served *from this very port*
    could script the endpoint — and since the port is ephemeral and published in
    a file, "our own origin" is not a meaningful trust boundary here anyway.
    """
    return headers.get("Origin") is not None


def body_length_ok(headers: Any) -> bool:
    """False if ``Content-Length`` is unparseable, negative, or over the cap.

    A negative length is the case worth naming: it is not merely invalid, it is
    dangerous, because ``read(-1)`` reads until EOF and so imposes no bound at
    all. It is refused here, before anything reads.
    """
    raw = headers.get("Content-Length")
    if raw is None:
        return True
    try:
        length = int(raw)
    except ValueError:
        return False
    return 0 <= length <= MAX_REQUEST_BODY


# ── Handler ───────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    """Muninn's raven API. Four routes and no state.

    ``menu_provider`` is set by :func:`serve` on a per-server subclass rather
    than read from a module global, so two servers in one process (which is
    exactly what the tests do) cannot end up sharing one archive.
    """

    protocol_version = "HTTP/1.1"
    menu_provider: Callable[[], dict[str, Any]] = staticmethod(
        lambda: raven.build_menu(recent=[], sessions=0, chunks=0))

    def log_message(self, *_args: object) -> None:
        """Silence the access log.

        stderr on a background indexer's console is the user's log, and a line
        per menu poll (Appistry refreshes on a timer) would bury the import
        receipts that console exists to show.
        """

    def _guard(self) -> bool:
        if not host_is_loopback(self.headers):
            self._json(400, {"error": "unexpected host"})
            return False
        if has_origin(self.headers):
            self._json(403, {"error": "cross-origin request rejected"})
            return False
        if not body_length_ok(self.headers):
            self._json(413, {"error": "this endpoint accepts no request body"})
            return False
        return True

    def do_GET(self) -> None:      # noqa: N802 - BaseHTTPRequestHandler's spelling
        if not self._guard():
            return
        path = self.path.partition("?")[0]
        try:
            if path == "/api/menu":
                self._json(200, type(self).menu_provider())
            elif path in ("/", ""):
                self._html(_page("Muninn", "Muninn — agent history. "
                                           "Use <code>muninn search</code> to query the archive."))
            elif path.startswith("/session/"):
                self._session_page(path[len("/session/"):])
            else:
                self._json(404, {"error": "not found"})
        except Exception:
            # A failure building the menu must produce a response, not a dropped
            # connection: Appistry reports a closed connection as "Is not
            # answering on its recorded port", which points the user at the
            # wrong problem entirely. The exception class is logged, never its
            # message — a query error can embed transcript text, which is the
            # same rule receipt.py follows.
            logger.warning("muninn raven: request failed")
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass

    def do_POST(self) -> None:     # noqa: N802 - BaseHTTPRequestHandler's spelling
        """405, because Muninn publishes no actions.

        The guard runs first even though nothing is served: answering a
        cross-origin POST with a route-shaped 405 would confirm what this port is
        to a page that should have been refused before reaching a router.
        """
        if not self._guard():
            return
        self._json(405, {"error": "this raven publishes no actions"})

    def _session_page(self, session_id: str) -> None:
        """A stub page for a menu link's target.

        Deliberately does not render the session. A menu row's job is to get the
        user to Muninn, and Muninn's actual interface is the CLI; serving prose
        over this port would put transcript text on an unauthenticated endpoint,
        which is the one thing the token decision in this module's docstring
        depends on not happening. The id is echoed escaped so the user can paste
        it into ``muninn show``.
        """
        import html

        clean = raven.safe_label(session_id, 128)
        if not clean:
            self._json(404, {"error": "not found"})
            return
        self._html(_page(
            "Muninn session",
            f"<code>muninn show {html.escape(clean)}</code>",
        ))

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._respond(status, json.dumps(payload).encode("utf-8"), "application/json")

    def _html(self, page: str) -> None:
        self._respond(200, page.encode("utf-8"), "text/html; charset=utf-8",
                      extra=(("Content-Security-Policy", _CSP),))

    def _respond(self, status: int, body: bytes, content_type: str,
                 extra: tuple[tuple[str, str], ...] = ()) -> None:
        """Write one response from a fixed header set.

        Nothing here is copied from the request. That is the property worth
        stating rather than inferring: a handler that echoes any inbound header
        is one refactor away from reflecting an attacker-chosen value, and there
        is no reason this endpoint ever needs to.
        """
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _page(title: str, body_html: str) -> str:
    import html

    safe_title = html.escape(title)
    return (f"<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{safe_title}</title></head><body><h1>{safe_title}</h1>"
            f"<p>{body_html}</p></body></html>")


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Threaded so one slow request cannot stall Appistry's next poll.

    ``daemon_threads`` is on so a wedged request thread cannot keep the indexer
    process alive after the user has asked it to stop.
    """

    allow_reuse_address = True
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        """Log a dropped connection, never traceback it to stderr.

        ``socketserver``'s default prints a full traceback, and the commonest
        cause here is entirely routine: this handler speaks HTTP/1.1 with
        keep-alive, so every client that finishes and closes its socket leaves a
        thread blocked on a read that ends in ``ConnectionResetError``. The
        default behaviour would scatter tracebacks across the indexer's console —
        the console whose whole purpose is showing one line per import — and
        train the user to ignore it.
        """
        logger.debug("muninn raven: connection from %s dropped", client_address, exc_info=True)


# ── Lifecycle ─────────────────────────────────────────────────────────────────

class RavenService:
    """A bound listener plus the descriptor that advertises it.

    Use as a context manager. The ordering inside is the whole point and is easy
    to get backwards: **bind, then publish; withdraw, then close.** Publishing
    before the bind advertises a port nothing is listening on, which the host
    reports as an unreachable Muninn during startup. Closing before withdrawing
    leaves a descriptor pointing at a dead port for as long as the removal takes.
    """

    def __init__(self, server: _Server, descriptor: Path) -> None:
        self.server = server
        self.descriptor = descriptor
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def start(self) -> "RavenService":
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="muninn-raven", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Withdraw the descriptor and shut the listener down. Idempotent.

        ``server_close`` is not optional: without it the listening socket stays
        bound, and an in-process restart (a test, or a watcher that re-publishes
        after a config change) hits "address already in use" and silently drops
        Muninn out of the menubar.
        """
        raven.withdraw(self.descriptor)
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> "RavenService":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def serve(menu_provider: Callable[[], dict[str, Any]], *,
          directory: Path | None = None) -> RavenService:
    """Bind a loopback port, publish the descriptor, and start serving.

    ``menu_provider`` is called per request and must return the payload
    ``raven.build_menu`` produces. It is a callable rather than a fixed payload
    because the menu has to reflect the archive *now* — a payload captured at
    startup would show a session count that stops moving while the indexer that
    serves it keeps importing.

    Binds port 0 and reads back what the OS assigned, rather than picking a free
    port and then binding it: those are two operations with a race between them,
    and losing that race means the descriptor names someone else's port.
    """
    handler = type("_MuninnRavenHandler", (_Handler,),
                   {"menu_provider": staticmethod(menu_provider)})
    server = _Server(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    try:
        descriptor = raven.publish(port, directory=directory)
    except OSError:
        # A descriptor that cannot be written means Muninn cannot be discovered,
        # so there is no point holding the port. It is not fatal to the indexer
        # though — see attach(), which is what decides that.
        server.server_close()
        raise
    logger.info("muninn raven: serving http://127.0.0.1:%d, descriptor %s", port, descriptor)
    return RavenService(server, descriptor).start()


def menu_provider_for(db_path: str | Path,
                      roots: dict[str, Path] | None = None) -> Callable[[], dict[str, Any]]:
    """Return a provider that queries the archive fresh on every menu fetch.

    Opens its own short-lived ``Store`` per request rather than sharing the
    indexer's connection. That is deliberate and the reason is not politeness:
    ``sqlite3`` connections are not safe to use across threads, and this runs on
    a request thread while the indexer is mid-sweep on its own. WAL mode (see
    ``store._connect_and_prepare``) makes a concurrent reader cheap.

    Index lag is *not* recomputed here. ``ingest.index_lag`` stats every
    transcript in every root — thousands of files — and Appistry's menu fetch has
    a two-second budget, so putting it on this path would turn a menu poll into a
    full corpus walk and time the raven out. The queue depth stands in for it: it
    is one directory listing, and a wedged drain is the failure that actually
    makes the index fall behind.
    """
    from . import queue, store

    def provide() -> dict[str, Any]:
        st = store.open_store(db_path)
        try:
            recent = st.log(limit=raven.RECENT_LIMIT)
            return raven.build_menu(
                recent=recent,
                sessions=st.count_sessions(),
                chunks=st.count_chunks(),
                last_sweep=st.last_sweep_at(),
                pending_jobs=queue.pending_count(),
            )
        finally:
            st.close()

    _ = roots  # accepted for symmetry with the indexer's own signature
    return provide


def attach(db_path: str | Path) -> RavenService | None:
    """Start the raven surface for a long-running indexer, or return None.

    Returns None rather than raising on any failure to bind or publish. The
    reason is a priority ordering, not laziness: the indexer's job is to not lose
    transcripts, and a menubar section is worth nothing beside that. A port that
    cannot be bound (already in use, a sandbox with no loopback) or a state
    directory that cannot be written must cost Muninn its menu row, never its
    ingest. The failure is logged as a warning so it is visible rather than
    silent.
    """
    try:
        return serve(menu_provider_for(db_path))
    except OSError as exc:
        logger.warning("muninn raven: not publishing to the menubar (%s)", type(exc).__name__)
        return None
