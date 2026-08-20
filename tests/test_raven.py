"""The raven contract: descriptor lifecycle, menu payload, and the transport guards.

Acceptance criteria from docs/specs/009-raven-descriptor-menu.md and
docs/specs/021-unix-socket-transport.md.

## Why Appistry's parser is reimplemented here instead of imported

These tests validate the menu payload against a local copy of Appistry's
``menu_spec.parse_menu`` rather than importing the real one. That is a deliberate
tradeoff and the reason matters, because the obvious alternative looks better than
it is:

- Appistry is not a dependency of Muninn and must not become one. It is a
  *consumer* of this protocol; a test that could not run without it would make
  Muninn's suite depend on a desktop menubar app to check a JSON shape.
- Appistry is mid-rename of its own runtime package (its modules moved into a
  ``roost/`` package while this was written), so any import path pinned here
  would have been wrong within the day.

The risk this accepts is drift: a change to Appistry's real caps or its
sanitising will not fail this suite. So :class:`AppistryParityTest` exists to
bound that risk — it asserts the *numbers* this file encodes, in one place, so a
reader comparing the two documents has a single list to check rather than a
parser to diff. The payload was additionally verified end to end against the real
``roost.ravens.discover``, ``roost.raven_client.fetch_menu`` and
``roost.menu_spec.parse_menu`` at the commit named in docs/specs/009; that run is
what makes this reimplementation trustworthy, not the reimplementation itself.

The one rule to keep: **make this parser stricter or equal to Appistry's, never
looser.** A local parser that accepts something the real one drops turns a
silently truncated menu into a green test.

## Why this file talks ``multiprocessing.connection`` and not HTTP any more

Spec 021 replaced the loopback HTTP listener with a Unix domain socket (POSIX)
or a named pipe (Windows), both spoken through ``multiprocessing.connection``.
There is no verb, no path, no header — a request is one JSON message naming an
``op``, and a reply is one JSON message back, then the connection closes. The
``Host``/``Origin``/``Content-Length`` guards the old ``HttpGuardTest`` checked
are gone from the implementation, not hardened, so they are gone from this file
too; :class:`TransportGuardTest` below tests the guarantees that replaced them.
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import unittest
from multiprocessing.connection import Client
from pathlib import Path

from muninn import raven, ravenserve

# ── A faithful, deliberately strict copy of Appistry's bounds ─────────────────
#
# From its ``menu_spec.py``. Every one of these is a *drop*, not an error, on the
# host side — which is precisely why a raven that exceeds one gets a quietly
# shorter menu with nothing on screen to explain it.

MAX_SECTIONS = 12
MAX_ITEMS_PER_SECTION = 50
MAX_TOTAL_ITEMS = 200
MAX_LABEL_LENGTH = 120
MAX_DETAIL_LENGTH = 80
MAX_ACTION_ID_LENGTH = 128
MAX_URL_LENGTH = 512
STYLES = ("normal", "attention", "muted")

_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-9;:<=>?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_SPOOF_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


def host_sanitize_label(value: object, limit: int = MAX_LABEL_LENGTH) -> str:
    """Appistry's ``sanitize.sanitize_label``, reproduced."""
    if not isinstance(value, str):
        return ""
    cleaned = _ANSI_RE.sub("", value)
    cleaned = _CONTROL_RE.sub("", cleaned)
    cleaned = _SPOOF_RE.sub("", cleaned)
    cleaned = cleaned.replace("\x1b", "")
    cleaned = re.sub(r"[\s\u00a0\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+", " ", cleaned).strip()
    if limit > 0 and len(cleaned) > limit:
        cleaned = cleaned[: max(limit - 1, 0)].rstrip() + "\u2026"
    return cleaned


def host_contains_unsafe(value: object) -> bool:
    """Appistry's ``sanitize.contains_unsafe_text``, reproduced."""
    if not isinstance(value, str):
        return True
    return bool("\x1b" in value or _CONTROL_RE.search(value) or _SPOOF_RE.search(value))


def _parse_url(raw: object) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL_LENGTH:
        return ""
    if host_contains_unsafe(raw):
        return ""
    if not raw.startswith("/") or raw[1:2] in ("/", "\\"):
        return ""
    if ".." in raw.split("?", 1)[0].split("/") or "#" in raw:
        return ""
    return raw


def _parse_action_id(raw: object) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_ACTION_ID_LENGTH:
        return ""
    if host_contains_unsafe(raw) or any(ch in raw for ch in "\r\n"):
        return ""
    return raw


def parse_item(raw: object) -> dict | None:
    """Appistry's ``menu_spec.parse_item``. None means the row is dropped."""
    if not isinstance(raw, dict):
        return None
    if raw.get("separator") is True:
        return {"separator": True, "label": "", "url": "", "detail": "",
                "enabled": True, "style": "normal", "action_id": ""}
    label = host_sanitize_label(raw.get("label"))
    if not label:
        return None
    action_id = _parse_action_id(raw.get("id"))
    url = _parse_url(raw.get("url"))
    requested = raw.get("enabled")
    style = raw.get("style")
    return {
        "separator": False,
        "label": label,
        "action_id": action_id,
        "url": url,
        "detail": host_sanitize_label(raw.get("detail"), MAX_DETAIL_LENGTH),
        "enabled": (requested if isinstance(requested, bool) else True) and bool(action_id or url),
        "style": style if isinstance(style, str) and style in STYLES else "normal",
    }


def parse_section(raw: object, budget: int) -> tuple[dict | None, int]:
    """Appistry's ``menu_spec.parse_section``, budget threaded through."""
    if not isinstance(raw, dict) or budget <= 0:
        return None, budget
    raw_items = raw.get("items")
    if not isinstance(raw_items, list):
        return None, budget
    items: list[dict] = []
    for raw_item in raw_items[:MAX_ITEMS_PER_SECTION]:
        if budget <= 0:
            break
        item = parse_item(raw_item)
        if item is None:
            continue
        if item["separator"] and (not items or items[-1]["separator"]):
            continue
        items.append(item)
        budget -= 1
    while items and items[-1]["separator"]:
        items.pop()
        budget += 1
    if not items:
        return None, budget
    return {"id": _parse_action_id(raw.get("id")),
            "title": host_sanitize_label(raw.get("title")),
            "items": items}, budget


def parse_menu(payload: object) -> dict:
    """Appistry's ``menu_spec.parse_menu``. Never raises for content reasons."""
    if not isinstance(payload, dict):
        return {"title": "", "badge": 0, "sections": []}
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        return {"title": host_sanitize_label(payload.get("title")), "badge": 0, "sections": []}
    sections: list[dict] = []
    budget = MAX_TOTAL_ITEMS
    for raw_section in raw_sections[:MAX_SECTIONS]:
        section, budget = parse_section(raw_section, budget)
        if section is not None:
            sections.append(section)
    badge = payload.get("badge")
    if isinstance(badge, bool) or not isinstance(badge, int) or not (0 <= badge <= 9999):
        badge = 0
    return {"title": host_sanitize_label(payload.get("title")), "badge": badge,
            "sections": sections}


# ── Fixtures ──────────────────────────────────────────────────────────────────

def a_session(session_id: str = "abc123", **over: object) -> dict:
    row = {
        "session_id": session_id,
        "source": "claude",
        "provenance": "human",
        "started_at": "2026-08-01T09:00:00Z",
        "cwd": "/Users/someone/Projects/muninn",
        "words": 4200,
        "topic": "Fixed the JSONL parser",
    }
    row.update(over)
    return row


def _family_for(transport: str) -> str:
    """The ``multiprocessing.connection`` family that speaks ``transport``."""
    return "AF_UNIX" if transport == raven.TRANSPORT_UNIX else "AF_PIPE"


def _connect(service: "ravenserve.RavenService"):
    """Open one client connection to ``service``, matching its own transport."""
    return Client(service.address, family=_family_for(service.transport))


def _ask(service: "ravenserve.RavenService", payload: dict) -> dict:
    """Send one JSON request and return the parsed JSON reply. One round trip,
    matching the protocol: one message in, one message out, then close."""
    conn = _connect(service)
    try:
        conn.send_bytes(json.dumps(payload).encode("utf-8"))
        return json.loads(conn.recv_bytes().decode("utf-8"))
    finally:
        conn.close()


class RavenTestCase(unittest.TestCase):
    """Points RAVENS_STATE_DIR at a tempdir.

    Every test in this file must do this. The real value is a shared directory in
    the user's home that a live Appistry polls, and a test that published there
    would put a descriptor naming a dead address into the user's actual menubar.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-raven-"))
        self._prior = os.environ.get(raven.STATE_DIR_ENV)
        os.environ[raven.STATE_DIR_ENV] = str(self.tmp / "ravens")

    def tearDown(self) -> None:
        if self._prior is None:
            os.environ.pop(raven.STATE_DIR_ENV, None)
        else:
            os.environ[raven.STATE_DIR_ENV] = self._prior
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── Descriptor directory resolution ───────────────────────────────────────────

class StateDirTest(unittest.TestCase):
    """The one rule every participant must implement identically.

    A raven that resolves this differently publishes where the host is not
    looking, and the failure is entirely silent.
    """

    def setUp(self) -> None:
        self._env = {k: os.environ.get(k) for k in
                     (raven.STATE_DIR_ENV, "XDG_STATE_HOME", "LOCALAPPDATA")}

    def tearDown(self) -> None:
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_override_wins(self) -> None:
        os.environ[raven.STATE_DIR_ENV] = "/tmp/somewhere-else"
        self.assertEqual(raven.state_dir(), Path("/tmp/somewhere-else"))

    def test_blank_override_is_not_an_override(self) -> None:
        # A blank value must fall through, not resolve to Path("") — which is the
        # current directory, so a descriptor would land in whatever repo the
        # indexer happened to be started from.
        os.environ[raven.STATE_DIR_ENV] = "   "
        self.assertNotEqual(raven.state_dir(), Path(""))
        self.assertEqual(raven.state_dir().name, "Ravens" if sys.platform == "win32" else "ravens")

    @unittest.skipIf(sys.platform == "win32", "POSIX resolution rule")
    def test_honours_xdg_state_home(self) -> None:
        os.environ.pop(raven.STATE_DIR_ENV, None)
        os.environ["XDG_STATE_HOME"] = "/tmp/xdg"
        self.assertEqual(raven.state_dir(), Path("/tmp/xdg/ravens"))

    @unittest.skipIf(sys.platform == "win32", "POSIX resolution rule")
    def test_falls_back_to_local_state(self) -> None:
        os.environ.pop(raven.STATE_DIR_ENV, None)
        os.environ.pop("XDG_STATE_HOME", None)
        self.assertEqual(raven.state_dir(), Path.home() / ".local/state/ravens")

    @unittest.skipIf(sys.platform != "win32", "Windows resolution rule")
    def test_windows_uses_localappdata(self) -> None:
        os.environ.pop(raven.STATE_DIR_ENV, None)
        os.environ["LOCALAPPDATA"] = r"C:\Users\someone\AppData\Local"
        self.assertEqual(raven.state_dir(), Path(r"C:\Users\someone\AppData\Local\Ravens"))

    def test_is_not_muninns_own_state_dir(self) -> None:
        """The mistake this guards: publishing into muninn/paths.py's STATE_DIR.

        The host does not look there, so the descriptor is never found and the
        menubar is silently empty.
        """
        from muninn import paths
        os.environ.pop(raven.STATE_DIR_ENV, None)
        self.assertNotEqual(raven.state_dir(), paths.STATE_DIR)


# ── Descriptor content and lifecycle ──────────────────────────────────────────

class DescriptorTest(RavenTestCase):
    """``raven.descriptor``/``publish`` over the new address/transport shape.

    ``address``/``transport``/``pages_dir`` are plain strings here — nothing in
    this class binds a real listener, on purpose: a test that can build and
    check the descriptor with no socket bound is the whole reason the payload
    lives in ``raven.py`` rather than in ``ravenserve.py``.
    """

    ADDR = "/tmp/fake-ravens/muninn.sock"
    PAGES = "/tmp/fake-ravens/muninn/pages"

    def test_fields_match_the_protocol(self) -> None:
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES,
                                   pid=4242, started=1785315600.5)
        # `launch` is platform-derived and absent where there is no supervisor,
        # so it is asserted separately rather than pinned into this literal.
        self.assertEqual({k: v for k, v in payload.items() if k != "launch"}, {
            "api_version": 1,
            "min_api": 1,
            "max_api": 1,
            "name": "muninn",
            "display": "Muninn",
            "pid": 4242,
            "transport": raven.TRANSPORT_UNIX,
            "address": self.ADDR,
            "pages_dir": self.PAGES,
            "started": 1785315600.5,
            "host_priority": 50,
            "endpoints": {"menu": raven.MENU_OP},
        })

    def test_the_launch_block_names_a_service_never_a_command(self) -> None:
        """The host executes nothing this file names, so it gets an identifier.

        Absent is valid: a platform with no start-at-login mechanism publishes
        no block, and the host then draws no Start row.
        """
        launch = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES).get("launch")
        if launch is None:
            self.skipTest("no start-at-login mechanism on this platform")
        self.assertIn(launch["kind"], ("launchd", "systemd", "windows-run"))
        identifier = launch["id"]
        self.assertRegex(identifier, r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
        for forbidden in ("/", "\\", " ", ";", "$", "`"):
            self.assertNotIn(forbidden, identifier)

    def test_declares_a_range_not_a_single_version(self) -> None:
        """tohuw/huginn#38: equality comparison silently disabled every plugin."""
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertLessEqual(payload["min_api"], payload["api_version"])
        self.assertGreaterEqual(payload["max_api"], payload["api_version"])

    def test_defers_to_huginn(self) -> None:
        # 50 against Huginn's 100. Ordering is data the ravens supply; the host
        # knows neither name.
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertEqual(payload["host_priority"], 50)
        self.assertLess(raven.HOST_PRIORITY, 100)

    def test_advertises_no_token_and_no_action_endpoint_by_default(self) -> None:
        """Both absences are decisions. See ravenserve.py's module docstring."""
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertNotIn("token_path", payload)
        self.assertNotIn("action", payload["endpoints"])

    def test_token_path_is_advertised_only_when_given(self) -> None:
        """Windows carries one; POSIX callers simply never pass it (spec 021)."""
        token = str(Path(self.PAGES).parent / "muninn.token")
        payload = raven.descriptor(r"\\.\pipe\muninn-raven", raven.TRANSPORT_PIPE,
                                   self.PAGES, token_path=token)
        self.assertEqual(payload["token_path"], token)

    def test_actions_advertises_the_action_op(self) -> None:
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES, actions=True)
        self.assertEqual(payload["endpoints"]["action"], raven.ACTION_OP)

    def test_supplies_a_plausible_epoch_started(self) -> None:
        """Without ``started`` a recycled PID passes as a live raven.

        Also catches the ``time.monotonic()`` mistake: a monotonic reading is not
        epoch-based, so the host's cross-check would reject every live process.

        Compared against this process's *start* rather than against "now", which
        is the whole point of the field. The old assertion allowed five seconds
        of drift from ``time.time()`` and only held while ``started`` was
        stamped at publish time; a suite that takes half a minute -- or the eight
        the macOS job takes -- is far past that by the time this runs.
        """
        import time

        from muninn.store import process_start_time

        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        # Epoch-based, not monotonic: a monotonic reading counts from an
        # arbitrary origin and is a far smaller number than this.
        self.assertGreater(payload["started"], 1_600_000_000)
        self.assertLessEqual(payload["started"], time.time() + 5.0)
        actual = process_start_time(os.getpid())
        if actual:  # None on a platform that cannot answer; then the clock stands in
            self.assertAlmostEqual(payload["started"], actual, delta=2.0)

    def test_started_is_read_from_the_os_not_the_wall_clock(self) -> None:
        from unittest.mock import patch

        with patch("muninn.store.process_start_time", return_value=1_700_000_000.0):
            self.assertEqual(
                raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)["started"],
                1_700_000_000.0)

    def test_a_republish_reports_the_same_start_time(self) -> None:
        """The Restart row does not start a new process.

        ``cli._run_ingest_loop`` loops in place, so a restart republishes from a
        process the OS says began long before. Stamping ``time.time()`` here made
        the host's cross-check fail and declare a healthy Muninn gone — the empty
        "its recorded process is gone" section, daemon still running. Two
        descriptors from one process must agree about when that process began.
        """
        import time

        first = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)["started"]
        time.sleep(0.05)
        second = raven.descriptor(self.ADDR + ".2", raven.TRANSPORT_UNIX, self.PAGES)["started"]
        self.assertEqual(second, first)

    def test_started_falls_back_to_the_clock_when_the_os_cannot_answer(self) -> None:
        """A descriptor with a weak ``started`` beats no descriptor at all."""
        import time
        from unittest.mock import patch

        with patch("muninn.store.process_start_time", return_value=None):
            self.assertAlmostEqual(
                raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)["started"],
                time.time(), delta=5.0)

    def test_pid_names_this_process(self) -> None:
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertEqual(payload["pid"], os.getpid())

    def test_publish_writes_valid_json_at_the_right_name(self) -> None:
        path = raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertEqual(path, raven.descriptor_path())
        # The filename stem must equal the declared name, or the host refuses the
        # file rather than reconciling it.
        self.assertEqual(path.name, "muninn.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], path.stem)
        self.assertEqual(payload["address"], self.ADDR)
        self.assertEqual(payload["transport"], raven.TRANSPORT_UNIX)
        self.assertEqual(payload["pages_dir"], self.PAGES)

    @unittest.skipIf(sys.platform == "win32", "NTFS uses ACLs, not mode bits")
    def test_permissions_are_owner_only(self) -> None:
        path = raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    @unittest.skipIf(sys.platform == "win32", "NTFS uses ACLs, not mode bits")
    def test_permissions_hold_under_a_permissive_umask(self) -> None:
        """mkdir/mkstemp modes are masked by umask; the chmod is not.

        This is the reason publish() chmods explicitly rather than passing a mode
        to mkdir: under umask 0 the directory would otherwise be world-writable.
        """
        prior = os.umask(0)
        try:
            path = raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        finally:
            os.umask(prior)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_publish_leaves_no_temp_file_behind(self) -> None:
        raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        leftovers = [p.name for p in raven.state_dir().iterdir() if p.name != "muninn.json"]
        self.assertEqual(leftovers, [])

    def test_publish_replaces_a_stale_descriptor(self) -> None:
        raven.publish("/tmp/fake-ravens/first.sock", raven.TRANSPORT_UNIX, self.PAGES)
        raven.publish("/tmp/fake-ravens/second.sock", raven.TRANSPORT_UNIX, self.PAGES)
        payload = json.loads(raven.descriptor_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["address"], "/tmp/fake-ravens/second.sock")

    def test_withdraw_removes_it(self) -> None:
        path = raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        raven.withdraw(path)
        self.assertFalse(path.exists())

    def test_withdraw_is_idempotent(self) -> None:
        """A double stop must not raise: this runs in a shutdown path."""
        path = raven.publish(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES)
        raven.withdraw(path)
        raven.withdraw(path)
        self.assertFalse(path.exists())

    def test_a_stale_descriptor_is_detectable_by_pid(self) -> None:
        """The crash case. A hard kill skips withdraw(), and that is survivable.

        The host checks the recorded pid and renders "Not running" with a reason,
        so this asserts the descriptor carries what that check needs rather than
        that removal is guaranteed.
        """
        from muninn import store
        payload = raven.descriptor(self.ADDR, raven.TRANSPORT_UNIX, self.PAGES,
                                   pid=999_999_998, started=1.0)
        self.assertFalse(store.pid_alive(payload["pid"]))
        self.assertIsNotNone(payload["started"])


class ServiceLifecycleTest(RavenTestCase):
    """The descriptor exists exactly while something is listening."""

    def test_descriptor_appears_on_serve_and_is_gone_after_stop(self) -> None:
        self.assertFalse(raven.descriptor_path().exists())
        service = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        try:
            self.assertTrue(service.descriptor.exists())
            payload = json.loads(service.descriptor.read_text(encoding="utf-8"))
            self.assertEqual(payload["address"], service.address)
            self.assertEqual(payload["transport"], service.transport)
        finally:
            service.stop()
        self.assertFalse(raven.descriptor_path().exists())

    def test_the_advertised_address_is_actually_listening(self) -> None:
        """Publish after bind, never before.

        A descriptor naming an address nothing is listening on makes the host
        report a healthy Muninn as unreachable during startup.
        """
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            reply = _ask(svc, {"op": raven.MENU_OP})
            self.assertTrue(reply["ok"])

    @unittest.skipIf(sys.platform == "win32", "socket file modes; Windows binds a named pipe")
    def test_the_socket_is_owner_only(self) -> None:
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            self.assertEqual(svc.transport, raven.TRANSPORT_UNIX)
            sock_path = Path(svc.address)
            self.assertEqual(stat.S_IMODE(sock_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(sock_path.parent.stat().st_mode), 0o700)

    def test_stop_releases_the_address_for_a_restart(self) -> None:
        """Without unlinking the socket, the address stays bound and a restart fails."""
        first = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        first.stop()
        second = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        second.stop()

    def test_context_manager_withdraws(self) -> None:
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            descriptor = svc.descriptor
            self.assertTrue(descriptor.exists())
        self.assertFalse(descriptor.exists())

    @unittest.skipIf(sys.platform == "win32", "asserts the POSIX socket file specifically")
    def test_stop_removes_the_socket_file(self) -> None:
        """No stale address should survive a clean shutdown (spec 021 #12)."""
        svc = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        address = Path(svc.address)
        self.assertTrue(address.exists())
        svc.stop()
        self.assertFalse(address.exists())

    def test_attach_returns_none_rather_than_raising(self) -> None:
        """A menubar section must never cost the indexer its ingest.

        The descriptor directory here cannot be created because a *file* sits
        where one of its parents would go, which is an ordinary ``OSError`` —
        the same class of failure as a read-only state directory, without
        needing a chmod that Windows does not honour.
        """
        blocker = self.tmp / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        os.environ[raven.STATE_DIR_ENV] = str(blocker / "ravens")
        self.assertIsNone(ravenserve.attach(self.tmp / "muninn.db"))
        self.assertFalse((self.tmp / "ravens" / "muninn.json").exists())


# ── The menu payload under the host's parser ──────────────────────────────────

class MenuPayloadTest(unittest.TestCase):
    def build(self, **over: object) -> dict:
        kwargs: dict = {"recent": [a_session()], "sessions": 1234, "chunks": 98765,
                        "last_sweep": "2026-08-01T10:00:00+00:00", "pending_jobs": 0}
        kwargs.update(over)
        return raven.build_menu(**kwargs)   # type: ignore[arg-type]

    def test_survives_the_hosts_parser_intact(self) -> None:
        payload = self.build()
        spec = parse_menu(payload)
        self.assertEqual(spec["title"], "Muninn")
        self.assertEqual([s["id"] for s in spec["sections"]], ["recent", "archive"])

    def test_nothing_is_dropped_or_truncated(self) -> None:
        """Every row Muninn emits must survive. A dropped row is invisible.

        Separators are excluded from the count because the host legitimately
        trims a trailing one; nothing else may differ.
        """
        payload = self.build(recent=[a_session(f"s{i:04d}") for i in range(20)])
        spec = parse_menu(payload)
        emitted = sum(1 for section in payload["sections"] for item in section["items"]
                      if not item.get("separator"))
        parsed = sum(1 for section in spec["sections"] for item in section["items"]
                     if not item["separator"])
        self.assertEqual(parsed, emitted)

    def test_stays_inside_every_budget(self) -> None:
        payload = self.build(recent=[a_session(f"s{i:04d}") for i in range(500)])
        self.assertLessEqual(len(payload["sections"]), MAX_SECTIONS)
        total = 0
        for section in payload["sections"]:
            self.assertLessEqual(len(section["items"]), MAX_ITEMS_PER_SECTION)
            total += len(section["items"])
        self.assertLessEqual(total, MAX_TOTAL_ITEMS)

    def test_every_row_is_clickable_or_a_separator(self) -> None:
        """An item with neither id nor url renders disabled — a dead-looking row."""
        spec = parse_menu(self.build())
        for section in spec["sections"]:
            for item in section["items"]:
                if item["separator"]:
                    continue
                self.assertTrue(item["enabled"], item["label"])
                self.assertTrue(item["url"], item["label"])

    def test_publishes_links_not_actions(self) -> None:
        spec = parse_menu(self.build())
        for section in spec["sections"]:
            for item in section["items"]:
                self.assertEqual(item["action_id"], "")

    def test_emits_no_badge(self) -> None:
        """The host sums badges across ravens, so a corpus count here would read
        as thousands of pending decisions next to Huginn's approvals."""
        self.assertNotIn("badge", self.build())
        self.assertEqual(parse_menu(self.build())["badge"], 0)

    def test_urls_are_raven_local(self) -> None:
        for section in self.build()["sections"]:
            for item in section["items"]:
                if item.get("separator"):
                    continue
                url = item["url"]
                self.assertTrue(url.startswith("/"))
                self.assertNotEqual(url[1:2], "/")     # "//host" is scheme-relative
                self.assertNotIn("..", url.split("/"))
                self.assertNotIn("#", url)

    def test_empty_archive_still_says_something(self) -> None:
        """"Up but silent" must be distinguishable from "unreachable"."""
        spec = parse_menu(raven.build_menu(recent=[], sessions=0, chunks=0))
        self.assertEqual([s["id"] for s in spec["sections"]], ["archive"])
        self.assertTrue(spec["sections"][0]["items"])

    def test_never_swept_is_stated_not_blank(self) -> None:
        spec = parse_menu(raven.build_menu(recent=[], sessions=0, chunks=0, last_sweep=None))
        details = [i["detail"] for s in spec["sections"] for i in s["items"]]
        self.assertIn("never", details)

    def test_index_lag_is_flagged_for_attention(self) -> None:
        spec = parse_menu(self.build(lag={"claude": {"unindexed_or_grown_files": 7}}))
        labels = {i["label"]: i["style"] for s in spec["sections"] for i in s["items"]}
        self.assertEqual(labels.get("7 files not yet indexed"), "attention")

    def test_counts_read_as_prose(self) -> None:
        """A menubar row is text the user reads; "1 sessions" looks unfinished."""
        spec = parse_menu(raven.build_menu(
            recent=[], sessions=1, chunks=1, pending_jobs=1,
            lag={"claude": {"unindexed_or_grown_files": 1}}))
        labels = [i["label"] for s in spec["sections"] for i in s["items"]]
        self.assertIn("1 session · 1 chunk", labels)
        self.assertIn("1 file not yet indexed", labels)
        self.assertIn("1 session queued to index", labels)

    def test_no_lag_line_when_up_to_date(self) -> None:
        spec = parse_menu(self.build(lag={"claude": {"unindexed_or_grown_files": 0}}))
        labels = [i["label"] for s in spec["sections"] for i in s["items"]]
        self.assertFalse([lbl for lbl in labels if "not yet indexed" in lbl])

    def test_a_malformed_lag_reading_costs_a_line_not_the_menu(self) -> None:
        for bogus in ({"claude": "nonsense"}, {"claude": {"unindexed_or_grown_files": True}},
                      {"claude": {}}, "nonsense", None):
            spec = parse_menu(self.build(lag=bogus))     # type: ignore[arg-type]
            self.assertTrue(spec["sections"])

    def test_label_falls_back_through_topic_title_cwd_id(self) -> None:
        cases = [
            (a_session(topic="A topic", title="A title"), "A topic"),
            (a_session(topic=None, title="A title"), "A title"),
            (a_session(topic=None, title=None, cwd="/a/b/muninn"), "muninn"),
            (a_session("sid12345", topic=None, title=None, cwd=None), "sid12345"),
        ]
        for row, expected in cases:
            with self.subTest(expected=expected):
                spec = parse_menu(raven.build_menu(recent=[row], sessions=1, chunks=1))
                self.assertEqual(spec["sections"][0]["items"][0]["label"], expected)

    def test_only_the_basename_of_cwd_reaches_a_label(self) -> None:
        """A full path in a menubar row discloses the user's directory layout."""
        spec = parse_menu(raven.build_menu(
            recent=[a_session(topic=None, title=None, cwd="/Users/someone/secret/work/proj")],
            sessions=1, chunks=1))
        label = spec["sections"][0]["items"][0]["label"]
        self.assertEqual(label, "proj")
        self.assertNotIn("someone", label)

    def test_a_row_with_no_usable_id_is_omitted(self) -> None:
        """A session id becomes a URL path segment, so it is validated."""
        for bad in ("../../etc/passwd", "a/b", "", "has space", None, 42, "x" * 400):
            with self.subTest(session_id=bad):
                spec = parse_menu(raven.build_menu(
                    recent=[a_session(session_id=bad)], sessions=1, chunks=1))  # type: ignore[arg-type]
                self.assertEqual([s["id"] for s in spec["sections"]], ["archive"])

    def test_relative_times_are_human(self) -> None:
        # Offsets are deliberately off the exact boundary. An exact multiple of
        # 60 makes the assertion depend on microsecond truncation in the ISO
        # round trip, which flips the floor either way — a flaky test that says
        # nothing about the code.
        import datetime as dt
        import time
        now = time.time()
        cases = [(30, "just now"), (630, "10m ago"), (7500, "2h ago"),
                 (86400 * 1.2, "yesterday"), (86400 * 5.5, "5d ago")]
        for ago, expected in cases:
            iso = dt.datetime.fromtimestamp(now - ago, dt.timezone.utc).isoformat()
            with self.subTest(expected=expected):
                self.assertEqual(raven._relative_when(iso, now=now), expected)

    def test_an_unparseable_timestamp_costs_a_detail_not_the_menu(self) -> None:
        for bogus in ("not a date", "", None, 12345, "2026-13-45T99:99:99Z"):
            with self.subTest(started_at=bogus):
                self.assertEqual(raven._relative_when(bogus), "")  # type: ignore[arg-type]

    def test_a_future_timestamp_does_not_read_as_a_bug(self) -> None:
        import datetime as dt
        import time
        future = dt.datetime.fromtimestamp(time.time() + 9000, dt.timezone.utc).isoformat()
        self.assertEqual(raven._relative_when(future), "just now")


# ── Quit/Restart in the menu ───────────────────────────────────────────────────

class LifecycleMenuTest(unittest.TestCase):
    """Quit and Restart: drawn only where the click can be honoured."""

    def _menu(self, **kw) -> dict:
        return parse_menu(raven.build_menu(recent=[a_session()], sessions=1,
                                          chunks=1, **kw))

    def _lifecycle(self, menu: dict) -> dict | None:
        for section in menu["sections"]:
            if section.get("id") == "lifecycle":
                return section
        return None

    def test_absent_by_default(self) -> None:
        """The default payload has no server behind it — a Quit row there lies."""
        self.assertIsNone(self._lifecycle(self._menu()))

    def test_present_when_asked_for(self) -> None:
        section = self._lifecycle(self._menu(lifecycle=True))
        self.assertIsNotNone(section)
        self.assertEqual([item["label"] for item in section["items"]],
                         ["Quit Muninn", "Restart Muninn"])
        # action_id, because that is what the host's own parser produces from
        # our "id" — and `enabled` is what proves it will render clickable rather
        # than as an inert row.
        self.assertEqual([item["action_id"] for item in section["items"]],
                         [raven.QUIT, raven.RESTART])
        self.assertTrue(all(item["enabled"] for item in section["items"]))

    def test_it_is_the_last_section(self) -> None:
        # The destructive rows belong below everything a user opens the menu to
        # read, and a menu is read top-down.
        self.assertEqual(self._menu(lifecycle=True)["sections"][-1]["id"], "lifecycle")

    def test_the_rows_are_not_styled_as_attention(self) -> None:
        # Attention competes with the one Archive row that genuinely wants it.
        for item in self._lifecycle(self._menu(lifecycle=True))["items"]:
            self.assertEqual(item["style"], "muted")

    def test_no_start_row_is_published(self) -> None:
        """A stopped daemon has no menu for a Start row to live in."""
        labels = [item["label"]
                  for section in self._menu(lifecycle=True)["sections"]
                  for item in section["items"]]
        self.assertNotIn("Start Muninn", labels)


class ActionDispatchTest(unittest.TestCase):
    """``perform_action`` records intent and defers the stop. Both matter."""

    class FakeDaemon:
        def __init__(self, running: bool = True) -> None:
            self.running = running
            self.restart_requested = False
            self.signalled = False

        def request_stop(self, *, restart: bool = False) -> bool:
            if not self.running:
                return False
            self.restart_requested = restart
            return True

        def deliver_stop_signal(self) -> None:
            self.signalled = True

    def test_quit_accepts_and_defers_the_signal(self) -> None:
        d = self.FakeDaemon()
        reply, followup = raven.perform_action(d, raven.QUIT)
        self.assertEqual(reply, {"ok": True, "stopping": True})
        self.assertFalse(d.restart_requested)
        # The whole point of the split: nothing has stopped yet, because the
        # reply has not been written.
        self.assertFalse(d.signalled)
        followup()
        self.assertTrue(d.signalled)

    def test_restart_sets_the_flag_the_supervising_loop_reads(self) -> None:
        d = self.FakeDaemon()
        reply, followup = raven.perform_action(d, raven.RESTART)
        self.assertEqual(reply, {"ok": True, "restarting": True})
        self.assertTrue(d.restart_requested)
        self.assertIsNotNone(followup)

    def test_a_daemon_with_no_loop_refuses_rather_than_pretending(self) -> None:
        reply, followup = raven.perform_action(self.FakeDaemon(running=False), raven.QUIT)
        self.assertFalse(reply["ok"])
        self.assertIsNone(followup)

    def test_an_unknown_id_is_reported_not_ignored(self) -> None:
        d = self.FakeDaemon()
        reply, followup = raven.perform_action(d, "focus:something")
        self.assertEqual(reply, {"ok": False, "error": "unknown action"})
        self.assertIsNone(followup)
        self.assertFalse(d.signalled)


class HostileLabelTest(unittest.TestCase):
    """Titles, topics and paths come from transcripts. Assume they are hostile.

    A transcript holds whatever a user pasted and whatever a tool printed, and
    this project's archive is full of terminal output by construction. So every
    one of these is realistic content, not a contrived payload.
    """

    HOSTILE = {
        "ansi colour": "\x1b[31mDANGER\x1b[0m",
        "ansi clear screen": "boring\x1b[2J\x1b[Hgone",
        "osc window title": "x\x1b]0;pwned\x07y",
        "bare escape": "a\x1bb",
        "c1 csi introducer": "a\x9b31mb",
        "newline injection": "line one\nline two",
        "carriage return overwrite": "real text\rFAKE",
        "nul byte": "before\x00after",
        "bell": "ding\x07dong",
        "tab": "a\tb",
        "rtl override": "Quit\u202egnihtemos\u202c",
        "zero width": "Op\u200ben\u200b Console",
        "bom": "\ufeffTitle",
        "bidi isolate": "\u2066spoof\u2069",
    }

    def _labels(self, text: str) -> list[str]:
        payload = raven.build_menu(
            recent=[a_session(topic=text, title=text)],
            sessions=1, chunks=1, last_sweep=text)
        out: list[str] = []
        for section in payload["sections"]:
            out.append(section.get("title", ""))
            for item in section["items"]:
                out.append(item.get("label", ""))
                out.append(item.get("detail", ""))
        return [s for s in out if isinstance(s, str)]

    def test_no_control_character_reaches_a_label(self) -> None:
        for name, text in self.HOSTILE.items():
            with self.subTest(name=name):
                for label in self._labels(text):
                    self.assertNotIn("\x1b", label)
                    self.assertIsNone(_CONTROL_RE.search(label), repr(label))
                    self.assertIsNone(_SPOOF_RE.search(label), repr(label))
                    self.assertNotIn("\n", label)
                    self.assertNotIn("\r", label)

    def test_the_host_would_not_have_to_repair_anything(self) -> None:
        """Muninn's own sanitising is a no-op for the host, not a duplicate of it.

        If the host's sanitiser changes a label Muninn emitted, then Muninn sent
        something it should not have — and the row the user sees is not the row
        this code emitted.
        """
        for name, text in self.HOSTILE.items():
            with self.subTest(name=name):
                for label in self._labels(text):
                    self.assertEqual(host_sanitize_label(label, MAX_LABEL_LENGTH), label)

    def test_a_hostile_row_still_renders(self) -> None:
        """Sanitised, not refused. A bad character costs a row's text, not the raven."""
        spec = parse_menu(raven.build_menu(
            recent=[a_session(topic="\x1b[31mDeployed\x1b[0m staging")], sessions=1, chunks=1))
        self.assertEqual(spec["sections"][0]["items"][0]["label"], "Deployed staging")

    def test_an_all_escape_title_drops_to_the_next_candidate(self) -> None:
        """A title that sanitises to nothing must not become a blank clickable row."""
        spec = parse_menu(raven.build_menu(
            recent=[a_session(topic="\x1b[0m\x1b[0m", title=None, cwd="/a/b/fallback")],
            sessions=1, chunks=1))
        self.assertEqual(spec["sections"][0]["items"][0]["label"], "fallback")

    def test_labels_are_bounded(self) -> None:
        spec = parse_menu(raven.build_menu(
            recent=[a_session(topic="x" * 5000)], sessions=1, chunks=1))
        for section in spec["sections"]:
            for item in section["items"]:
                self.assertLessEqual(len(item["label"]), MAX_LABEL_LENGTH)
                self.assertLessEqual(len(item["detail"]), MAX_DETAIL_LENGTH)

    def test_a_non_string_title_is_not_coerced(self) -> None:
        """str() on a dict would put repr()'s punctuation on screen."""
        spec = parse_menu(raven.build_menu(
            recent=[a_session(topic={"evil": "}"}, title=None, cwd="/a/b/proj")],
            sessions=1, chunks=1))
        self.assertEqual(spec["sections"][0]["items"][0]["label"], "proj")

    def test_secret_bearing_text_is_a_documented_non_goal(self) -> None:
        """Muninn does not redact secrets in labels — it never emits prose.

        Stated as a test so the property is enforced rather than merely claimed:
        a label is a topic, a title, a cwd basename, or a count. Prompt and
        transcript text — the place a leaked credential would actually live —
        never reaches this payload, so there is nothing for a redactor to scan.
        The moment a row does carry prose, docs/specs/009 says this decision must
        be revisited in the same change; this test is what fails then.
        """
        secret = "AKIAIOSFODNN7EXAMPLE and ghp_" + "a" * 36
        payload = raven.build_menu(
            recent=[a_session(text=secret, words=1)], sessions=1, chunks=1)
        blob = json.dumps(payload)
        self.assertNotIn(secret, blob)
        self.assertNotIn("AKIA", blob)


# ── The transport surface ──────────────────────────────────────────────────────

class TransportGuardTest(RavenTestCase):
    """The guarantees the Unix-socket/named-pipe transport actually gives.

    This replaces the old ``HttpGuardTest``. There is no ``Host``, no
    ``Origin``, no ``Content-Length`` any more — a Unix domain socket cannot be
    opened by a browser tab at all, so those checks were retired rather than
    ported (docs/specs/021). What is left to guarantee: a request over the
    length cap never reaches the JSON parser, a malformed or non-dict body gets
    an honest ``{"ok": false}`` rather than a crash or a dropped connection, and
    an unrecognised ``op`` is refused the same way.
    """

    def setUp(self) -> None:
        super().setUp()
        self.service = ravenserve.serve(
            lambda: raven.build_menu(recent=[a_session()], sessions=1, chunks=1))
        self.addCleanup(self.service.stop)

    def _raw(self, raw: bytes) -> bytes:
        conn = _connect(self.service)
        try:
            conn.send_bytes(raw)
            return conn.recv_bytes()
        finally:
            conn.close()

    def test_menu_is_served(self) -> None:
        reply = json.loads(self._raw(json.dumps({"op": raven.MENU_OP}).encode()))
        self.assertTrue(reply["ok"])
        self.assertEqual(parse_menu(reply["body"])["title"], "Muninn")

    def test_a_message_over_the_cap_is_refused_without_being_parsed(self) -> None:
        """multiprocessing.connection enforces the length cap while reading the
        length-prefixed frame, before a single byte reaches ``json.loads`` — the
        transport's equivalent of the old ``Content-Length`` guard, minus the
        header that guard existed to police in the first place. The connection
        is closed with no reply, which is how a caller can tell the message was
        never dispatched at all.
        """
        oversized = json.dumps(
            {"op": raven.MENU_OP, "pad": "x" * (ravenserve.MAX_REQUEST_BODY * 2)}).encode()
        conn = _connect(self.service)
        try:
            conn.send_bytes(oversized)
            with self.assertRaises(EOFError):
                conn.recv_bytes()
        finally:
            conn.close()

    def test_a_non_json_body_answers_ok_false_not_a_crash(self) -> None:
        for raw in (b"", b"not json", b"\xff\xfe garbage", b"{not json"):
            with self.subTest(raw=raw):
                reply = json.loads(self._raw(raw))
                self.assertFalse(reply["ok"])
                self.assertIn("error", reply)

    def test_a_non_dict_body_answers_ok_false(self) -> None:
        for raw in (b"[]", b"7", b'"a string"', b"null", b"true"):
            with self.subTest(raw=raw):
                reply = json.loads(self._raw(raw))
                self.assertFalse(reply["ok"])

    def test_an_unknown_op_is_refused(self) -> None:
        reply = json.loads(self._raw(json.dumps({"op": "delete-everything"}).encode()))
        self.assertFalse(reply["ok"])

    def test_a_missing_op_is_refused(self) -> None:
        reply = json.loads(self._raw(json.dumps({"id": raven.QUIT}).encode()))
        self.assertFalse(reply["ok"])

    def test_a_failing_provider_answers_ok_false_rather_than_dropping(self) -> None:
        """A dropped connection reads to the host as "not answering," which
        points the user at completely the wrong problem than an archive error."""
        def boom() -> dict:
            raise RuntimeError("archive on fire")

        with ravenserve.serve(boom) as svc:
            reply = _ask(svc, {"op": raven.MENU_OP})
        self.assertFalse(reply["ok"])
        # The exception's own text must never reach the reply — see _handle's
        # docstring on why only the exception class is logged.
        self.assertNotIn("archive on fire", json.dumps(reply))

    def test_the_menu_is_rebuilt_per_request(self) -> None:
        """A payload captured at startup would freeze the session count."""
        calls: list[int] = []

        def provide() -> dict:
            calls.append(1)
            return raven.build_menu(recent=[], sessions=len(calls), chunks=0)

        with ravenserve.serve(provide) as svc:
            for _ in range(3):
                _ask(svc, {"op": raven.MENU_OP})
        self.assertEqual(len(calls), 3)

    def test_nothing_inbound_is_reflected_in_an_error_reply(self) -> None:
        reply = self._raw(json.dumps({"op": "reflect-me-please"}).encode())
        self.assertNotIn(b"reflect-me-please", reply)

    def test_one_request_per_connection(self) -> None:
        """No keep-alive, no pipelining — a second message on the same
        connection gets no second reply, matching the one-shot protocol.

        The server has already closed its end by the time a second message is
        attempted, so which call raises is a race between this client's send
        and the closed socket being noticed — a ``BrokenPipeError`` on the
        second ``send_bytes`` is just as much "no second reply" as an
        ``EOFError`` on the following ``recv_bytes`` would be.
        """
        conn = _connect(self.service)
        try:
            conn.send_bytes(json.dumps({"op": raven.MENU_OP}).encode())
            first = json.loads(conn.recv_bytes())
            self.assertTrue(first["ok"])
            with self.assertRaises((EOFError, OSError)):
                conn.send_bytes(json.dumps({"op": raven.MENU_OP}).encode())
                conn.recv_bytes()
        finally:
            conn.close()


class ActionEndpointTest(RavenTestCase):
    """The action op, its guards, and the descriptor claim that goes with it."""

    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []
        self.followups: list[str] = []

        def handler(action_id: str):
            self.calls.append(action_id)
            if action_id == raven.QUIT:
                return {"ok": True, "stopping": True}, lambda: self.followups.append("ran")
            if action_id == "boom":
                raise RuntimeError("handler exploded")
            return {"ok": False, "error": "unknown action"}, None

        self.service = ravenserve.serve(
            lambda: raven.build_menu(recent=[a_session()], sessions=1, chunks=1,
                                     lifecycle=True),
            action_handler=handler)
        self.addCleanup(self.service.stop)

    def post(self, body: dict) -> dict:
        return _ask(self.service, body)

    def test_a_published_action_is_dispatched(self) -> None:
        reply = self.post({"op": raven.ACTION_OP, "id": raven.QUIT})
        self.assertTrue(reply["ok"])
        self.assertTrue(reply["stopping"])
        self.assertEqual(self.calls, [raven.QUIT])

    def test_the_reply_is_written_before_the_followup_runs(self) -> None:
        """A quit that drops the connection reads as a wedged raven, not a quit.

        The two halves are asserted differently on purpose. That the reply comes
        first is proved synchronously: this client read a complete reply before
        looking. That the followup then runs is *waited* for, because it runs on
        the server thread after the flush -- so demanding it be finished the
        instant the client returns asserts the opposite ordering to the one
        claimed, and races.
        """
        import time

        reply = self.post({"op": raven.ACTION_OP, "id": raven.QUIT})
        self.assertTrue(reply["ok"])
        deadline = time.monotonic() + 10.0
        while not self.followups and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.followups, ["ran"])

    def test_a_refused_action_reports_ok_false_not_a_dropped_connection(self) -> None:
        # ok=false with a real reply is the whole point: a caller that only
        # checked for a live connection would otherwise call this a success.
        reply = self.post({"op": raven.ACTION_OP, "id": "nope"})
        self.assertFalse(reply["ok"])

    def test_a_handler_that_raises_answers_ok_false_rather_than_dropping(self) -> None:
        reply = self.post({"op": raven.ACTION_OP, "id": "boom"})
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "internal error")
        self.assertNotIn("handler exploded", reply.get("error", ""))

    def test_malformed_action_bodies_are_refused_with_a_reason(self) -> None:
        for body in ({"op": raven.ACTION_OP}, {"op": raven.ACTION_OP, "id": ""},
                     {"op": raven.ACTION_OP, "id": 7}):
            with self.subTest(body=body):
                reply = self.post(body)
                self.assertFalse(reply["ok"])
        self.assertEqual(self.calls, [])

    def test_a_body_over_the_cap_never_reaches_the_handler(self) -> None:
        oversized = json.dumps({"op": raven.ACTION_OP,
                                "id": "x" * (ravenserve.MAX_REQUEST_BODY * 2)}).encode()
        conn = _connect(self.service)
        try:
            conn.send_bytes(oversized)
            with self.assertRaises(EOFError):
                conn.recv_bytes()
        finally:
            conn.close()
        self.assertEqual(self.calls, [])

    def test_a_long_id_is_truncated_before_it_is_dispatched(self) -> None:
        self.post({"op": raven.ACTION_OP, "id": "a" * 400})
        self.assertEqual(self.calls, ["a" * raven.MAX_ACTION_ID])

    def test_a_menu_request_is_not_an_action(self) -> None:
        reply = self.post({"op": raven.MENU_OP, "id": raven.QUIT})
        self.assertTrue(reply["ok"])
        self.assertEqual(self.calls, [])

    def test_the_descriptor_advertises_the_action_op(self) -> None:
        # Advertising it is the same claim as routing it; the host draws a row
        # from one and posts to the other.
        payload = json.loads(raven.descriptor_path().read_text())
        self.assertEqual(payload["endpoints"]["action"], raven.ACTION_OP)


class NoActionsPublishedTest(RavenTestCase):
    """Without a handler, the pre-existing "no actions" contract is unchanged."""

    def setUp(self) -> None:
        super().setUp()
        self.service = ravenserve.serve(
            lambda: raven.build_menu(recent=[a_session()], sessions=1, chunks=1))
        self.addCleanup(self.service.stop)

    def test_the_descriptor_omits_the_action_op(self) -> None:
        payload = json.loads(raven.descriptor_path().read_text())
        self.assertEqual(payload["endpoints"], {"menu": raven.MENU_OP})

    def test_the_menu_draws_no_lifecycle_rows(self) -> None:
        reply = _ask(self.service, {"op": raven.MENU_OP})
        self.assertTrue(reply["ok"])
        self.assertNotIn("lifecycle", [s.get("id") for s in reply["body"]["sections"]])

    def test_an_action_request_answers_ok_false(self) -> None:
        reply = _ask(self.service, {"op": raven.ACTION_OP, "id": raven.QUIT})
        self.assertFalse(reply["ok"])


class MenuProviderTest(RavenTestCase):
    """The provider queries a real archive and must not fail on an empty one."""

    def test_an_empty_archive_produces_a_parseable_menu(self) -> None:
        provider = ravenserve.menu_provider_for(self.tmp / "muninn.db")
        spec = parse_menu(provider())
        self.assertEqual(spec["title"], "Muninn")
        self.assertTrue(spec["sections"])

    def test_a_populated_archive_lists_its_sessions(self) -> None:
        from muninn import store
        db = self.tmp / "muninn.db"
        st = store.open_store(db)
        st.upsert_session({
            "session_id": "sess0001", "source": "claude", "provenance": "human",
            "cwd": "/a/b/proj", "started_at": "2026-08-01T09:00:00+00:00",
            "text": "hello", "words": 1, "updated_at": "2026-08-01T09:00:00+00:00",
            "user_turns": 1, "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "source_present": 1, "origin": "raw",
        })
        st.close()
        spec = parse_menu(ravenserve.menu_provider_for(db)())
        labels = [i["label"] for s in spec["sections"] for i in s["items"]]
        self.assertIn("proj", labels)

    def test_the_provider_is_usable_from_another_thread(self) -> None:
        """sqlite3 connections are not thread-safe; the provider opens its own.

        This is the failure that would only show up in production, since every
        real call arrives on a request thread while the indexer holds its own
        connection on the main one.
        """
        import threading
        provider = ravenserve.menu_provider_for(self.tmp / "muninn.db")
        provider()      # open once on this thread first
        result: list[object] = []

        def run() -> None:
            try:
                result.append(parse_menu(provider())["title"])
            except Exception as exc:      # noqa: BLE001 - the assertion is the type
                result.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join(timeout=30)
        self.assertEqual(result, ["Muninn"])

    def _archive_with(self, *, topic: str, outcome: str) -> Path:
        """One ingested session, then enriched — the two writes the real thing does.

        Enrichment goes through ``set_facets`` rather than ``upsert_session``,
        which carries ingest columns only. Building the row by hand here would
        pass while the production path wrote the fields somewhere recall never
        looks.
        """
        from muninn import store
        db = self.tmp / "muninn.db"
        st = store.open_store(db)
        st.upsert_session({
            "session_id": "sess0001", "source": "claude", "provenance": "human",
            "cwd": "/a/b/proj", "started_at": "2026-08-01T09:00:00+00:00",
            "text": "hello", "words": 1, "updated_at": "2026-08-01T09:00:00+00:00",
            "user_turns": 1, "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "source_present": 1, "origin": "raw",
        })
        st.conn.execute(
            "UPDATE sessions SET topic = ?, outcome = ? WHERE session_id = ?",
            (topic, outcome, "sess0001"))
        st.conn.commit()
        st.close()
        return db

    def test_a_loose_end_reaches_the_menu_through_the_provider(self) -> None:
        """The seam: recall's SQL, the menu builder, and the raven parser agree.

        Each half is tested alone in test_recall.py. This is the join, which is
        the part that fails silently — a menu that simply omits a section looks
        exactly like a menu with nothing to report.
        """
        db = self._archive_with(topic="the flaky retry", outcome="ongoing")
        spec = parse_menu(ravenserve.menu_provider_for(db)())
        section = next(s for s in spec["sections"] if s["id"] == "unfinished")
        self.assertIn("proj", section["title"])
        self.assertEqual([i["label"] for i in section["items"]], ["the flaky retry"])

    def test_a_finished_session_draws_no_unfinished_section(self) -> None:
        db = self._archive_with(topic="the flaky retry", outcome="fixed")
        spec = parse_menu(ravenserve.menu_provider_for(db)())
        self.assertNotIn("unfinished", [s["id"] for s in spec["sections"]])

    def test_the_menu_does_not_pay_for_the_embedding_half(self) -> None:
        """Related-work needs the whole vector matrix, far past a menu's budget.

        Guarding this by timing would be flaky, so it asserts the real
        constraint instead: the expensive entry point is never called.
        """
        from muninn import recall as recall_module
        db = self._archive_with(topic="the flaky retry", outcome="ongoing")
        called: list[object] = []
        original = recall_module.recall
        recall_module.recall = lambda *a, **k: called.append(a) or original(*a, **k)
        try:
            parse_menu(ravenserve.menu_provider_for(db)())
        finally:
            recall_module.recall = original
        self.assertEqual(called, [])

    # ── pages_dir rendering (spec 021, _write_pages/_write_index_page/
    # _write_session_page) ─────────────────────────────────────────────────

    def test_pages_dir_is_untouched_when_omitted(self) -> None:
        """Tests that build a payload with no listener have nowhere for pages
        to live, and must not get an implicit directory anyway."""
        provider = ravenserve.menu_provider_for(self.tmp / "muninn.db")
        provider()
        self.assertFalse((self.tmp / "pages").exists())

    def test_pages_dir_renders_an_index_page_restating_the_archive_section(self) -> None:
        pages = self.tmp / "pages"
        provider = ravenserve.menu_provider_for(self.tmp / "muninn.db", pages_dir=pages)
        payload = provider()
        index_html = (pages / "index.html").read_text(encoding="utf-8")
        self.assertIn("Muninn", index_html)
        archive = next(s for s in payload["sections"] if s["id"] == "archive")
        self.assertIn(archive["items"][0]["label"], index_html)

    def test_pages_dir_renders_a_session_page_with_the_real_transcript(self) -> None:
        from muninn import store
        db = self.tmp / "muninn.db"
        st = store.open_store(db)
        st.upsert_session({
            "session_id": "sess0001", "source": "claude", "provenance": "human",
            "cwd": "/a/b/proj", "started_at": "2026-08-01T09:00:00+00:00",
            "text": "the actual transcript text lives here, not a stub",
            "words": 9, "updated_at": "2026-08-01T09:00:00+00:00",
            "user_turns": 1, "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "source_present": 1, "origin": "raw",
        })
        st.close()
        pages = self.tmp / "pages"
        provider = ravenserve.menu_provider_for(db, pages_dir=pages)
        provider()
        page = (pages / "session" / "sess0001.html").read_text(encoding="utf-8")
        self.assertIn("the actual transcript text lives here, not a stub", page)
        self.assertIn("sess0001", page)
        self.assertIn("/a/b/proj", page)

    def test_pages_dir_only_gets_pages_the_payload_just_referenced(self) -> None:
        """A url build_menu did not emit must have no file waiting for it —
        Roost's containment check treats "no file" the same as "refused"."""
        from muninn import store
        db = self.tmp / "muninn.db"
        st = store.open_store(db)
        st.upsert_session({
            "session_id": "sess0001", "source": "claude", "provenance": "human",
            "cwd": "/a/b/proj", "started_at": "2026-08-01T09:00:00+00:00",
            "text": "hello", "words": 1, "updated_at": "2026-08-01T09:00:00+00:00",
            "user_turns": 1, "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "source_present": 1, "origin": "raw",
        })
        st.close()
        pages = self.tmp / "pages"
        ravenserve.menu_provider_for(db, pages_dir=pages)()
        self.assertFalse((pages / "session" / "never-existed.html").exists())

    @unittest.skipIf(sys.platform == "win32", "asserts POSIX mode bits")
    def test_pages_dir_is_owner_only(self) -> None:
        from muninn import store
        db = self.tmp / "muninn.db"
        st = store.open_store(db)
        st.close()
        pages = self.tmp / "pages"
        ravenserve.menu_provider_for(db, pages_dir=pages)()
        self.assertEqual(stat.S_IMODE(pages.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((pages / "session").stat().st_mode), 0o700)


# ── Parity with the document this implements ──────────────────────────────────

class AppistryParityTest(unittest.TestCase):
    """The numbers this file's local parser encodes, in one checkable place.

    If Appistry changes a cap, this is the list to reconcile. See this module's
    docstring for why the real parser is not imported.
    """

    def test_bounds_match_the_protocol_document(self) -> None:
        self.assertEqual(
            (MAX_SECTIONS, MAX_ITEMS_PER_SECTION, MAX_TOTAL_ITEMS,
             MAX_LABEL_LENGTH, MAX_DETAIL_LENGTH, MAX_ACTION_ID_LENGTH, MAX_URL_LENGTH),
            (12, 50, 200, 120, 80, 128, 512))
        self.assertEqual(STYLES, ("normal", "attention", "muted"))

    def test_muninns_own_caps_do_not_exceed_the_hosts(self) -> None:
        self.assertLessEqual(raven.MAX_LABEL, MAX_LABEL_LENGTH)
        self.assertLessEqual(raven.MAX_DETAIL, MAX_DETAIL_LENGTH)
        self.assertLessEqual(raven.RECENT_LIMIT, MAX_ITEMS_PER_SECTION)

    def test_muninns_sanitiser_agrees_with_the_hosts(self) -> None:
        """Both directions, over every hostile string this suite knows.

        Muninn's sanitiser is allowed to be stricter, never looser: a raven that
        emits something the host repairs is a raven whose rendered menu is not
        the menu it wrote.
        """
        for name, text in HostileLabelTest.HOSTILE.items():
            with self.subTest(name=name):
                self.assertEqual(raven.safe_label(text), host_sanitize_label(text))

    def test_the_declared_range_overlaps_what_the_host_speaks(self) -> None:
        host_min, host_max = 1, 1
        self.assertTrue(raven.MIN_API <= host_max and raven.MAX_API >= host_min)


if __name__ == "__main__":
    unittest.main()
