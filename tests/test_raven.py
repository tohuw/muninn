"""The raven contract: descriptor lifecycle, menu payload, and the HTTP guards.

Acceptance criteria from docs/specs/009-raven-descriptor-menu.md.

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
"""
from __future__ import annotations

import http.client
import json
import os
import re
import socket
import stat
import sys
import tempfile
import unittest
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


class RavenTestCase(unittest.TestCase):
    """Points RAVENS_STATE_DIR at a tempdir.

    Every test in this file must do this. The real value is a shared directory in
    the user's home that a live Appistry polls, and a test that published there
    would put a descriptor naming a dead port into the user's actual menubar.
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
    def test_fields_match_the_protocol(self) -> None:
        payload = raven.descriptor(47101, pid=4242, started=1785315600.5)
        self.assertEqual(payload, {
            "api_version": 1,
            "min_api": 1,
            "max_api": 1,
            "name": "muninn",
            "display": "Muninn",
            "pid": 4242,
            "port": 47101,
            "started": 1785315600.5,
            "host_priority": 50,
            "endpoints": {"menu": "/api/menu"},
        })

    def test_declares_a_range_not_a_single_version(self) -> None:
        """tohuw/huginn#38: equality comparison silently disabled every plugin."""
        payload = raven.descriptor(1)
        self.assertLessEqual(payload["min_api"], payload["api_version"])
        self.assertGreaterEqual(payload["max_api"], payload["api_version"])

    def test_defers_to_huginn(self) -> None:
        # 50 against Huginn's 100. Ordering is data the ravens supply; the host
        # knows neither name.
        self.assertEqual(raven.descriptor(1)["host_priority"], 50)
        self.assertLess(raven.HOST_PRIORITY, 100)

    def test_advertises_no_token_and_no_action_endpoint(self) -> None:
        """Both absences are decisions. See ravenserve.py's module docstring."""
        payload = raven.descriptor(1)
        self.assertNotIn("token_path", payload)
        self.assertNotIn("token_header", payload)
        self.assertNotIn("action", payload["endpoints"])

    def test_supplies_a_plausible_epoch_started(self) -> None:
        """Without ``started`` a recycled PID passes as a live raven.

        Also catches the ``time.monotonic()`` mistake: a monotonic reading is not
        epoch-based, so the host's cross-check would reject every live process.
        """
        import time
        payload = raven.descriptor(1)
        self.assertAlmostEqual(payload["started"], time.time(), delta=5.0)
        self.assertGreater(payload["started"], 1_600_000_000)

    def test_pid_names_this_process(self) -> None:
        self.assertEqual(raven.descriptor(1)["pid"], os.getpid())

    def test_publish_writes_valid_json_at_the_right_name(self) -> None:
        path = raven.publish(47101)
        self.assertEqual(path, raven.descriptor_path())
        # The filename stem must equal the declared name, or the host refuses the
        # file rather than reconciling it.
        self.assertEqual(path.name, "muninn.json")
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["name"], path.stem)
        self.assertEqual(payload["port"], 47101)

    @unittest.skipIf(sys.platform == "win32", "NTFS uses ACLs, not mode bits")
    def test_permissions_are_owner_only(self) -> None:
        path = raven.publish(47101)
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
            path = raven.publish(47101)
        finally:
            os.umask(prior)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_publish_leaves_no_temp_file_behind(self) -> None:
        raven.publish(47101)
        leftovers = [p.name for p in raven.state_dir().iterdir() if p.name != "muninn.json"]
        self.assertEqual(leftovers, [])

    def test_publish_replaces_a_stale_descriptor(self) -> None:
        raven.publish(1111)
        raven.publish(2222)
        payload = json.loads(raven.descriptor_path().read_text(encoding="utf-8"))
        self.assertEqual(payload["port"], 2222)

    def test_withdraw_removes_it(self) -> None:
        path = raven.publish(47101)
        raven.withdraw(path)
        self.assertFalse(path.exists())

    def test_withdraw_is_idempotent(self) -> None:
        """A double stop must not raise: this runs in a shutdown path."""
        path = raven.publish(47101)
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
        payload = raven.descriptor(47101, pid=999_999_998, started=1.0)
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
            self.assertEqual(payload["port"], service.port)
        finally:
            service.stop()
        self.assertFalse(raven.descriptor_path().exists())

    def test_the_advertised_port_is_actually_listening(self) -> None:
        """Publish after bind, never before.

        A descriptor naming a port nothing is listening on makes the host report
        a healthy Muninn as unreachable during startup.
        """
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            port = json.loads(svc.descriptor.read_text(encoding="utf-8"))["port"]
            with socket.create_connection(("127.0.0.1", port), timeout=5):
                pass

    def test_binds_loopback_only(self) -> None:
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            self.assertEqual(svc.server.server_address[0], "127.0.0.1")

    def test_stop_releases_the_port_for_a_restart(self) -> None:
        """Without server_close() the socket stays bound and a restart fails."""
        first = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        first.stop()
        second = ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0))
        second.stop()

    def test_context_manager_withdraws(self) -> None:
        with ravenserve.serve(lambda: raven.build_menu(recent=[], sessions=0, chunks=0)) as svc:
            descriptor = svc.descriptor
            self.assertTrue(descriptor.exists())
        self.assertFalse(descriptor.exists())

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


# ── Hostile transcript text ───────────────────────────────────────────────────

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


# ── The HTTP surface ──────────────────────────────────────────────────────────

class HttpGuardTest(RavenTestCase):
    """Loopback is reachable by any web page the user has open.

    With no token, ``Host`` and ``Origin`` are the *only* thing between this port
    and such a page — the opposite of the intuition that "no secret to steal"
    means there is less to defend.
    """

    def setUp(self) -> None:
        super().setUp()
        self.service = ravenserve.serve(
            lambda: raven.build_menu(recent=[a_session()], sessions=1, chunks=1))
        self.addCleanup(self.service.stop)

    def request(self, method: str = "GET", path: str = "/api/menu",
                host: str = "127.0.0.1", headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.service.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", host)
            for name, value in (headers or {}).items():
                conn.putheader(name, value)
            conn.endheaders()
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def test_menu_is_served(self) -> None:
        status, _headers, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(parse_menu(json.loads(body))["title"], "Muninn")

    def test_loopback_host_names_are_accepted(self) -> None:
        for host in ("127.0.0.1", "localhost", "127.0.0.1:1234", "[::1]:80", "LOCALHOST"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 200)

    def test_a_foreign_host_is_refused(self) -> None:
        """The DNS-rebinding defence: a page served from evil.example carries
        that hostname here even when it resolves to 127.0.0.1."""
        for host in ("evil.example.com", "127.0.0.1.evil.com", "localhost.evil.com",
                     "0.0.0.0", "169.254.169.254"):
            with self.subTest(host=host):
                self.assertEqual(self.request(host=host)[0], 400)

    def test_a_missing_host_is_refused(self) -> None:
        """Absent must not be treated as acceptable — that is a one-line bypass."""
        sock = socket.create_connection(("127.0.0.1", self.service.port), timeout=10)
        try:
            sock.sendall(b"GET /api/menu HTTP/1.1\r\n\r\n")
            self.assertIn(b"400", sock.recv(64).split(b"\r\n")[0])
        finally:
            sock.close()

    def test_any_origin_is_refused(self) -> None:
        for origin in ("https://evil.example", "null", "",
                       f"http://127.0.0.1:{self.service.port}"):
            with self.subTest(origin=origin):
                # Muninn's own origin is refused too: allowlisting it would let a
                # page served from this very port script the endpoint.
                self.assertEqual(self.request(headers={"Origin": origin})[0], 403)

    def test_content_length_is_guarded_before_anything_is_read(self) -> None:
        for length in ("-1", "-999999999", "99999", "abc", "1 1", str(2 ** 64)):
            with self.subTest(content_length=length):
                # -1 is the case that matters most: read(-1) means "until EOF",
                # which is no bound at all.
                self.assertEqual(self.request(headers={"Content-Length": length})[0], 413)

    def test_post_is_refused_because_no_actions_are_published(self) -> None:
        self.assertEqual(self.request(method="POST")[0], 405)

    def test_a_cross_origin_post_is_refused_before_routing(self) -> None:
        """403, not 405: a route-shaped answer confirms what this port is."""
        self.assertEqual(
            self.request(method="POST", headers={"Origin": "https://evil.example"})[0], 403)

    def test_guards_apply_to_every_route(self) -> None:
        for path in ("/", "/api/menu", "/session/abc123", "/nope"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path, host="evil.example.com")[0], 400)
                self.assertEqual(
                    self.request(path=path, headers={"Origin": "https://e.example"})[0], 403)

    def test_headers_come_from_an_allowlist(self) -> None:
        status, headers, _body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertTrue(headers.get("Content-Length"))
        # Nothing inbound is reflected.
        echoed = self.request(headers={"X-Muninn-Echo": "reflect-me"})[1]
        self.assertNotIn("reflect-me", json.dumps(echoed))

    def test_html_carries_a_restrictive_csp(self) -> None:
        for path in ("/", "/session/abc123"):
            with self.subTest(path=path):
                _status, headers, _body = self.request(path=path)
                self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
                self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")

    def test_a_session_page_escapes_its_id(self) -> None:
        status, _headers, body = self.request(path="/session/<script>alert(1)</script>")
        self.assertNotIn(b"<script>", body)
        self.assertIn(status, (200, 404))

    def test_unknown_routes_are_404_not_500(self) -> None:
        self.assertEqual(self.request(path="/nope")[0], 404)

    def test_a_failing_provider_answers_500_rather_than_dropping(self) -> None:
        """A dropped connection reads to the host as "not answering on its port",
        which points the user at completely the wrong problem."""
        def boom() -> dict:
            raise RuntimeError("archive on fire")

        with ravenserve.serve(boom) as svc:
            conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=10)
            try:
                conn.request("GET", "/api/menu")
                self.assertEqual(conn.getresponse().status, 500)
            finally:
                conn.close()

    def test_the_menu_is_rebuilt_per_request(self) -> None:
        """A payload captured at startup would freeze the session count."""
        calls: list[int] = []

        def provide() -> dict:
            calls.append(1)
            return raven.build_menu(recent=[], sessions=len(calls), chunks=0)

        with ravenserve.serve(provide) as svc:
            for _ in range(3):
                conn = http.client.HTTPConnection("127.0.0.1", svc.port, timeout=10)
                try:
                    conn.request("GET", "/api/menu")
                    conn.getresponse().read()
                finally:
                    conn.close()
        self.assertEqual(len(calls), 3)


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
