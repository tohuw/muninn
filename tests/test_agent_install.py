"""The login-agent installer: Muninn's spec, all three backends, and coexistence.

Normative source: docs/specs/010-daemon.md, "Follow-up seam: `corvidae`".

## What this file covers, and what it deliberately does not

The launchd/systemd/Run-key *mechanism* lives in `corvidae.login_agent` and has
its own tests there. Duplicating them would be worse than useless: two copies of
a security assertion drift, and the copy a reader finds first is the one they
trust. So the hardening properties (``plistlib`` rather than a string template,
systemd refusing ``\\n``/``\\r``/``%``, 0600 files, refused symlinks) are asserted
here only through the *artefacts Muninn's spec produces* — one test per property,
proving Muninn actually inherits it rather than re-proving corvidae works.

What is genuinely Muninn's, and is therefore tested thoroughly:

- the spec's argv runs ``muninn serve`` (not ``index --watch``, which publishes
  nothing);
- every path and label is **disjoint from Huginn's** — see :class:`CoexistenceTest`,
  which is the point of the whole exercise;
- ``install`` refuses while an ingest loop holds the single-instance lock, because
  a supervisor restarting a process that exits 1 is a crash loop;
- ``doctor`` says whether an agent is installed.

## Linux and Windows are exercised on macOS, through the seam

corvidae exposes each backend's OS boundary as one overridable method
(``launchctl``/``systemctl``/``registry``), and overriding it is documented as
supported. Every backend below is driven that way, so a Linux unit and a Windows
Run key are both verified on the macOS dev host — the pattern Huginn's
``tests/test_agent_install.py`` established.

## Exit codes are asserted; wording is not

corvidae states outright that its backends' printed text may change within a
CalVer year and that the *exit code* is the contract. So nothing here matches on a
message from corvidae. The two places a message is asserted are Muninn's own
(``agent_install.install``'s lock refusal, and `doctor`'s line), and both check for
a stable substring rather than a whole line.
"""
from __future__ import annotations

import contextlib
import io
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from corvidae.login_agent import (
    LaunchdAgent,
    LoginAgentSpec,
    SystemdUserAgent,
    WindowsStartupAgent,
)

from muninn import agent_install, cli, daemon, paths

# ── Huginn's values, as literal constants ─────────────────────────────────────
#
# Source: /Users/tohuw/Projects/huginn -> huginn/agent_install.py, at the commit
# that introduced corvidae consumption. Copied as literals **on purpose**:
# `huginn` is not a dependency of Muninn and will never be importable in this test
# environment, so the alternative to literals is no coexistence test at all.
#
# The tradeoff is stated rather than hidden. If Huginn ever changes one of these,
# this test keeps passing while the real collision it was written to catch becomes
# possible — so the values are named here, in one block, next to the file they came
# from, precisely so a Huginn-side rename can be reflected in one edit. A
# docstring claiming disjointness would have been cheaper and worth less.
HUGINN_LABEL = "is.tohuw.huginn"
HUGINN_UNIT_NAME = "huginn.service"
HUGINN_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{HUGINN_LABEL}.plist"
HUGINN_LOG = Path.home() / ".local" / "state" / "huginn" / "agent.log"
HUGINN_DAEMON_RUN_VALUE = "HuginnDaemon"
HUGINN_TRAY_RUN_VALUE = "Huginn"
HUGINN_BACKUP_TAG = "huginn-bak"


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", stderr)


@contextlib.contextmanager
def _quiet():
    """Swallow a backend's own diagnosis. The exit code is what is asserted."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


class _TempState(unittest.TestCase):
    """Base: a redirected ``paths.STATE_DIR``, so nothing touches a real archive.

    ``paths.STATE_DIR`` is patched rather than the environment because it is
    computed at *import* time (see muninn/paths.py), so setting ``XDG_STATE_HOME``
    after import would move nothing. ``agent_install.log_path`` and
    ``daemon.lock_path`` both read it at call time, which is what makes one patch
    cover both.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-agent-"))
        self.addCleanup(self._cleanup)
        self.prior_state_dir = paths.STATE_DIR
        paths.STATE_DIR = self.tmp / "state"
        self.addCleanup(setattr, paths, "STATE_DIR", self.prior_state_dir)
        # Pin the environment check to "agrees" for every test that is not about
        # it. ``install()`` and `doctor` both consult the *live* environment, so
        # a developer or CI runner who exports XDG_STATE_HOME would otherwise
        # turn every install-reaches-the-backend test red for a reason none of
        # them are testing. EnvironmentMismatchTest below patches nothing and
        # drives the real function with synthetic environments instead.
        clean = patch.object(agent_install, "environment_mismatch",
                             return_value=agent_install.EnvironmentMismatch())
        clean.start()
        self.addCleanup(clean.stop)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def redirect_agent(self, *, launchctl=None) -> LaunchdAgent:
        """Patch ``get_login_agent`` to a launchd backend confined to the tempdir.

        Any test that calls ``agent_install.install()``, or that reads
        ``installed()`` through the real ``get_login_agent()``, **must** use this.
        Two failures it prevents, the first of which actually happened during this
        change's own mutation testing:

        1. **A real install.** ``install()`` on the dev machine writes
           ``~/Library/LaunchAgents/is.tohuw.muninn.plist`` and runs a real
           ``launchctl load``. A test that only avoids that because the code under
           test refused is not isolated — it is one mutation away from installing
           a live LaunchAgent, which is exactly what it did.
        2. **A verdict that depends on the developer's machine.** ``installed()``
           against the real plist path answers differently for someone who has run
           `muninn install-agent`, so a green suite would mean nothing on their
           box.

        Patching ``get_login_agent`` rather than ``spec`` is deliberate: the
        ``launchctl`` boundary lives on the *backend*, and a redirected spec with a
        live boundary would still shell out to the real launchd.
        """
        plist = self.tmp / "LaunchAgents" / f"{agent_install.LABEL}.plist"
        calls: list[tuple] = []
        run = launchctl or (lambda *a: _fail() if a[0] == "list" else _ok())

        class _Agent(LaunchdAgent):
            def launchctl(self, *args):
                calls.append(args)
                return run(*args)

        from dataclasses import replace

        agent = _Agent(replace(agent_install.spec(), plist_path=plist))
        agent.calls = calls          # type: ignore[attr-defined]
        patcher = patch.object(agent_install, "get_login_agent", return_value=agent)
        patcher.start()
        self.addCleanup(patcher.stop)
        return agent


# ── Muninn's spec ─────────────────────────────────────────────────────────────

class SpecTest(_TempState):
    """The five required fields, and the two that must agree with the daemon."""

    def test_argv_runs_muninn_serve(self) -> None:
        # Not `index --watch`: since spec 010 that path publishes no descriptor,
        # no /api/menu port and no state file, so a supervised watcher would look
        # installed while putting Muninn in no menubar at all.
        argv = list(agent_install.spec().argv)
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:], ["-m", "muninn.cli", "serve"])
        self.assertNotIn("--watch", argv)
        self.assertNotIn("index", argv)

    def test_argv_does_not_suppress_the_menubar(self) -> None:
        # Being in the shared menubar whenever the machine is up is the reason to
        # install this at all; --no-menubar would install the half that is
        # invisible.
        self.assertNotIn("--no-menubar", list(agent_install.spec().argv))

    def test_the_argv_is_a_command_the_cli_actually_accepts(self) -> None:
        # The one defect in an argv that reading cannot catch: `serve` is a string
        # here and a subparser name over there, so a renamed verb would leave this
        # looking correct while every login started a process that exits 2.
        #
        # Checked against the real parser in-process rather than by spawning
        # `python -m muninn.cli`. A subprocess would additionally prove `-m`
        # resolves, but subprocess tests wedge on the Windows runner rather than
        # failing (WINDOWS.md), which is strictly worse — and the module path is
        # covered by `test_the_module_path_in_argv_is_importable` below without
        # one.
        argv = list(agent_install.spec().argv)
        parsed = cli.build_parser().parse_args(argv[3:])
        self.assertIs(parsed.func, cli.cmd_serve)
        self.assertFalse(parsed.no_menubar)

    def test_the_module_path_in_argv_is_importable(self) -> None:
        # `-m muninn.cli` is resolved by import machinery at login, so a package
        # or module rename would leave the literal string looking right.
        import importlib.util

        self.assertEqual(list(agent_install.spec().argv)[1], "-m")
        module = list(agent_install.spec().argv)[2]
        self.assertIsNotNone(importlib.util.find_spec(module), module)

    def test_working_dir_matches_what_the_daemon_records_as_repo(self) -> None:
        # An installed unit whose WorkingDirectory disagrees with the running
        # daemon's reported `repo` is unanswerable from `doctor` alone: the two
        # would name different checkouts with nothing to say which is live.
        state_path = self.tmp / "daemon.json"
        daemon.write_state(None, path=state_path)
        import json

        recorded = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(agent_install.spec().working_dir, recorded["repo"])
        self.assertEqual(list(agent_install.spec().argv)[0], recorded["python"])

    def test_the_log_goes_under_muninns_own_state_dir(self) -> None:
        # Never the shared ravens directory. That one is a cross-project contract
        # holding descriptors; a log file there would sit next to Huginn's.
        log = agent_install.log_path()
        self.assertEqual(log.parent, paths.STATE_DIR)
        self.assertNotIn("ravens", str(log))

    def test_the_log_path_is_read_at_call_time(self) -> None:
        # A module-level constant would snapshot STATE_DIR at import and ignore
        # every override — which is how an install writes one developer's paths
        # into somebody else's LaunchAgents directory (Huginn's issue #37).
        first = agent_install.spec().log_path
        paths.STATE_DIR = self.tmp / "elsewhere"
        self.assertNotEqual(agent_install.spec().log_path, first)

    def test_derived_locations_are_left_to_corvidae(self) -> None:
        # plist_path/unit_path unset means corvidae derives them, including
        # reading $XDG_CONFIG_HOME per call. A constant computed at import here
        # would lose that.
        spec = agent_install.spec()
        self.assertIsNone(spec.plist_path)
        self.assertIsNone(spec.unit_path)
        self.assertEqual(spec.plist.name, f"{agent_install.LABEL}.plist")
        self.assertEqual(spec.unit.name, agent_install.UNIT_NAME)

    def test_the_unit_follows_xdg_config_home(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.tmp / "cfg")}):
            self.assertEqual(agent_install.spec().unit,
                             self.tmp / "cfg" / "systemd" / "user" / "muninn.service")

    def test_no_tray_registry_value_is_declared(self) -> None:
        # Muninn ships no tray. Appistry is the shared menubar host and registers
        # itself through a Start Menu Startup shortcut, not the Run key, and it
        # only reads Muninn's descriptor — it never starts or stops `muninn
        # serve`. Inventing a value name here would make an unrelated key's
        # presence refuse a valid install (corvidae's tray_owns_startup).
        self.assertEqual(agent_install.spec().tray_registry_value, "")


# ── Coexistence: the point of the exercise ────────────────────────────────────

class CoexistenceTest(unittest.TestCase):
    """Both ravens must be installable at once. Every collision is silent.

    A shared menubar with one raven in it is the failure this class exists to
    prevent, and none of these collisions produces an error: an overlapping plist
    path means the second install overwrites the first, an overlapping launchd
    label means two agents contend for one identity, and an overlapping Run value
    means whichever ran last is the only one that starts. The user's symptom in
    every case is "the other one stopped starting at login", months later.
    """

    def setUp(self) -> None:
        self.spec = agent_install.spec()

    def test_the_launchd_label_differs(self) -> None:
        self.assertNotEqual(self.spec.label, HUGINN_LABEL)

    def test_the_plist_path_differs(self) -> None:
        # Derived from the label, so this is the label test's consequence — and
        # worth asserting separately, because it is the file that gets clobbered.
        self.assertNotEqual(self.spec.plist, HUGINN_PLIST)

    def test_the_systemd_unit_name_and_path_differ(self) -> None:
        self.assertNotEqual(self.spec.unit_name, HUGINN_UNIT_NAME)
        self.assertNotEqual(self.spec.unit.name, HUGINN_UNIT_NAME)

    def test_the_log_path_differs(self) -> None:
        # Two daemons appending to one launchd log interleave, and neither's
        # output is then attributable — a debugging failure rather than a
        # lifecycle one, but the same silent kind.
        self.assertNotEqual(self.spec.log_path, HUGINN_LOG)

    def test_the_windows_run_value_differs_from_both_of_huginns(self) -> None:
        # Huginn owns two: its daemon's and its tray's. Muninn must collide with
        # neither, and overwriting the *tray's* would be the worse of the two
        # since the user would lose Huginn's menu bar entirely.
        self.assertNotEqual(self.spec.run_value, HUGINN_DAEMON_RUN_VALUE)
        self.assertNotEqual(self.spec.run_value, HUGINN_TRAY_RUN_VALUE)

    def test_the_backup_tag_differs(self) -> None:
        # Both projects back up into the same LaunchAgents directory. A shared tag
        # would not collide (the timestamp differs) but would make the directory
        # unreadable as to which project a backup came from.
        self.assertNotEqual(self.spec.backup_tag, HUGINN_BACKUP_TAG)

    def test_every_named_location_is_disjoint_at_once(self) -> None:
        # One assertion over the whole set, so a *future* field that happens to
        # collide is caught by this test rather than by needing a new one. The set
        # sizes are compared rather than the members, which is what makes it
        # indifferent to how many fields there are.
        muninn_values = {
            str(self.spec.label), str(self.spec.plist), str(self.spec.unit),
            str(self.spec.log_path), str(self.spec.run_value), str(self.spec.backup_tag),
            str(self.spec.unit_name),
        }
        huginn_values = {
            HUGINN_LABEL, str(HUGINN_PLIST), HUGINN_UNIT_NAME, str(HUGINN_LOG),
            HUGINN_DAEMON_RUN_VALUE, HUGINN_TRAY_RUN_VALUE, HUGINN_BACKUP_TAG,
        }
        self.assertEqual(muninn_values & huginn_values, set())

    def test_muninns_own_values_do_not_collide_with_each_other(self) -> None:
        # A copy-paste that left `registry_value` equal to `name`, say, would make
        # install and uninstall disagree about what they touch.
        self.assertNotEqual(self.spec.run_value, self.spec.name)
        self.assertNotEqual(str(self.spec.plist), str(self.spec.unit))


# ── The launchd backend, on macOS ─────────────────────────────────────────────

class LaunchdTest(_TempState):
    """macOS behaviour, including the KeepAlive that is deliberately kept."""

    def _agent(self, plist: Path, launchctl) -> LaunchdAgent:
        spec = LoginAgentSpec(**{**_spec_fields(), "plist_path": plist})

        class _Agent(LaunchdAgent):
            def launchctl(self, *args):
                return launchctl(*args)

        return _Agent(spec)

    def test_keepalive_survives_into_muninns_plist(self) -> None:
        # corvidae's contract, inherited rather than re-decided: a supervisor that
        # gives up on a crash is not a supervisor. Muninn has no app owning the
        # daemon lifecycle (Appistry reads the descriptor, it does not start the
        # daemon), so there is nothing here for KeepAlive to fight.
        parsed = _plist_of(agent_install.spec())
        self.assertIs(parsed["KeepAlive"], True)
        self.assertIs(parsed["RunAtLoad"], True)
        self.assertEqual(parsed["Label"], agent_install.LABEL)
        self.assertEqual(parsed["ProgramArguments"][1:], ["-m", "muninn.cli", "serve"])

    def test_the_plist_is_built_by_plistlib_not_a_template(self) -> None:
        # corvidae's C3 hardening, verified through Muninn's own spec (finding C3
        # of the security review of Huginn's #41, not of #41's own scope):
        # a working directory carrying XML must land as inert data in the one value
        # it belongs to, adding no keys. Before the fix upstream, an equivalent
        # payload injected a live DYLD_INSERT_LIBRARIES dict.
        payload = ("/tmp/x</string><key>EnvironmentVariables</key><dict>"
                   "<key>DYLD_INSERT_LIBRARIES</key><string>/tmp/evil.dylib</string>"
                   "</dict><key>Ignored</key><string>y")
        with patch.object(agent_install, "REPO_ROOT", payload):
            parsed = _plist_of(agent_install.spec())
        self.assertNotIn("EnvironmentVariables", parsed)
        self.assertEqual(parsed["WorkingDirectory"], payload)

    def test_install_uninstall_round_trip(self) -> None:
        plist = self.tmp / "LaunchAgents" / f"{agent_install.LABEL}.plist"
        calls: list[tuple] = []

        def launchctl(*args):
            calls.append(args)
            # "list" failing is how corvidae learns the agent is not loaded.
            return _fail() if args[0] == "list" else _ok()

        agent = self._agent(plist, launchctl)
        self.assertFalse(agent.installed())
        with _quiet():
            self.assertEqual(agent.install(), 0)
        self.assertTrue(agent.installed())
        self.assertTrue(plist.is_file())
        self.assertEqual(calls[-1], ("load", "-w", str(plist)))

        with _quiet():
            self.assertEqual(agent.uninstall(), 0)
        self.assertFalse(agent.installed())
        self.assertFalse(plist.exists())

    def test_installed_reflects_the_filesystem_not_a_cached_answer(self) -> None:
        plist = self.tmp / f"{agent_install.LABEL}.plist"
        agent = self._agent(plist, lambda *a: _ok())
        self.assertFalse(agent.installed())
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        self.assertTrue(agent.installed())
        plist.unlink()
        self.assertFalse(agent.installed())

    def test_install_reports_a_failed_load_with_exit_1(self) -> None:
        agent = self._agent(self.tmp / "a.plist", lambda *a: _fail())
        with _quiet():
            self.assertEqual(agent.install(), 1)

    def test_uninstall_is_a_no_op_when_absent(self) -> None:
        calls: list[tuple] = []
        agent = self._agent(self.tmp / "absent.plist",
                            lambda *a: calls.append(a) or _ok())
        with _quiet():
            self.assertEqual(agent.uninstall(), 0)
        self.assertEqual(calls, [], "uninstall touched launchctl for an agent that is not there")

    @unittest.skipIf(sys.platform == "win32", "mode bits are meaningless on Windows")
    def test_the_published_plist_is_owner_only_under_a_permissive_umask(self) -> None:
        # corvidae writes 0600 via mkstemp before the replace. A umask of 0 is
        # what makes that observable rather than merely claimed.
        plist = self.tmp / "modes.plist"
        agent = self._agent(plist, lambda *a: _ok())
        prior = os.umask(0)
        try:
            with _quiet():
                self.assertEqual(agent.install(), 0)
        finally:
            os.umask(prior)
        self.assertEqual(stat.S_IMODE(plist.stat().st_mode), 0o600)

    def test_an_existing_plist_is_backed_up_under_muninns_own_tag(self) -> None:
        plist = self.tmp / "backup.plist"
        plist.write_text("<plist>old</plist>")
        agent = self._agent(plist, lambda *a: _ok())
        with _quiet():
            self.assertEqual(agent.install(), 0)
        backups = list(self.tmp.glob(f"backup.plist.{agent_install.NAME}-bak.*"))
        self.assertEqual(len(backups), 1, list(self.tmp.iterdir()))
        self.assertEqual(backups[0].read_text(), "<plist>old</plist>")
        self.assertEqual(list(self.tmp.glob("*.tmp")), [])


# ── The systemd backend, exercised on macOS through the seam ──────────────────

class SystemdTest(_TempState):
    """Linux behaviour, verified on the dev host by overriding ``systemctl``."""

    def _agent(self, unit: Path, systemctl) -> SystemdUserAgent:
        spec = LoginAgentSpec(**{**_spec_fields(), "unit_path": unit})

        class _Agent(SystemdUserAgent):
            def systemctl(self, *args):
                return systemctl(*args)

        return _Agent(spec)

    def test_the_unit_restarts_on_failure_and_honours_a_deliberate_stop(self) -> None:
        # Not KeepAlive's semantics, on purpose: `systemctl --user stop muninn`
        # must stay effective. corvidae's contract, asserted through Muninn's
        # spec so a future consumer-side override would fail here.
        unit = _unit_of(agent_install.spec())
        self.assertIn("Restart=on-failure", unit)
        self.assertNotIn("Restart=always", unit)
        self.assertIn("WantedBy=default.target", unit)
        self.assertIn("-m muninn.cli serve", unit)

    def test_install_prints_the_enable_linger_caveat(self) -> None:
        # A user unit stops at logout without lingering, so a headless host would
        # silently lose the daemon it just installed. This is the one corvidae
        # message worth checking for, and it is checked as a substring rather than
        # a line: the *presence* of the caveat is the property, the wording is not.
        agent = self._agent(self.tmp / "muninn.service", lambda *a: _ok())
        with _quiet() as (out, _err):
            self.assertEqual(agent.install(), 0)
        self.assertIn("enable-linger", out.getvalue())

    def test_install_uninstall_round_trip(self) -> None:
        unit = self.tmp / "systemd" / "user" / "muninn.service"
        calls: list[tuple] = []
        agent = self._agent(unit, lambda *a: calls.append(a) or _ok())

        self.assertFalse(agent.installed())
        with _quiet():
            self.assertEqual(agent.install(), 0)
        self.assertTrue(agent.installed())
        self.assertEqual(calls, [("daemon-reload",),
                                 ("enable", "--now", "muninn.service")])

        calls.clear()
        with _quiet():
            self.assertEqual(agent.uninstall(), 0)
        self.assertFalse(unit.exists())
        self.assertEqual(calls, [("disable", "--now", "muninn.service"),
                                 ("daemon-reload",)])

    def test_install_reports_a_failed_enable_with_exit_1(self) -> None:
        agent = self._agent(self.tmp / "muninn.service",
                            _sequence(_ok(), _fail("unit is masked")))
        with _quiet():
            self.assertEqual(agent.install(), 1)

    def test_a_newline_in_the_checkout_path_is_refused_not_escaped(self) -> None:
        # corvidae's C3 hardening, inherited: \n ends a systemd
        # directive, so a path containing one injected arbitrary ones. Refusing
        # is right rather than escaping — this is a checkout path, so a value
        # with a newline in it is a broken install to report.
        with patch.object(agent_install, "REPO_ROOT", "/tmp/x\nExecStartPre=/bin/false"):
            with self.assertRaisesRegex(ValueError, "newline"):
                _unit_of(agent_install.spec())

    def test_a_percent_specifier_is_refused_rather_than_expanded(self) -> None:
        # systemd expands %h/%t at load time, so the unit would name something
        # other than the checkout meant.
        with patch.object(agent_install, "REPO_ROOT", "/home/%h/muninn"):
            with self.assertRaisesRegex(ValueError, "specifier"):
                _unit_of(agent_install.spec())

    def test_the_error_names_which_path_to_move(self) -> None:
        # This is what spec's program_label/working_dir_label are for: "the
        # program path contains a newline" is accurate and useless.
        with patch.object(sys, "executable", "/tmp/p\nExecStartPre=/bin/false"):
            with self.assertRaisesRegex(ValueError, "Python executable"):
                _unit_of(agent_install.spec())
        with patch.object(agent_install, "REPO_ROOT", "/tmp/x\rboom"):
            with self.assertRaisesRegex(ValueError, "Muninn checkout path"):
                _unit_of(agent_install.spec())

    def test_uninstall_refuses_a_symlinked_unit_path(self) -> None:
        # The unit path follows $XDG_CONFIG_HOME. A symlink there means the
        # installed agent is not the file we think it is, which is worth refusing
        # loudly rather than quietly tidying away.
        elsewhere = self.tmp / "elsewhere"
        elsewhere.write_text("not ours")
        unit = self.tmp / "muninn.service"
        unit.symlink_to(elsewhere)
        agent = self._agent(unit, lambda *a: _ok())
        with self.assertRaisesRegex(ValueError, "symlink"):
            agent.uninstall()
        self.assertTrue(elsewhere.exists())


# ── The Windows backend, exercised on macOS through the seam ──────────────────

class _FakeKey:
    def __init__(self, store: dict, name: str) -> None:
        self.store = store
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg:
    r"""Minimal HKCU\...\Run stand-in, so Windows is testable off Windows."""

    HKEY_CURRENT_USER = object()
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self, values: dict[str, str] | None = None, key_exists: bool = True) -> None:
        self.values = dict(values or {})
        self.key_exists = key_exists

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802 - winreg's own name
        if not self.key_exists:
            raise FileNotFoundError(path)
        return _FakeKey(self.values, path)

    def CreateKey(self, root, path):  # noqa: N802
        self.key_exists = True
        return _FakeKey(self.values, path)

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return (self.values[name], self.REG_SZ)

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def DeleteValue(self, key, name):  # noqa: N802
        del self.values[name]


class WindowsTest(_TempState):
    """Run-key behaviour, verified on the dev host by overriding ``registry``."""

    def _agent(self, winreg: _FakeWinreg) -> WindowsStartupAgent:
        spec = agent_install.spec()

        class _Agent(WindowsStartupAgent):
            def registry(self):
                return winreg

        return _Agent(spec)

    def test_install_uninstall_round_trip_on_muninns_own_value(self) -> None:
        winreg = _FakeWinreg()
        agent = self._agent(winreg)
        self.assertFalse(agent.installed())
        with _quiet():
            self.assertEqual(agent.install(), 0)
        self.assertTrue(agent.installed())
        self.assertEqual(list(winreg.values), [agent_install.DAEMON_RUN_VALUE])
        self.assertIn("muninn.cli serve", winreg.values[agent_install.DAEMON_RUN_VALUE])

        with _quiet():
            self.assertEqual(agent.uninstall(), 0)
        self.assertFalse(agent.installed())
        self.assertEqual(winreg.values, {})

    def test_huginns_run_values_are_left_alone(self) -> None:
        # The coexistence property, at the one place it is actually enforced by
        # code rather than by two constants differing.
        winreg = _FakeWinreg({HUGINN_DAEMON_RUN_VALUE: "huginn",
                              HUGINN_TRAY_RUN_VALUE: "tray"})
        agent = self._agent(winreg)
        with _quiet():
            self.assertEqual(agent.install(), 0)
            self.assertEqual(agent.uninstall(), 0)
        self.assertEqual(winreg.values, {HUGINN_DAEMON_RUN_VALUE: "huginn",
                                         HUGINN_TRAY_RUN_VALUE: "tray"})

    def test_no_tray_check_fires_because_muninn_ships_no_tray(self) -> None:
        # Huginn refuses to install while its tray owns startup. Muninn declares
        # no tray_registry_value, so corvidae must not consult the registry for
        # one — inventing a name would make an unrelated key refuse a valid
        # install. Even a key literally named "Muninn" must not block it.
        winreg = _FakeWinreg({"Muninn": "something else entirely"})
        agent = self._agent(winreg)
        self.assertFalse(agent.tray_owns_startup())
        with _quiet():
            self.assertEqual(agent.install(), 0)

    def test_a_missing_run_key_is_not_an_error(self) -> None:
        self.assertFalse(self._agent(_FakeWinreg(key_exists=False)).installed())

    def test_uninstall_is_a_no_op_when_absent(self) -> None:
        with _quiet():
            self.assertEqual(self._agent(_FakeWinreg()).uninstall(), 0)


# ── Platform dispatch ─────────────────────────────────────────────────────────

class DispatchTest(_TempState):
    """Every supported platform gets its own backend; the rest exit 2."""

    def test_each_platform_selects_its_own_backend(self) -> None:
        for name, expected in (("darwin", LaunchdAgent), ("linux", SystemdUserAgent),
                               ("win32", WindowsStartupAgent),
                               ("cygwin", WindowsStartupAgent)):
            with self.subTest(platform=name):
                self.assertIsInstance(agent_install.get_login_agent(name), expected)

    def test_an_unknown_platform_has_no_backend(self) -> None:
        # None rather than an exception: "this OS has no start-at-login mechanism
        # I know" is a thing to report with an exit code.
        self.assertIsNone(agent_install.get_login_agent("freebsd14"))

    def test_install_and_uninstall_exit_2_on_an_unsupported_platform(self) -> None:
        with patch.object(agent_install.sys, "platform", "freebsd14"), _quiet():
            self.assertEqual(agent_install.install(), 2)
            self.assertEqual(agent_install.uninstall(), 2)

    def test_every_backend_carries_muninns_spec(self) -> None:
        for name in ("darwin", "linux", "win32"):
            with self.subTest(platform=name):
                self.assertEqual(agent_install.get_login_agent(name).spec.label,
                                 agent_install.LABEL)

    def test_config_location_names_the_right_thing_per_backend(self) -> None:
        self.assertTrue(agent_install.config_location(
            agent_install.get_login_agent("darwin")).endswith(".plist"))
        self.assertTrue(agent_install.config_location(
            agent_install.get_login_agent("linux")).endswith("muninn.service"))
        self.assertIn(agent_install.DAEMON_RUN_VALUE, agent_install.config_location(
            agent_install.get_login_agent("win32")))


# ── The lock interaction: install must not create a crash loop ────────────────

class LockInteractionTest(_TempState):
    """Installing while an ingest loop runs would hand the user a restart loop.

    The lock already stops two loops from double-ingesting; what it cannot stop is
    a supervisor reacting to the exit code. launchd's ``KeepAlive`` relaunches a
    daemon that exits 1 forever; systemd's ``Restart=on-failure`` does the same
    until it gives up and leaves the unit failed. Either way the user asked for a
    service and got a log full of "already running", so install refuses instead —
    exit 1, nothing written, the same shape as corvidae's Windows tray refusal.
    """

    def _held_by(self, holder: str) -> None:
        lock = daemon.SingleInstance(daemon.lock_path(), holder=holder)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)

    def test_install_refuses_while_index_watch_holds_the_lock(self) -> None:
        agent = self.redirect_agent()
        self._held_by(daemon.HOLDER_WATCH)
        with _quiet() as (_o, err):
            self.assertEqual(agent_install.install(), 1)
        self.assertIn("already running", err.getvalue())
        self.assertIn(str(os.getpid()), err.getvalue())
        self.assertEqual(agent.calls, [], "the refusal still shelled out to launchctl")

    def test_the_refusal_writes_nothing(self) -> None:
        # A refusal that had already published the plist would be worse than no
        # refusal: the user would have both a warning and a crash loop.
        agent = self.redirect_agent()
        self._held_by(daemon.HOLDER_SERVE)
        with _quiet():
            self.assertEqual(agent_install.install(), 1)
        self.assertFalse(agent.installed())

    def test_install_proceeds_once_the_lock_is_free(self) -> None:
        # The other half of the refusal, so a check that refused *always* would
        # fail here rather than looking like a working guard.
        agent = self.redirect_agent()
        with _quiet():
            self.assertEqual(agent_install.install(), 0)
        self.assertTrue(agent.installed())

    def test_install_refuses_while_serve_holds_the_lock_and_nothing_is_installed(self) -> None:
        # Nothing a supervisor started can be holding the lock if no agent is
        # installed, so something else is, and installing now would fight it.
        agent = self.redirect_agent()
        self._held_by(daemon.HOLDER_SERVE)
        self.assertFalse(agent.installed())
        self.assertIsNotNone(agent_install.conflicting_ingest_loop(agent))

    def test_reinstalling_over_an_installed_agents_own_daemon_is_allowed(self) -> None:
        # The one time a refresh is most needed — the checkout moved, or the
        # interpreter changed — is while the agent's own daemon is running.
        # Refusing here would make `install-agent` impossible to re-run.
        self._held_by(daemon.HOLDER_SERVE)
        plist = self.tmp / "installed.plist"
        plist.write_text("<plist/>")
        spec = LoginAgentSpec(**{**_spec_fields(), "plist_path": plist})
        agent = LaunchdAgent(spec)
        self.assertTrue(agent.installed())
        self.assertIsNone(agent_install.conflicting_ingest_loop(agent))

    def test_a_watch_holder_conflicts_even_with_an_agent_installed(self) -> None:
        # `index --watch` is by definition not the agent's daemon (the agent runs
        # `serve`), so a crash loop is certain rather than merely possible.
        self._held_by(daemon.HOLDER_WATCH)
        plist = self.tmp / "installed.plist"
        plist.write_text("<plist/>")
        agent = LaunchdAgent(LoginAgentSpec(**{**_spec_fields(), "plist_path": plist}))
        self.assertEqual(agent_install.conflicting_ingest_loop(agent)[1],
                         daemon.HOLDER_WATCH)

    def test_a_free_lock_is_no_conflict(self) -> None:
        self.assertIsNone(agent_install.conflicting_ingest_loop(self.redirect_agent()))

    def test_an_unknown_lock_state_is_treated_as_no_conflict(self) -> None:
        # probe() returns None (not False) when there is no locking primitive or
        # the lock file cannot be opened. Matching the daemon's own fail-open
        # (spec 010) is deliberate: inverting it would make `install-agent`
        # impossible wherever the guard is unenforceable, which is worse than a
        # possible double-start.
        agent = self.redirect_agent()
        with patch.object(daemon.SingleInstance, "probe",
                          return_value=(None, 999, daemon.HOLDER_SERVE)):
            self.assertIsNone(agent_install.conflicting_ingest_loop(agent))

    def test_uninstall_does_not_check_the_lock(self) -> None:
        # Removing the agent while its daemon runs is the normal case, and the
        # backend stopping it is the point rather than a hazard.
        self._held_by(daemon.HOLDER_SERVE)
        with patch.object(agent_install.sys, "platform", "freebsd14"), _quiet():
            # Exit 2 is the unsupported-platform path, which proves uninstall got
            # as far as backend selection rather than short-circuiting on a lock.
            self.assertEqual(agent_install.uninstall(), 2)


# ── `doctor` ──────────────────────────────────────────────────────────────────

class DoctorTest(_TempState):
    """The daemon section gains a fourth fact: will this come back by itself.

    Every test here redirects the backend. `doctor` reads ``installed()``, so
    against the real plist path the verdict would differ between a developer who
    has run `muninn install-agent` and one who has not — a suite whose colour
    depends on the machine proves nothing.
    """

    def setUp(self) -> None:
        super().setUp()
        self.agent = self.redirect_agent()

    def _report(self) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_daemon_section()
        return buffer.getvalue()

    def test_not_installed_names_the_verb_that_installs_it(self) -> None:
        report = self._report()
        self.assertIn("at login", report)
        self.assertIn("not installed", report)
        self.assertIn("muninn install-agent", report)

    def test_installed_reports_the_mechanism_and_the_path(self) -> None:
        # The path, not just the verdict: "installed" while the file lives in a
        # redirected $XDG_CONFIG_HOME the reader is not looking at is the same
        # invisible mismatch the descriptor line exists to prevent.
        plist = self.agent.spec.plist
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text("<plist/>")
        report = self._report()
        self.assertIn("installed · LaunchAgent", report)
        self.assertIn(str(plist), report)

    def test_it_lives_in_the_existing_daemon_section(self) -> None:
        # Extended, not competing: a reader who has to correlate two sections to
        # learn the daemon is up *but* nothing will restart it has been given a
        # puzzle rather than a report.
        report = self._report()
        heading = report.index("daemon (`muninn serve`)")
        self.assertLess(heading, report.index("at login"))
        self.assertLess(report.index("at login"), report.index("lock "))

    def test_an_unsupported_platform_says_so_rather_than_omitting_the_line(self) -> None:
        # Omitting it would let the reader assume "not installed", which is a
        # different fact with a different remedy.
        with patch.object(agent_install, "get_login_agent", return_value=None):
            self.assertIn("no start-at-login mechanism", self._report())

    def test_the_line_survives_a_stale_state_file(self) -> None:
        # The crashed-daemon path returns early, and it is precisely where "is
        # anything going to restart it" is the most useful line on screen. Ordered
        # after the lock, the answer would vanish exactly when it matters.
        import json

        (paths.STATE_DIR).mkdir(parents=True, exist_ok=True)
        (paths.STATE_DIR / "daemon.json").write_text(
            json.dumps({"pid": 2 ** 30, "port": 1, "started": 0.0}), encoding="utf-8")
        report = self._report()
        self.assertIn("stale", report.lower())
        self.assertIn("at login", report)


# ── The CLI verbs ─────────────────────────────────────────────────────────────

class CliTest(unittest.TestCase):
    """`install-agent` / `uninstall-agent` exist, dispatch, and return exit codes."""

    def test_both_verbs_are_registered(self) -> None:
        parser = cli.build_parser()
        for verb in ("install-agent", "uninstall-agent"):
            with self.subTest(verb=verb):
                args = parser.parse_args([verb])
                self.assertTrue(callable(args.func))

    def test_each_verb_returns_its_functions_exit_code(self) -> None:
        # Every code the installer can produce, relayed unchanged. `main()` casts
        # to int, and a verb that swallowed a 1 into a 0 would tell a supervisor
        # the install worked.
        for code in (0, 1, 2):
            with self.subTest(code=code):
                with patch.object(agent_install, "install", return_value=code):
                    self.assertEqual(cli.main(["install-agent"]), code)
                with patch.object(agent_install, "uninstall", return_value=code):
                    self.assertEqual(cli.main(["uninstall-agent"]), code)

    def test_the_verbs_match_huginns_names(self) -> None:
        # Someone running both ravens should learn one word, which is the same
        # reason `serve` is called `serve` (docs/specs/010-daemon.md).
        help_text = cli.build_parser().format_help()
        self.assertIn("install-agent", help_text)
        self.assertIn("uninstall-agent", help_text)


# ── The environment the service will not inherit (tohuw/muninn#7) ─────────────

class EnvironmentMismatchTest(unittest.TestCase):
    """What this shell resolves versus what a login session will.

    Deliberately NOT a ``_TempState`` subclass: that base pins
    ``environment_mismatch`` to "agrees" so the rest of the suite is not hostage
    to the developer's exported variables, and inheriting it here would test the
    stub. These drive the real function with synthetic environments instead,
    which is also the only way to exercise a redirected ``$HOME`` without
    redirecting the one running the tests.
    """

    def setUp(self) -> None:
        # The home a login session gets. Every "clean" environment below states
        # it explicitly, because an env dict with no HOME at all falls back to
        # Path.home() and would quietly agree for the wrong reason.
        self.home = agent_install._account_home() or Path.home()

    def _clean(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), **extra}

    def test_a_clean_environment_diverges_about_nothing(self) -> None:
        # The other half of every assertion below: a check that reported a
        # mismatch always would refuse every install on every machine.
        self.assertFalse(agent_install.environment_mismatch(self._clean()))

    def test_a_redirected_state_home_moves_the_archive_and_the_descriptor(self) -> None:
        mismatch = agent_install.environment_mismatch(
            self._clean(XDG_STATE_HOME="/tmp/muninn-elsewhere"))
        self.assertTrue(mismatch)
        self.assertIn("XDG_STATE_HOME", mismatch.variables)
        moved = {d.what for d in mismatch.paths}
        self.assertIn("archive and queue", moved)
        # Both, because raven.state_dir reads XDG_STATE_HOME too. A report naming
        # only the archive would leave the user to discover the descriptor half
        # by finding Muninn missing from the menubar.
        self.assertIn("raven descriptor", moved)

    @unittest.skipIf(sys.platform == "win32", "XDG_STATE_HOME is POSIX-only in paths.py")
    def test_a_variable_set_to_the_default_is_not_a_divergence(self) -> None:
        # The precision that makes this usable. Exporting XDG paths explicitly is
        # common and correct; refusing on the *variable* rather than on the
        # resolved *path* would make Muninn uninstallable for those users while
        # catching nothing this misses.
        self.assertFalse(agent_install.environment_mismatch(
            self._clean(XDG_STATE_HOME=str(self.home / ".local/state"))))

    def test_the_ravens_dir_moves_the_descriptor_alone(self) -> None:
        # Scoped, not blanket: RAVENS_STATE_DIR does not move the archive, and a
        # warning that said it did would send someone looking for a database
        # that never moved.
        mismatch = agent_install.environment_mismatch(
            self._clean(RAVENS_STATE_DIR="/tmp/ravens-elsewhere"))
        self.assertEqual([d.what for d in mismatch.paths], ["raven descriptor"])
        self.assertEqual(mismatch.variables, ("RAVENS_STATE_DIR",))

    def test_codex_home_moves_only_the_codex_transcripts(self) -> None:
        mismatch = agent_install.environment_mismatch(
            self._clean(CODEX_HOME="/tmp/codex-elsewhere"))
        self.assertEqual([d.what for d in mismatch.paths], ["codex transcripts"])

    def test_a_redirected_home_moves_everything_and_is_named(self) -> None:
        # The case the sandboxed trial in the issue actually hit. $HOME is not in
        # BLIND_VARS — it is always set — so it has to be caught by comparing
        # against the account's real home rather than by looking for a variable.
        mismatch = agent_install.environment_mismatch({"HOME": "/tmp/not-my-home"})
        self.assertIn("HOME", mismatch.variables)
        moved = {d.what for d in mismatch.paths}
        self.assertEqual(moved, {"archive and queue", "raven descriptor",
                                 "claude transcripts", "codex transcripts"})

    def test_an_unresolvable_account_never_flags_a_redirected_home(self) -> None:
        # Failing towards a missed warning rather than towards a refusal nobody
        # can clear: a container with no passwd entry must still be installable.
        with patch.object(agent_install, "_account_home", return_value=None):
            self.assertFalse(agent_install.environment_mismatch({"HOME": "/tmp/anywhere"}))

    def test_a_db_the_service_will_never_open_is_reported(self) -> None:
        # `muninn --db X install-agent` installs a unit that runs a bare
        # `muninn serve`, so X reaches nothing. Same silent mismatch, different
        # route in.
        mismatch = agent_install.environment_mismatch(self._clean(), db="/tmp/other.db")
        self.assertIn("--db", mismatch.variables)
        self.assertIn("archive named by --db", {d.what for d in mismatch.paths})

    def test_the_db_the_service_will_actually_open_is_not_reported(self) -> None:
        default = agent_install.paths.state_dir(self._clean(), self.home) / "muninn.db"
        self.assertFalse(agent_install.environment_mismatch(self._clean(), db=default))

    def test_os_provided_variables_are_not_treated_as_blind(self) -> None:
        # LOCALAPPDATA and USERPROFILE are set by Windows for every login session,
        # so scrubbing them would manufacture a divergence out of a correctly
        # relocated profile. The list encodes "exported by a shell" versus
        # "provided by the OS", not "affects a path".
        self.assertNotIn("LOCALAPPDATA", agent_install.BLIND_VARS)
        self.assertNotIn("USERPROFILE", agent_install.BLIND_VARS)

    def test_the_rendering_shows_both_sides_of_every_path(self) -> None:
        # "Your archive is elsewhere" is not actionable without both halves.
        mismatch = agent_install.environment_mismatch(
            self._clean(XDG_STATE_HOME="/tmp/muninn-elsewhere"))
        text = "\n".join(agent_install.format_mismatch(mismatch))
        self.assertIn("XDG_STATE_HOME", text)
        self.assertIn("/tmp/muninn-elsewhere", text)
        self.assertIn(str(agent_install.paths.state_dir(self._clean(), self.home)), text)


class InstallEnvironmentTest(_TempState):
    """Install refuses a divergence, and ``--force`` proceeds through it."""

    def _diverging(self):
        return agent_install.EnvironmentMismatch(
            variables=("XDG_STATE_HOME",),
            paths=(agent_install.PathDivergence(
                "archive and queue", Path("/tmp/sandbox/muninn"),
                Path("/home/real/.local/state/muninn")),))

    def test_install_refuses_and_writes_nothing(self) -> None:
        # The failure this exists to stop is not an error — it is a daemon that
        # comes up every login and ingests the real archive while behaving
        # correctly by its own lights. So the refusal must land before anything
        # is published, exactly like the lock refusal.
        agent = self.redirect_agent()
        with patch.object(agent_install, "environment_mismatch",
                          return_value=self._diverging()), _quiet() as (_o, err):
            self.assertEqual(agent_install.install(), 1)
        self.assertFalse(agent.installed())
        self.assertEqual(agent.calls, [], "the refusal still shelled out to launchctl")
        self.assertIn("/tmp/sandbox/muninn", err.getvalue())
        self.assertIn("/home/real/.local/state/muninn", err.getvalue())

    def test_force_installs_and_still_prints_the_divergence(self) -> None:
        # The claim being forced is "I know, and I have arranged for login to
        # agree" — only meaningful if the reader saw what they agreed to.
        agent = self.redirect_agent()
        with patch.object(agent_install, "environment_mismatch",
                          return_value=self._diverging()), _quiet() as (_o, err):
            self.assertEqual(agent_install.install(force=True), 0)
        self.assertTrue(agent.installed())
        self.assertIn("/home/real/.local/state/muninn", err.getvalue())

    def test_force_is_not_needed_when_the_environment_agrees(self) -> None:
        agent = self.redirect_agent()
        with _quiet():
            self.assertEqual(agent_install.install(), 0)
        self.assertTrue(agent.installed())

    def test_the_cli_passes_force_and_the_archive_through(self) -> None:
        # A `--force` the parser accepts but the handler drops is worse than no
        # flag: the user believes they overrode a refusal that is still active.
        with patch.object(agent_install, "install", return_value=0) as installed:
            cli.main(["--db", "/tmp/named.db", "install-agent", "--force"])
        installed.assert_called_once_with(force=True, db="/tmp/named.db")

    def test_the_environment_check_runs_before_the_lock_check(self) -> None:
        # Both refuse with 1, so order is only observable in the message. The
        # environment is the more surprising of the two and the one the user
        # cannot see for themselves, so it leads.
        self.redirect_agent()
        lock = daemon.SingleInstance(daemon.lock_path(), holder=daemon.HOLDER_WATCH)
        self.assertTrue(lock.acquire())
        self.addCleanup(lock.release)
        with patch.object(agent_install, "environment_mismatch",
                          return_value=self._diverging()), _quiet() as (_o, err):
            self.assertEqual(agent_install.install(), 1)
        self.assertIn("at login", err.getvalue())
        self.assertNotIn("already running", err.getvalue())


class DoctorEnvironmentTest(_TempState):
    """`doctor` catches the case a refusal cannot: an environment changed later."""

    def setUp(self) -> None:
        super().setUp()
        self.agent = self.redirect_agent()

    def _report(self) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._print_daemon_section()
        return buffer.getvalue()

    def _install(self) -> None:
        self.agent.spec.plist.parent.mkdir(parents=True, exist_ok=True)
        self.agent.spec.plist.write_text("<plist/>")

    def _diverging(self):
        return agent_install.EnvironmentMismatch(
            variables=("RAVENS_STATE_DIR",),
            paths=(agent_install.PathDivergence(
                "raven descriptor", Path("/tmp/sandbox/ravens"),
                Path("/home/real/.local/state/ravens")),))

    def test_an_installed_agent_with_a_diverging_environment_warns(self) -> None:
        self._install()
        with patch.object(agent_install, "environment_mismatch",
                          return_value=self._diverging()):
            report = self._report()
        self.assertIn("WARNING", report)
        self.assertIn("/home/real/.local/state/ravens", report)

    def test_no_warning_when_nothing_is_installed(self) -> None:
        # With no agent there is no second environment to disagree with, and a
        # redirected shell is then simply a redirected shell — which is what the
        # rest of this suite runs in.
        with patch.object(agent_install, "environment_mismatch",
                          return_value=self._diverging()):
            self.assertNotIn("WARNING", self._report())

    def test_no_warning_when_the_environment_agrees(self) -> None:
        self._install()
        self.assertNotIn("WARNING", self._report())


# ── Helpers ───────────────────────────────────────────────────────────────────

def _spec_fields() -> dict:
    """Muninn's spec as a kwargs dict, so a test can override one field.

    Reads the real ``spec()`` rather than restating its values: a test fixture
    that duplicated them would keep passing after the real spec changed, which is
    the whole failure mode ``SpecTest`` exists to catch.
    """
    from dataclasses import fields

    spec = agent_install.spec()
    return {f.name: getattr(spec, f.name) for f in fields(spec)}


def _plist_of(spec: LoginAgentSpec) -> dict:
    """Parse the plist corvidae would write for ``spec``.

    Goes through a real ``install()`` into a tempdir rather than calling
    corvidae's private ``_plist_xml``: the underscore names carry no stability
    promise, and what is worth asserting is the file that actually lands.
    """
    with tempfile.TemporaryDirectory() as tmp:
        plist = Path(tmp) / "probe.plist"
        from dataclasses import replace

        class _Agent(LaunchdAgent):
            def launchctl(self, *args):
                return _ok()

        with _quiet():
            _Agent(replace(spec, plist_path=plist)).install()
        return plistlib.loads(plist.read_bytes())


def _unit_of(spec: LoginAgentSpec) -> str:
    """The systemd unit corvidae would write for ``spec``. Same reasoning as above."""
    with tempfile.TemporaryDirectory() as tmp:
        unit = Path(tmp) / "probe.service"
        from dataclasses import replace

        class _Agent(SystemdUserAgent):
            def systemctl(self, *args):
                return _ok()

        with _quiet():
            _Agent(replace(spec, unit_path=unit)).install()
        return unit.read_text()


def _sequence(*results):
    """A ``systemctl``/``launchctl`` stand-in returning each result in turn."""
    queue = list(results)

    def call(*_args):
        return queue.pop(0) if queue else _ok()

    return call


if __name__ == "__main__":
    unittest.main()
