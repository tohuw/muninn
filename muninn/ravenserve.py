"""The loopback listener that serves ``/api/menu``, and its lifecycle.

Normative sources: docs/specs/009-raven-descriptor-menu.md for the payload and
this surface, docs/specs/010-daemon.md for who runs it. Read ``muninn/raven.py``
first — it owns the descriptor and the payload; this module owns the socket and
the publish/withdraw lifecycle around it.

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

Note what did *not* change when the publisher did: same descriptor fields, same
shared directory, same liveness rules, same payload. Only which local process
writes the file, which is the kind of change a self-published-descriptor protocol
is meant to make free — no coordinated release with the host or the other raven.

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

The paragraph above required that this decision be revisited in the same change
that added an action, and the Quit/Restart rows are that change. **Revisited, and
unchanged** — for one reason that is specific to what these actions do:

- The only mutation they cause is *stopping this process*, and any local process
  running as this user can already do that with ``kill``. A token would guard a
  door that has no lock on the wall beside it, and pretending otherwise is worse
  than declining.
- The browser threat is unchanged and is still handled by ``Host`` and ``Origin``,
  not by a credential: a page's ``fetch`` carries an ``Origin`` and is refused
  before the router sees it, and ``<form>`` POSTs cannot set the JSON content the
  handler requires. A token would refuse the same requests for a different reason.

This does **not** generalise. An action that wrote to the archive, spent money at
a provider, or exposed transcript text would not be defensible on either ground,
and would need the token this endpoint still does not have.

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
- ``GET`` is routed always; ``POST`` only when the process running this surface
  supplied an action handler, and only at the one action path. Without one it
  answers 405 rather than succeeding quietly, and the descriptor it publishes
  omits the action endpoint so the host never draws a row to click.
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

#: The only body this surface reads is ``{"id": "<action>"}``, so the cap is a
#: few hundred bytes rather than zero. It was zero — a body refused outright —
#: until the menu gained Quit and Restart, and it stays this small for the same
#: reason it was zero: the cap is what bounds ``rfile.read`` before anything
#: parses, and no legitimate caller comes close to it.
MAX_REQUEST_BODY = 512

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
            self._json(413, {"error": "request body too large"})
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
        """Dispatch one action, or 405 when this server publishes none.

        The guard runs first even though the unrouted case serves nothing:
        answering a cross-origin POST with a route-shaped 405 would confirm what
        this port is to a page that should have been refused before reaching a
        router.

        ``action_handler`` is unset unless a caller wired one, so a bare
        ``ravenserve.serve`` still answers 405 to everything — the pre-existing
        contract, kept because the descriptor it publishes still says the same.

        The followup call is the reason this is not simply "handle, then reply".
        A Quit action stops this process, and it must not do so until the reply is
        on the wire: the host is holding an open request, and a dropped connection
        reads as a wedged raven rather than a successful quit.
        """
        if not self._guard():
            return
        handler = getattr(type(self), "action_handler", None)
        if handler is None:
            self._json(405, {"error": "this raven publishes no actions"})
            return

        path = self.path.partition("?")[0]
        if path != raven.ACTION_ENDPOINT:
            self._json(404, {"error": "not found"})
            return

        try:
            action_id = self._posted_action_id()
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return

        followup = None
        try:
            reply, followup = handler(action_id)
        except Exception:
            # Same rule as do_GET: a response, never a dropped connection, and
            # the exception class only — an action id reaches this log and a
            # message could carry more.
            logger.warning("muninn raven: action failed")
            try:
                self._json(500, {"error": "internal error"})
            except OSError:
                pass
            return

        self._json(200 if reply.get("ok") else 409, reply)
        if followup is not None:
            try:
                self.wfile.flush()
            except OSError:
                # The host hung up. Still run the followup: the user clicked
                # Quit, and whether they are still listening does not change
                # what they asked for.
                pass
            followup()

    def _posted_action_id(self) -> str:
        """Read ``{"id": ...}`` from the request body. Raises ValueError if it isn't.

        The length was already bounded by ``body_length_ok`` in the guard, so this
        reads exactly ``Content-Length`` bytes and never to EOF. Every rejection
        is a ``ValueError`` with a short reason rather than a bare 400: the only
        client is the menu-bar host, and "which part of my POST was wrong" is the
        difference between a one-line fix and a protocol argument.
        """
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            raise ValueError("unreadable Content-Length") from None
        if length <= 0:
            raise ValueError("expected a JSON body")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("body is not JSON") from None
        if not isinstance(payload, dict):
            raise ValueError("body is not a JSON object")
        action_id = payload.get("id")
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("no action id")
        # Bounded before it is compared or logged. An id longer than any Muninn
        # publishes cannot match one, so truncating loses nothing and keeps an
        # unbounded string out of the log line below.
        return action_id[:raven.MAX_ACTION_ID]

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

    def server_bind(self) -> None:
        """Bind without the reverse DNS lookup ``http.server`` does by default.

        ``HTTPServer.server_bind`` calls ``socket.getfqdn(host)`` to fill in
        ``server_name``, which exists for CGI's ``SERVER_NAME`` variable. Nothing
        in this handler reads it, and the lookup is a *blocking reverse DNS
        query* made while the daemon is starting up.

        On macOS that query can take tens of seconds when the resolver has
        nothing useful to say about 127.0.0.1 — which is the state of a CI runner
        and of plenty of laptops on a captive or VPN'd network. The daemon then
        sat silent before its first log line, having bound nothing and published
        nothing: indistinguishable from a hang, because it was one. It is why
        every ``LiveLifecycleTest`` timed out on macOS while Linux passed.

        The host address is a literal we chose; using it directly is both
        correct and instant.
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

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
          directory: Path | None = None,
          action_handler: Callable[[str], tuple[dict[str, Any], Any]] | None = None
          ) -> RavenService:
    """Bind a loopback port, publish the descriptor, and start serving.

    ``menu_provider`` is called per request and must return the payload
    ``raven.build_menu`` produces. It is a callable rather than a fixed payload
    because the menu has to reflect the archive *now* — a payload captured at
    startup would show a session count that stops moving while the indexer that
    serves it keeps importing.

    ``action_handler``, when given, is what makes POST route instead of answering
    405, and the descriptor advertises the action endpoint only when it is. Those
    two facts are set from the same argument on purpose: a descriptor that
    promises an action route the server refuses would have the host draw a row
    whose click fails.

    Binds port 0 and reads back what the OS assigned, rather than picking a free
    port and then binding it: those are two operations with a race between them,
    and losing that race means the descriptor names someone else's port.
    """
    attrs: dict[str, Any] = {"menu_provider": staticmethod(menu_provider)}
    if action_handler is not None:
        attrs["action_handler"] = staticmethod(action_handler)
    handler = type("_MuninnRavenHandler", (_Handler,), attrs)
    server = _Server(("127.0.0.1", 0), handler)
    port = int(server.server_address[1])
    try:
        descriptor = raven.publish(port, directory=directory,
                                   actions=action_handler is not None)
    except OSError:
        # A descriptor that cannot be written means Muninn cannot be discovered,
        # so there is no point holding the port. It is not fatal to the indexer
        # though — see attach(), which is what decides that.
        server.server_close()
        raise
    logger.info("muninn raven: serving http://127.0.0.1:%d, descriptor %s", port, descriptor)
    return RavenService(server, descriptor).start()


def menu_provider_for(db_path: str | Path,
                      roots: dict[str, Path] | None = None, *,
                      lifecycle: bool = False) -> Callable[[], dict[str, Any]]:
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

    ``lifecycle`` is passed straight through to ``build_menu``, and the caller
    that sets it is the same one that supplies an action handler — see attach().
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
                lifecycle=lifecycle,
            )
        finally:
            st.close()

    _ = roots  # accepted for symmetry with the indexer's own signature
    return provide


def attach(db_path: str | Path, *,
           action_handler: Callable[[str], tuple[dict[str, Any], Any]] | None = None
           ) -> RavenService | None:
    """Start the raven surface for a long-running indexer, or return None.

    Returns None rather than raising on any failure to bind or publish. The
    reason is a priority ordering, not laziness: the indexer's job is to not lose
    transcripts, and a menubar section is worth nothing beside that. A port that
    cannot be bound (already in use, a sandbox with no loopback) or a state
    directory that cannot be written must cost Muninn its menu row, never its
    ingest. The failure is logged as a warning so it is visible rather than
    silent.

    One argument controls three things — whether POST routes, whether the
    descriptor advertises the action endpoint, and whether the menu draws the
    Quit/Restart rows — because all three are the same claim, and any two of them
    disagreeing produces a row that lies about what clicking it will do.
    """
    try:
        return serve(menu_provider_for(db_path,
                                       lifecycle=action_handler is not None),
                     action_handler=action_handler)
    except OSError as exc:
        logger.warning("muninn raven: not publishing to the menubar (%s)", type(exc).__name__)
        return None
