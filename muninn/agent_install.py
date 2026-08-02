"""Start-at-login installation for `muninn serve`, via the shared `corvidae` seam.

Normative source: docs/specs/010-daemon.md, "Follow-up seam: `corvidae`". Spec 010
built the *supervised* half — a state file, a clean SIGTERM/SIGHUP teardown, and a
single-instance lock — and deliberately left the *supervisor installer* out, so
that the launchd/systemd/Run-key mechanism could be written once and consumed
twice. This module is Muninn's half of that: **only** the things that are
Muninn's, which is the label, the argv, where the log goes, and the Windows Run
value nothing else owns.

## Nothing here implements launchd, systemd, or the registry

`corvidae.login_agent` does, and the hardening it carries came from a security
review of Huginn's issue #41 — of the surface that change added, not of its own
scope, which was the model-policy chokepoint — with a working exploit behind each
item: the plist is built by
``plistlib.dumps`` of a real dict rather than formatted into an XML template (a
directory name containing XML injected live launchd keys), systemd refuses
``\\n``/``\\r``/``%`` in any value rather than escaping them, config is written via
``mkstemp`` + ``os.replace`` at 0600 with a 0600 backup taken *before* any content
lands, and a symlink at either the target or the temp path is refused outright.

Naming the mistake a reader of this file might make: none of that is re-stated
here as a local check, and adding one would be a defect rather than
belt-and-braces. Two copies of a security property drift, and the copy a reader
finds first is then the one they trust. The single implementation is corvidae's;
this module's job is to hand it a correct ``LoginAgentSpec`` and get out of the
way.

## Coexistence with Huginn is a property, not an aspiration

Both ravens are meant to be installed at once — that is the entire point of a
*shared* menubar (.valholl/articles/shared-menubar.md). So every value in
:func:`spec` that names a file, a launchd label, or a registry value has to differ
from Huginn's, and the failure if one collides is not an error message: it is one
install silently overwriting the other's autostart config, discovered as "why did
Huginn stop starting at login". ``tests/test_agent_install.py::CoexistenceTest``
pins the disjointness against Huginn's literal values.

## The restart policy differs per OS, and that is corvidae's contract

launchd keeps ``KeepAlive`` (a supervisor that gives up on a crash is not a
supervisor), systemd uses ``Restart=on-failure`` so ``systemctl --user stop``
stays effective, and the Windows Run key starts a process once per login and
never restarts it. Muninn does not override any of the three, and the reason is
the same as for the hardening above: they were chosen with reasons recorded, and
a consumer quietly picking different ones would make "what supervises Muninn"
unanswerable without reading two packages.

## Why install refuses while an ingest loop is already running

See :func:`conflicting_ingest_loop`. Short version: `muninn serve` exits 1 when
the single-instance lock is held (spec 010), and a supervisor whose service exits
1 forever is a crash loop, not a service.

## The installed agent does not inherit the installing shell's environment

Measured, because it is the one thing about this that surprises: launchd starts
the agent from its own environment, not from the shell that ran
`muninn install-agent`. A ``$XDG_STATE_HOME`` or ``$RAVENS_STATE_DIR`` exported in
a terminal is therefore **absent** at login, and the daemon resolves both to their
defaults. Verified directly during this change: an install run with every path
redirected to a tempdir produced a daemon that ingested into the real
``~/.local/state/muninn/muninn.db``, which is a startling thing to discover from a
log file.

This is not worked around, and adding an ``EnvironmentVariables`` dict to capture
the installing shell would be the wrong fix twice over: it would bake one
terminal's transient state into config that runs at every login for years, and it
is the exact key that the XML injection created out of a directory name (finding
C3 of that same review; see above for why the review and #41 are not the same
thing). A user who genuinely relocates Muninn's state sets the variable
where login sessions see it (``launchctl setenv``, a systemd user environment
drop-in) — which is a statement about their machine rather than about this
install. Recorded in ``docs/specs/010-daemon.md`` so it is discoverable before
someone spends an afternoon on it.
"""
from __future__ import annotations

import sys
from pathlib import Path

from corvidae.login_agent import (
    RUN_KEY,
    LaunchdAgent,
    LoginAgent,
    LoginAgentSpec,
    SystemdUserAgent,
)
from corvidae.login_agent import get_login_agent as _select_backend

from . import daemon, paths

#: Muninn's launchd label, and the plist filename corvidae derives from it.
#: Reverse-DNS in the same style as Huginn's ``is.tohuw.huginn`` and **distinct
#: from it by construction** — two LaunchAgents sharing a label share a plist
#: path, so the second install would overwrite the first with nothing on screen
#: to say so.
LABEL = "is.tohuw.muninn"

#: Short project name. corvidae derives ``muninn.service`` and the
#: ``.muninn-bak.<ts>`` backup infix from it.
NAME = "muninn"
UNIT_NAME = f"{NAME}.service"

#: The ``HKCU\\...\\Run`` value name Muninn's daemon owns.
#:
#: There is deliberately no ``tray_registry_value`` counterpart. Huginn sets one
#: because ``windows/Huginn.Tray`` registers its own "Start at login" item in the
#: Run key and *supervises Huginn's daemon* — so a second autostart there would
#: resurrect a daemon the user just quit. Muninn ships no tray of its own:
#: Appistry is the shared menubar host, it registers itself through a Start Menu
#: Startup shortcut rather than the Run key, and it only *reads* Muninn's raven
#: descriptor — it never starts or stops `muninn serve`. So there is no Run value
#: to defer to, and inventing one would make an unrelated key's presence refuse a
#: valid install (corvidae's ``tray_owns_startup`` says exactly this).
DAEMON_RUN_VALUE = "MuninnDaemon"

#: Where the daemon is relaunched from. **The same expression
#: ``daemon.write_state`` records as ``repo``**, and the same caveat applies:
#: from a checkout this is the repo root, from an installed wheel it is
#: site-packages. Keeping the two identical is not cosmetic — an installed agent
#: whose working directory disagrees with what the running daemon reports is
#: unanswerable from `muninn doctor` alone, and
#: ``tests/test_agent_install.py::SpecTest`` asserts the two match rather than
#: leaving it to a comment.
REPO_ROOT = Path(__file__).resolve().parent.parent


def log_path() -> Path:
    """launchd's ``StandardOutPath``/``StandardErrorPath`` for the daemon.

    Under **Muninn's own** state directory (``muninn/paths.py``), never the shared
    ravens directory — that one is a cross-project contract holding descriptors,
    and writing a log into it would put Muninn's stdout next to Huginn's
    descriptor. ``paths.STATE_DIR`` is read at call time rather than captured at
    import, matching ``daemon.state_path`` so a test can redirect both by patching
    one name.

    systemd ignores this and uses the journal (``journalctl --user -u
    muninn.service``); the Run key ignores it too. It is macOS-only, which is
    corvidae's documented behaviour rather than an omission here.
    """
    return paths.STATE_DIR / "agent.log"


def spec() -> LoginAgentSpec:
    """Muninn's login-agent description, built per call.

    Per call rather than frozen at import for the reason Huginn records at its own
    ``spec()``: every ingredient is something a test or a relocated checkout
    overrides — ``paths.STATE_DIR`` via :func:`log_path`, ``sys.executable``, and
    ``$XDG_CONFIG_HOME`` inside corvidae's ``unit`` property. A module-level spec
    would snapshot all of them at import and quietly ignore every override, which
    is how an install comes to write one developer's paths into somebody else's
    LaunchAgents directory.

    ``plist_path`` and ``unit_path`` are left unset on purpose, so corvidae
    derives them: ``~/Library/LaunchAgents/{label}.plist`` and
    ``$XDG_CONFIG_HOME/systemd/user/{name}.service``. Naming a location here
    would be a second copy of a rule the shared package already owns, and the
    ``$XDG_CONFIG_HOME`` half in particular is read per call there — a constant
    computed at import in this module would lose that.

    ``argv`` runs ``muninn serve``, not ``index --watch``: the descriptor, the
    ``/api/menu`` port and the state file all belong to `serve` since spec 010,
    and a supervised watcher that publishes none of them would put Muninn in no
    menubar while looking installed. No ``--no-menubar`` either — being in the
    menubar whenever the machine is up is the reason to install this at all.
    """
    return LoginAgentSpec(
        name=NAME,
        label=LABEL,
        # The interpreter and the checkout resolved exactly as
        # ``daemon.write_state`` resolves ``python`` and ``repo``, so the
        # installed unit and the running daemon's state file cannot disagree
        # about what is being run or from where.
        argv=[str(sys.executable), "-m", "muninn.cli", "serve"],
        working_dir=str(REPO_ROOT),
        log_path=log_path(),
        description="Muninn local AI agent-history archive",
        documentation="https://github.com/tohuw/muninn",
        registry_value=DAEMON_RUN_VALUE,
        # error wording: corvidae's systemd validator refuses a value it cannot
        # represent, and the user has to know which path to move.
        program_label="the Python executable",
        working_dir_label="the Muninn checkout path",
    )


def get_login_agent(name: str | None = None) -> LoginAgent | None:
    """corvidae's backend for a platform name (default: the host), bound to Muninn's spec.

    ``None`` on an unsupported platform rather than an exception: "this OS has no
    start-at-login mechanism I know" is a fact to report with an exit code, and
    `doctor` reports it too.
    """
    return _select_backend(spec(), name)


def config_location(agent: LoginAgent) -> str:
    """Where ``agent``'s installed config lives, for `doctor` to print.

    Not a corvidae API and deliberately not asked for as one: the three backends
    keep their location on ``spec`` under three different names because they *are*
    three different things (a plist path, a unit path, and a registry value), and
    a single ``location`` property on the shared spec would have to flatten them
    into a string that means something different per platform.

    The final branch is the Windows one *and* the fallback for any backend
    corvidae adds within a CalVer year — additive changes are promised, so an
    unrecognised backend must degrade to naming the run value rather than raise
    out of a health report.
    """
    if isinstance(agent, LaunchdAgent):
        return str(agent.spec.plist)
    if isinstance(agent, SystemdUserAgent):
        return str(agent.spec.unit)
    return rf"HKCU\{RUN_KEY}\{agent.spec.run_value}"


def conflicting_ingest_loop(agent: LoginAgent) -> tuple[int | None, str] | None:
    """``(pid, holder)`` of an ingest loop that would make this install a crash loop.

    The interaction spec 010 left for this seam to decide. The single-instance
    lock already prevents the *data* failure — two loops draining one queue and
    clobbering one descriptor — by making the second `muninn serve` exit
    ``EXIT_ALREADY_RUNNING`` (1). What the lock cannot prevent is what a
    *supervisor* does with that exit code:

    - **launchd** keeps ``KeepAlive``, so it relaunches a daemon that exits 1,
      forever, throttled to roughly one attempt every ten seconds. The visible
      result is ``agent.log`` filling with "another muninn ingest loop is already
      running" and a service that never comes up.
    - **systemd** uses ``Restart=on-failure``, and exit 1 *is* a failure, so it
      does the same until it hits ``StartLimitBurst`` and gives up — leaving a
      unit in ``failed`` state that nothing will retry.

    Both are crash loops caused by a healthy manually-started loop, which is a
    confusing thing to hand a user at the exact moment they asked for a service.
    So install refuses instead, exits 1, and writes nothing — the same shape and
    the same exit code as corvidae's Windows refusal when a tray already owns
    startup, and for the same double-owner reason.

    The rule, and why it is not simply "refuse whenever the lock is held":

    - **Holder is ``index --watch``** → always a conflict. That is a foreground
      debug watcher which by definition is not the agent's own daemon, so a crash
      loop is certain.
    - **Any holder, and the agent is not installed yet** → a conflict. Nothing
      the supervisor started can be holding the lock, so something else is.
    - **The agent is already installed** → *not* a conflict. The holder is almost
      certainly the agent's own daemon, and refusing here would make it
      impossible to re-run `install-agent` after moving the checkout or changing
      interpreters — the one time a refresh is most needed.

    A lock whose state is genuinely **unknown** (no locking primitive, or a lock
    file this user cannot open — ``probe()`` returns ``None``, never ``False``,
    for that) is treated as no conflict. That direction matches the daemon's own
    fail-open (spec 010: "No locking primitive ⇒ fail open"), and inverting it
    here would make `install-agent` impossible on a platform where the guard is
    unenforceable, which is a worse answer than a possible double-start.
    """
    held, pid, holder = daemon.SingleInstance.probe()
    if not held:
        return None
    if holder != daemon.HOLDER_WATCH and agent.installed():
        return None
    return pid, holder


def _unsupported() -> int:
    print(f"muninn: start-at-login is not supported on {sys.platform}", file=sys.stderr)
    return 2


def install() -> int:
    """Install the login agent for this host. Returns a process exit code.

    Exit codes are the contract, not the wording — corvidae states outright that
    each backend's printed text may change within a CalVer year, so nothing here
    or in the tests parses a message. 0 installed, 1 refused or the OS mechanism
    failed, 2 no mechanism on this platform.
    """
    agent = get_login_agent()
    if agent is None:
        return _unsupported()

    conflict = conflicting_ingest_loop(agent)
    if conflict is not None:
        pid, holder = conflict
        where = f"pid {pid}" if pid is not None else "an unrecorded pid"
        print(f"muninn: an ingest loop is already running ({where}, {holder})", file=sys.stderr)
        print(f"        installing a {agent.label} now would supervise a process that exits "
              f"immediately, because the single-instance lock is held — a restart loop, "
              f"not a service.", file=sys.stderr)
        print("        stop it first, then run `muninn install-agent` again. "
              "`muninn doctor` names what holds the lock.", file=sys.stderr)
        return 1

    return agent.install()


def uninstall() -> int:
    """Remove the login agent. Returns a process exit code.

    No lock check, deliberately: uninstalling while the daemon runs is the normal
    case, and ``launchctl unload -w`` / ``systemctl --user disable --now`` stopping
    it is the *point* rather than a side effect to guard against.
    """
    agent = get_login_agent()
    return agent.uninstall() if agent else _unsupported()


__all__ = [
    "DAEMON_RUN_VALUE",
    "LABEL",
    "NAME",
    "REPO_ROOT",
    "RUN_KEY",
    "UNIT_NAME",
    "config_location",
    "conflicting_ingest_loop",
    "get_login_agent",
    "install",
    "log_path",
    "spec",
    "uninstall",
]
