# Spec 010 — The daemon

**Status:** implemented.
**Depends on:** 003 (background indexer), 009 (raven descriptor and `/api/menu`).
**Read first:** `.valholl/articles/continuous-ingest-not-periodic.md` — normative
for *why* an always-on ingest loop exists at all, and for the measurement that
made a periodic one unacceptable. Then
`.valholl/articles/session-lifecycle-facts.md` ("Design consequences for the
indexer") for why the startup sweep is not optional, and
`.valholl/articles/shared-menubar.md` for the raven half.

## Why

Muninn had **no daemon**. Spec 009 said so in as many words and left the gap open
as an owner decision ("The lifecycle question"). Three facts made it a real
problem rather than a tidiness one:

1. **Every entry point was a one-shot CLI exit.** `fastapi` and `uvicorn` were
   declared dependencies that no module imported.
2. **The raven descriptor and `/api/menu` were published by `muninn index
   --watch`** — the only long-running process — so Muninn vanished from the
   shared menubar whenever nobody happened to be running a debug command.
3. Continuous ingest is the *whole* durability argument. `claudex`'s cron indexer
   was measured seven days stale with 148 unindexed transcripts, against a source
   that deletes after 30 days. A guarantee that depends on a human remembering to
   run a watcher is the same guarantee.

The owner decided: **"Muninn needs a daemon to be grabbing sessions."** So this
spec promotes watch-plus-serve into a service.

## The command is `serve`, not `daemon`

`muninn serve`. Three reasons, in order of weight:

- **Huginn's verb for the same thing is `serve`.** These are two halves of one
  product with one shared menubar; a user who runs both should learn one word.
  Diverging here would be a gratuitous difference in the most-typed command.
- It names what the process *does*. `daemon` names what it *is*, which the user
  does not need to care about, and which is also wrong on Windows where nothing
  is a daemon.
- `muninn daemon` reads like a noun awaiting a subcommand (`daemon start`,
  `daemon status`), and this spec deliberately ships neither — an external
  supervisor starts and stops the process, which is the whole point of the state
  file and the signal handling.

The module is still `muninn/daemon.py`, because that is what the subsystem is.

## What the daemon owns

| Concern | Owner | Note |
|---|---|---|
| Continuous ingest | `indexer.watch()` | **Not reimplemented.** The daemon owns *running* it. |
| Startup sweep | `indexer.watch()` | Already first thing in that function; must stay first. |
| Raven descriptor | `ravenserve.attach()` → `raven.publish()` | Moved here from `index --watch`. |
| `/api/menu` | `ravenserve.RavenService` | Reused as-is. No new HTTP code. |
| State file | `daemon.write_state()` | New. `~/.local/state/muninn/daemon.json`, 0600. |
| Single-instance guard | `daemon.SingleInstance` | New. `daemon.lock`, advisory `flock`. |
| Termination | `daemon.install_termination_handlers()` | SIGTERM + SIGHUP. |
| Reporting | `cli._print_daemon_section()` | New `doctor` section. |
| Start-at-login | `corvidae.login_agent` via `muninn/agent_install.py` | The follow-up seam, now filled. **The daemon itself needed no change.** |

**`muninn index --watch` stays** and is now the *foreground/debug* path: the same
ingest loop, publishing nothing — no port, no descriptor, no state file. It is
how someone watches ingest without installing a service. It still takes the
single-instance lock (see below).

`index --watch`'s `--no-menubar` flag is **accepted and inert**, deliberately
rather than removed: its request is now unconditionally satisfied, and an
existing plist or shell alias that passes it should not start failing on an
unrecognised argument. It is hidden from `--help`.

## The state file

`$XDG_STATE_HOME/muninn/daemon.json` (Windows: `%LOCALAPPDATA%\Muninn\`), by
`muninn/paths.py`'s existing resolution — **not** the shared ravens directory,
which is a cross-project contract and is untouched by this spec.

```json
{
  "db": "/Users/you/.local/state/muninn/muninn.db",
  "pid": 15586,
  "port": 53683,
  "python": "/Users/you/Projects/muninn/.venv/bin/python3",
  "repo": "/Users/you/Projects/muninn",
  "started": 1785622001.0744882
}
```

**Field names mirror Huginn's `daemon.json`** (`huginn/daemon.py`'s
`_write_daemon_state`) wherever they mean the same thing — `pid`, `port`,
`started`, `python`, `repo` — so the two ravens are operationally similar and one
script can read either. Decisions worth stating:

- **`started` is epoch seconds, not ISO.** The opposite of how the rest of Muninn
  stores time (`store.record_sweep`, the ledger). Deliberate: it matches both
  Huginn's field of that name and the `started` in Muninn's own raven descriptor,
  which are the two readers that exist. `doctor` renders it.
- **`port` may be `null`, and a reader that assumes otherwise is wrong.**
  Huginn's bind is mandatory and its daemon dies without one; Muninn's raven is
  best-effort by design (`attach()` returns `None` rather than costing ingest,
  spec 009 #9), so "running, no menu port" is a legitimate state that must still
  be discoverable. The field is present and null, never omitted.
- **`db` is Muninn's own addition.** The archive path is overridable per
  invocation (`--db`), so a supervisor otherwise cannot tell which archive a
  running daemon feeds.
- **`python` + `repo` answer "where do I relaunch this from."** That is the
  question Huginn's menubar app once answered by hardcoding one developer's
  checkout (its issue #37).

Written **atomically**, temp file in the same directory, `fsync`, mode set
*before* `os.replace`. **0600 in a 0700 directory.** The ordering is the point:
creating the final file and chmodding after leaves a world-readable window, and
this file names a loopback port that answers unauthenticated requests while
`python`/`repo` are paths something may execute. Huginn's equivalent uses
`write_text` then `chmod`, which has that window; this is the stricter of the two
on purpose.

**Removal is ownership-checked.** `remove_state()` deletes only a file whose
`pid` is this process. A process that lost the lock must never delete the live
daemon's state file on its way out — Huginn's issue #40 shape.

## Shutdown

The trap, stated plainly because it does not look like a missing handler:
**Python's default `SIGTERM` disposition terminates the process without
unwinding the stack, so a `finally:` never runs.** The descriptor survives naming
a dead port and the state file survives naming a dead pid, which makes a service
manager's ordinary "stop" indistinguishable from a crash. Huginn shipped exactly
this (its issue #43) and its Quit menu item was the trigger — the *common* path.

- **SIGTERM** → handler raises `SystemExit`, which unwinds. This is how a
  supervisor stops the daemon.
- **SIGHUP** → same handler. A daemon started from a terminal that then closes
  gets this one and orphans the same two files. Easy to forget precisely because
  it only happens to a daemon someone started by hand.
- **SIGINT** → **deliberately not claimed.** It already raises
  `KeyboardInterrupt`, which unwinds. Replacing a working path with an untested
  one is the trade being declined.
- **A second terminating signal during teardown is ignored.** A supervisor that
  escalates TERM, TERM, KILL must not have its second TERM abort the teardown
  that is busy removing the files. SIGKILL still works; the teardown is
  milliseconds.
- **SIGKILL leaves both files, and that is correct.** Not a gap to close. The
  host checks `pid` and `started` before trusting a descriptor, and `doctor`
  cross-checks the state file's pid, so a crash is *reported* rather than hidden.
  Machinery to guarantee removal would have to run in the one path where the
  process is already gone.

### `watchfiles` looks like it already handles this, and it does not

Worth recording because the interaction is confusing enough that a future reader
could delete the handler as redundant. `watchfiles.watch` is called with
`raise_interrupt=False`, and its Rust core notices *any* terminating signal — so on
`SIGTERM` it logs `KeyboardInterrupt caught, stopping watch` and makes its
generator **return normally**. Measured with both in play:

```
PYTHON HANDLER RAN
KeyboardInterrupt caught, stopping watch
GENERATOR RETURNED without SystemExit
```

Two consequences:

1. **That log line is `watchfiles`', and it is wrong about the signal.** Nothing
   raised `KeyboardInterrupt`. It is not from this repo and should not be read as
   evidence that Ctrl-C was pressed.
2. **The generator returning covers only the window where the loop is blocked on
   it.** It does **not** cover the startup sweep — which is the longest window
   there is, measured at ~45 s against the real 614-session corpus, spent entirely
   inside `indexer.sweep()` with no generator alive. A `SIGTERM` there meets
   Python's default disposition and orphans both files. The handler is what covers
   that, and
   `tests/test_daemon.py::test_sigterm_during_the_startup_sweep_still_tears_down`
   is what fails if it is removed on the theory that `watchfiles` had it handled.

Teardown order, each position load-bearing:

1. **Withdraw the descriptor** — stop advertising a port before it dies.
2. **Remove the state file** — a supervisor restarting immediately must not read
   a stale "running" state while the lock is already free.
3. **Close the store.**
4. **Release the lock last** — which is what makes the teardown atomic from
   outside.

## Single instance

Two ingest loops against one archive is **not** a data-corruption bug: spec 001's
`import_lock` serialises individual imports and `tests/test_indexer.py::Concurrent
ImportTest` proves it. It is a *lifecycle* bug, and the two failures are:

1. Both loops drain the same queue directory and sweep the same roots, so every
   transcript is parsed twice and each loop's receipts describe work the other
   already did.
2. Both publish the same descriptor path. Last writer wins — and then the
   **loser's** teardown deletes the **winner's** descriptor, so a perfectly
   healthy daemon silently drops out of the menubar.

**Decision: one advisory whole-file lock at `$XDG_STATE_HOME/muninn/daemon.lock`,
taken by `serve` *and* by `index --watch`.** Whichever starts second exits 1 with
the holder's pid and label named. Locking only in `serve` would make `index
--watch` look harmless while it silently doubles every import.

Implementation notes that are not incidental:

- **`fcntl.flock`, never `fcntl.lockf`.** POSIX record locks are owned by the
  *process*, so closing **any** descriptor for the file releases every lock that
  process holds on it — and `SingleInstance.probe()` opens and closes its own
  descriptor. With `lockf`, a probe running inside the daemon would silently
  unlock the daemon. `flock` locks belong to the open file description.
  `tests/test_daemon.py::test_probing_does_not_release_the_real_lock` fails if
  this is ever swapped.
- **The lock file is not unlinked on release.** Unlinking races a process that
  already opened the same path: it would hold a lock on an unlinked inode while a
  third opens a fresh file and locks that, and then two daemons each believe they
  are alone.
- **The kernel releases the lock on SIGKILL**, which is the property a pid file
  does not have and the reason this is a lock at all.
- **No locking primitive ⇒ fail open**, with a warning. Two loops waste work; a
  daemon that refuses to run loses transcripts, which is the failure this project
  exists to prevent. `doctor` reports the guard as *unknown*, never as free.
- **The holder label is a closed vocabulary** (`serve`, `index --watch`).
  Anything else read back out of the file reports as `unknown` rather than being
  echoed — that line reaches `doctor`'s output, and the read is bounded at 256
  bytes.

## `doctor`

A new `daemon` section, and the existing `shared menubar` section's heading
updated (`muninn serve` now publishes, not `index --watch`). Three facts,
deliberately not collapsed into one verdict:

```
daemon (`muninn serve`)
  lock        held by pid 15586 (serve)
  running     pid 15586 · since 2026-08-01T22:06:41+00:00
  menu port   53683
  archive     /Users/you/.local/state/muninn/muninn.db

shared menubar (published while `muninn serve` runs)
  descriptor  /Users/you/.local/state/ravens/muninn.json
              serving · pid 15586 · port 53683
```

- **The lock** answers "is an ingest loop running at all" — including `index
  --watch`, which writes no state file. It is the only one of the three the
  kernel maintains, so it survives a SIGKILL that leaves the others stale.
- **The state file** answers "can a supervisor find it", and names the port.
- **The descriptor** answers "is Muninn in the menubar".

A crashed daemon reads as stale, never as running:

```
  lock        free — no ingest loop is running
  state       /Users/you/.local/state/muninn/daemon.json
  WARNING: state file names pid 15782, which is not running — the daemon crashed; the file is stale
```

The pid is cross-checked against the OS rather than trusted because a file says
so. A stale state file plus a free lock is not "running", and reporting it as
running is the invisible-staleness failure this project has already been bitten
by.

## Security

**Unchanged from spec 009, deliberately, and the threat model was re-examined
rather than assumed.** `/api/menu` stays unauthenticated with no `token_path`;
`Host` validation, blanket `Origin` rejection, `Content-Length` guarding, the
response-header allowlist and `safe_label` sanitising are all reused, not
rewritten.

**Does a daemon change the threat model? No, and here is the reasoning rather
than the conclusion.** What changed is *uptime* — the port is now up whenever the
machine is, instead of whenever someone ran a watcher. That widens the *window*
for the threat spec 009 identified (a web page in the user's browser reaching a
loopback port), and widens nothing else:

- The payload is identical. Still counts, relative times, and short labels; still
  no prose, no transcript text, no path beyond a basename. `/` and
  `/session/<id>` are still stubs.
- The surface is identical. Still `GET` only, still one route with data on it.
- A token would still not help, for spec 009's reason: the page would be refused
  for lacking one, which is the same outcome `Host`/`Origin` already produce. A
  longer window on a defence does not make the defence weaker; it makes it more
  important, which is why those checks are not optional.

What *would* change the model is a menu row carrying prose or an action, and that
remains the trigger to reopen the decision — in the same change, not afterwards.

The two files this spec adds are 0600 in a 0700 directory. `write_state`'s
`python`/`repo` fields are the one genuinely new asset: a tray app may execute
that interpreter path, so **integrity matters even where confidentiality does
not**, which is the reason for 0600 rather than the archive's own precedent.

## Acceptance criteria

1. `muninn serve` sweeps before watching (inherited from `indexer.watch`, and
   re-asserted here because the daemon is now what runs it).
2. Startup publishes a raven descriptor **and** a state file; both 0600 in a 0700
   directory, verified under `umask 0`.
3. The state file's `pid`/`port` agree with the descriptor's, and the advertised
   port answers `/api/menu`.
4. `SIGTERM` → exit 0, descriptor **and** state file gone.
5. `SIGHUP` → identical.
6. `SIGINT` → identical (the pre-existing path must not have regressed).
6a. `SIGTERM` **during the startup sweep** → identical. This is the window
    `watchfiles` cannot cover, so it is the one that proves the handler is doing
    the work.
7. `SIGKILL` → both files remain, and `doctor` reports the state file as stale
   rather than running.
8. A second `serve`, or an `index --watch` alongside one, exits 1 naming the
   holder's pid — and the running daemon is untouched.
9. A restart over a crashed daemon's leftover files succeeds.
10. `index --watch` publishes **neither** file, and still takes the lock under
    its own label.
11. `serve --no-menubar` publishes no descriptor, records `"port": null`, and
    still ingests.
12. `doctor` reports the daemon's pid, port and lock holder while it runs.
13. State-file removal is ownership-checked: it refuses to delete a file naming
    another pid.
14. `probe()` never releases a lock it only meant to read.

## Definition of done

```sh
uv run python -m pytest tests -q       # 339 passed, 1 skipped, 109 subtests
uv run ruff check .
uv run muninn serve                    # descriptor + state appear; Ctrl-C removes both
uv run muninn doctor                   # daemon section names the pid and port
kill -TERM $(python3 -c 'import json;print(json.load(open("'"$HOME"'/.local/state/muninn/daemon.json"))["pid"])')
uv run muninn install-agent            # plist appears; launchd starts the daemon
uv run muninn uninstall-agent          # plist gone, daemon stopped, both files removed
```

The count was **278 passed, 1 skipped, 97 subtests** when the daemon alone landed;
the installer added 61 tests and 12 subtests.

Verified additionally against a live daemon in a redirected `HOME`: SIGTERM,
SIGHUP, SIGINT and SIGKILL each signalled at a real process, with the descriptor,
the state file, the lock and a real `/api/menu` fetch checked at every step.

The installer was verified the same way — a real `launchctl load`, a real daemon
under launchd's supervision, and a real `uninstall-agent`. That last one is the
useful evidence for this spec's own claim that the daemon needed no change:
`launchctl unload -w` sends `SIGTERM`, and both `daemon.json` and the raven
descriptor were gone afterwards, with the lock free. The teardown spec 010 built
for a supervisor was exercised by an actual supervisor.

### Mutation-verified, because a passing signal test proves less than it looks

Huginn's cautionary precedent: its first signal tests all passed with the fix's
call site deleted from `run()`, because they exercised the handler helper
directly. So this spec's tests were checked against four deliberate mutations,
each of which **must** fail the suite:

| Mutation | Tests that fail |
|---|---|
| `install_termination_handlers()` call site removed from `Daemon.run` | 8, incl. the live SIGTERM, SIGHUP and mid-sweep tests |
| `SIGHUP` dropped from the handler list | the live SIGHUP test |
| `remove_state()` removed from the teardown | 6 live tests |
| the lock never acquired | 6, incl. both refusal tests |

`WiringTest` additionally asserts the *ordering* — handlers installed before
anything is published and before the loop starts — which a live subprocess cannot
observe.

## Guardrails

- **Do not reimplement the ingest loop.** `indexer.watch()` is the engine.
- **Do not skip or move the startup sweep.** Events during downtime were missed
  by every watcher and only a sweep recovers them.
- **Do not delete `index --watch`.** It is the foreground path.
- **Do not let the daemon publish from two places.** Exactly one process owns the
  descriptor path.
- **Do not rewrite `ravenserve`.** Reuse `attach()`; it already returns `None`
  rather than costing ingest.
- **Do not change how the shared ravens directory resolves.** It is a
  cross-project contract with Appistry and Huginn.
- **Do not add auth to `/api/menu`** without the trigger spec 009 names (prose or
  an action in a row), and revisit it in the same change if so.
- **Do not put prose, transcript text, or a full path in a menu label.**
- **Do not claim `SIGINT`.** It already works.
- **Do not modify** `tests/test_losslessness.py`, `tests/test_ledger.py`,
  `tests/test_indexer.py`, `tests/test_raven.py`, `tests/test_query.py`,
  `tests/test_queue.py`, `tests/test_exports.py`, `tests/test_version.py`.
  This spec modified **none** of them — nor `tests/test_daemon.py`, which the
  installer work added nothing to either: the installer's tests live in
  `tests/test_agent_install.py`.

## Out of scope

- **A `stop`/`restart`/`status` verb.** A supervisor sends signals and reads the
  state file; `doctor` reports. Adding process-management verbs would duplicate
  what launchd and systemd already do better, and `muninn serve` would then own a
  second, worse copy of it.
- **The console.** Still unspec'd, and `/` and `/session/<id>` are still stubs —
  a real UI there would carry prose and force spec 009's token decision open.

Formerly out of scope and **now filled** — see "The login-agent installer" below:
the launchd/systemd/Windows installer, the `install-agent` verb, and the
`corvidae` dependency.

## The login-agent installer (the follow-up seam, now filled)

This section was written as future work while `corvidae` was unpublished. It is
implemented, and the daemon needed **no change** — which is what the seam was
predicting, so it is recorded as a confirmation rather than quietly deleted.

`corvidae 2026.8.1` is on PyPI (Apache-2.0, stdlib-only, **zero dependencies**),
and `pyproject.toml` now carries `corvidae>=2026.8.1,<2027`. The upper bound is
the CalVer year: corvidae promises every `2026.*` release is compatible with every
other and that a breaking change waits for the next year component, so `<2027` is
precisely "not the release that is allowed to break us."

**`muninn/agent_install.py` supplies a `LoginAgentSpec` and nothing else.** No
launchd, systemd, or registry code lives in this repo, and adding a local copy of
one of corvidae's checks would be a defect rather than belt-and-braces — two
copies of a security property drift, and the copy a reader finds first is the one
they trust. Inherited unchanged: `plistlib.dumps` of a real dict rather than an XML
template, systemd refusing `\n`/`\r`/`%`, `mkstemp` + `os.replace` at 0600 with a
0600 backup taken *before* content lands, refused symlinks at the target and the
temp path, launchd keeping `KeepAlive`, systemd using `Restart=on-failure`, and
the `loginctl enable-linger` caveat printed for headless hosts.

Muninn's values, every one of them disjoint from Huginn's:

| | Muninn | Huginn |
|---|---|---|
| launchd label | `is.tohuw.muninn` | `is.tohuw.huginn` |
| plist | `~/Library/LaunchAgents/is.tohuw.muninn.plist` | `…/is.tohuw.huginn.plist` |
| systemd unit | `$XDG_CONFIG_HOME/systemd/user/muninn.service` | `…/huginn.service` |
| launchd log | `$XDG_STATE_HOME/muninn/agent.log` | `~/.local/state/huginn/agent.log` |
| Run value | `MuninnDaemon` | `HuginnDaemon` (+ tray's `Huginn`) |
| backup tag | `muninn-bak` | `huginn-bak` |

`argv` is `[sys.executable, "-m", "muninn.cli", "serve"]` — **`serve`, not `index
--watch`**, since only `serve` publishes the descriptor, the port and the state
file. No `--no-menubar`: being in the shared menubar whenever the machine is up is
the reason to install this at all.

`sys.executable` and the checkout root are resolved by the **same expressions
`write_state` uses for `python` and `repo`**, and a test asserts the two agree. An
installed unit whose `WorkingDirectory` disagrees with the running daemon's
reported `repo` is unanswerable from `doctor` alone.

**No `tray_registry_value`.** Huginn declares one because `windows/Huginn.Tray`
registers itself in the Run key *and supervises Huginn's daemon*, so a second
autostart there would resurrect a daemon the user just quit. Muninn ships no tray:
Appistry is the shared menubar host, it registers itself through a Start Menu
Startup shortcut rather than the Run key, and it only *reads* Muninn's descriptor.
Inventing a value name would make an unrelated key's presence refuse a valid
install.

### Install refuses while an ingest loop already holds the lock

The interaction this spec left for the seam to decide. The lock already prevents
the *data* failure by making the second loop exit `EXIT_ALREADY_RUNNING` (1). What
it cannot prevent is what a supervisor does with that exit code:

- **launchd** keeps `KeepAlive`, so it relaunches a daemon that exits 1 forever.
  The user gets an `agent.log` filling with "another muninn ingest loop is already
  running" and a service that never comes up.
- **systemd** treats exit 1 as a failure under `Restart=on-failure`, so it does
  the same until `StartLimitBurst` gives up and leaves the unit `failed`.

Both are crash loops caused by a *healthy* manually-started loop. So `install-agent`
refuses, exits 1, and writes nothing — the same shape and exit code as corvidae's
Windows refusal when a tray owns startup, and for the same double-owner reason.
The rule:

- Holder is `index --watch` → **always a conflict.** A foreground debug watcher is
  by definition not the agent's own daemon, so the crash loop is certain.
- Any holder, agent **not yet installed** → a conflict. Nothing a supervisor
  started can hold the lock, so something else does.
- Agent **already installed** → *not* a conflict. The holder is almost certainly
  the agent's own daemon, and refusing would make it impossible to re-run
  `install-agent` after moving the checkout or changing interpreters — the one
  time a refresh is most needed.
- Lock state **unknown** (`probe()` returns `None`) → no conflict, matching this
  spec's own fail-open. Inverting it would make `install-agent` impossible
  wherever the guard is unenforceable, which is worse than a possible
  double-start.

`uninstall-agent` checks nothing: removing the agent while its daemon runs is the
normal case, and `launchctl unload -w` stopping it is the point rather than a
hazard.

### The installed agent does not inherit the installing shell's environment

Measured, because it is the one thing here that surprises. launchd starts the agent
from its own environment, so a `$XDG_STATE_HOME` or `$RAVENS_STATE_DIR` exported in
a terminal is **absent** at login and the daemon resolves both to their defaults.
Verified during implementation: an install run with every path redirected to a
tempdir produced a daemon that ingested into the real
`~/.local/state/muninn/muninn.db`, discovered from a log file.

Not worked around. An `EnvironmentVariables` dict capturing the installing shell
would bake one terminal's transient state into config that runs at every login for
years, and it is the exact plist key the C3 injection created out of a directory
name — a finding from the security review of the surface Huginn's issue #41 added,
not from #41's own scope, which was the model-policy chokepoint. A user who genuinely relocates Muninn's state sets the variable
where login sessions see it (`launchctl setenv`, a systemd user environment
drop-in), which is a statement about their machine rather than about this install.

### `doctor`

The existing `daemon` section gains a fourth fact rather than a competing section
— a reader who has to correlate two sections to learn the daemon is up *but*
nothing will restart it has been handed a puzzle instead of a report:

```
daemon (`muninn serve`)
  at login    installed · LaunchAgent · /Users/you/Library/LaunchAgents/is.tohuw.muninn.plist
  lock        held by pid 15586 (serve)
  running     pid 15586 · since 2026-08-01T22:06:41+00:00
```

Printed **first, before the early returns** for a stale or absent state file. That
position is load-bearing: the crashed-daemon path returns early, and it is exactly
where "is anything going to restart it" is the most useful line on screen. "not
installed" is a normal state, not a warning — an external supervisor configured by
hand, or a foreground `muninn serve`, are both legitimate and leave no plist.

### Acceptance criteria (installer)

15. `install-agent` writes a plist/unit/Run value whose command runs `muninn serve`.
16. Every named location is disjoint from Huginn's — asserted as a set, so a
    future field that collides fails the same test.
17. install → `installed()` true → uninstall → `installed()` false, per backend.
18. Linux and Windows are exercised **on macOS** through corvidae's overridable
    `systemctl`/`registry` boundary. No test requires those OSes.
19. `install-agent` exits 1 and writes nothing while `index --watch` holds the
    lock; exits 0 once it is free.
20. Re-running `install-agent` over the agent's own running daemon is allowed.
21. `doctor` reports installed/not-installed inside the daemon section, including
    when the state file is stale.
22. Exit codes are the contract: 0 installed, 1 refused or the OS mechanism
    failed, 2 no mechanism on this platform. No test parses corvidae's wording,
    which corvidae explicitly does not promise.

Mutation-verified, in the same spirit as the signal tests above. Each of these
**must** fail the suite, and each was checked: Muninn's label set to Huginn's (3
tests), `argv` switched to `index --watch` (5), the lock check removed from
`install` (2), the lock check made unconditional (3), `doctor`'s line removed (5),
the Run value set to Huginn's tray value (3), and the log path moved to Huginn's
(4).

**One test-isolation defect was found by that mutation run and is worth
recording:** the lock tests originally called the real `get_login_agent()`, so they
were safe only because the code under test refused. With the refusal mutated away,
the suite installed a live LaunchAgent into the developer's real
`~/Library/LaunchAgents` and started a real daemon. `_TempState.redirect_agent()`
now confines the backend to a tempdir, and every test that reaches `install()` or
`installed()` uses it. A test that avoids a side effect only because the code
declined is not isolated.

### Guardrails (installer)

- **Do not vendor or re-implement any part of corvidae's login-agent code**, and
  do not add a local copy of one of its checks. One implementation was the whole
  point (tohuw/huginn#42).
- **Do not soften `KeepAlive` or switch systemd to `Restart=always`.** Both were
  chosen with reasons recorded upstream.
- **Do not let Muninn's label, plist, unit, log or Run value drift onto Huginn's.**
  Every collision is silent, and the user's symptom is "the other raven stopped
  starting at login", months later.
- **Do not add `EnvironmentVariables` to the plist** to capture the installing
  shell. See above.
- **Do not assert on corvidae's printed wording.** The exit code is the contract.
