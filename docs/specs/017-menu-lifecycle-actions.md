# 017 — Quit and Restart from the menu bar

**Status:** implemented.
**Supersedes:** parts of 009 (see "What this changes in 009" below).
**Read first:** 009-raven-descriptor-menu.md for the descriptor, the payload and
the security model; 010-daemon.md for who runs the surface and what teardown
means.

## Why

Huginn's menu has offered Quit and Restart since it had a menu. Muninn's has
offered link rows only, so the two ravens sitting in one menu bar behaved
differently for no reason a user could see: one could be stopped where it was
visible, the other had to be stopped somewhere else. There is no *history-archive*
argument for that asymmetry — it was a consequence of 009 deciding, correctly for
what existed then, that a history console has nothing worth mutating from a menu.

Stopping the process is not mutating history. That distinction is the whole of
this spec, and it is also its boundary: it is the reason these two actions are
defensible on a port with no credential, and the reason a third action would not
automatically be.

## What ships

Two rows, in a `lifecycle` section, last in the menu:

| Label | Action id | Effect |
|---|---|---|
| `Quit Muninn` | `quit` | Graceful teardown, process exits 0 |
| `Restart Muninn` | `restart` | Graceful teardown, then the same process runs a fresh `Daemon` |

There is deliberately **no `Start Muninn`**. A stopped daemon has withdrawn its
descriptor, so there is no menu for the row to live in and no process to serve it.
Starting at login belongs to the OS supervisor (`muninn install-agent`), which is
where an exec path can be recorded once and audited, rather than in a menu.

Restart is a plain row, not an Option-click alternate the way the superseded
native menu-bar apps did it. The host renders labels and has no modifier-key
vocabulary to hide one behind, and a menu item nobody can discover is not a
replacement for one they could.

## The three claims that must agree

Whether POST routes, whether the descriptor advertises `endpoints.action`, and
whether the menu draws the two rows are **one decision expressed three times**.
They are therefore derived from one argument — `attach(action_handler=…)` — because
any two of them disagreeing produces a specific user-visible lie:

- Row drawn, no route → the click fails with a 405.
- Route, no advertisement → the host posts to its own fallback path or not at all.
- Advertised, no row → nothing, until something else posts to it.

`muninn index --watch` publishes no descriptor at all (009, and 010's "The
lifecycle question"), so this does not reach it.

## Reply first, stop second

The action handler returns `(reply, followup)`. The server writes and flushes the
reply, *then* calls `followup`. This ordering is the substance of the feature, not
an optimisation:

Roost holds an open request with a short budget, and a connection dropped
mid-response is indistinguishable from a wedged raven. A quit that exited from the
request thread would therefore render in the menu bar as an action that **failed**,
on every single success. This is Huginn's issue #43 one layer up, and the mistake
is available to anyone who reads "the action stops the process" and writes exactly
that.

## The stop routes through SIGTERM

`deliver_stop_signal` sends `SIGTERM` to its own pid rather than setting a private
shutdown flag. The handler installed at the top of `Daemon.run` turns that into
`SystemExit` on the main thread, which unwinds `indexer.watch` and lets the
`finally` withdraw the descriptor, stop the embedder, remove the state file and
release the lock — in that order.

Reusing that path rather than adding one is the point. Two teardowns means the one
that is exercised less is the one that orphans the descriptor, and Muninn's
signal teardown is the path with the live-process tests behind it.

**`_terminating` is cleared when handlers are installed.** It is process-global and
means "a teardown is in progress, ignore further signals" — which is correct within
one teardown and was silently wrong across a restart: a Restart stops the loop
*with* a SIGTERM, so the flag was already set when the next run began, and the
restarted daemon ignored every SIGTERM for the rest of its life. An unstoppable
service, visible only to someone who restarts and then tries to stop.

## Restart is in-process

A fresh `Daemon` per iteration of a loop in `cli._run_ingest_loop`, not a re-exec.
Re-exec would mean building an argv and an interpreter path and then running it,
which is the write-then-execute shape this project hardens against elsewhere;
there is nothing to resolve, since the code is loaded and the config is parsed.

A *fresh* instance rather than a reused one, because a restart has to look like a
restart to everything watching: a new port, a republished descriptor, a new state
file, and an embedding worker that starts from the current backlog rather than
from a stalled counter (`embedder.STALL_LIMIT` is per-instance by design). The
action handler is closed over its daemon instance, so a reused one would also
accept a click and stop nothing.

The sentinel that carries "restart" out of one run is **not** an exit code, and
must never become one: a supervisor reading it as a status would treat a restart as
a failure and start racing the process that is already coming back.

## Security: the token decision, revisited and unchanged

**Amended by 021, for POSIX only: the browser-threat argument two paragraphs
down no longer applies, because there is no `Host`/`Origin` to check on a Unix
socket — but the conclusion (no token) is re-derived there on different
grounds, not carried over unexamined.** Windows gained a token this section
did not anticipate, for reasons specific to that platform's named-pipe ACLs
rather than to what these two actions do; see 021.

009 required that the token decision be reopened in the same change that added an
action. It is, here, and the answer is the same — for one reason specific to these
two actions:

- The only mutation is stopping this process, and any local process running as
  this user can already do that with `kill`. A token would guard a door with no
  lock on the wall beside it.
- The browser threat is unchanged and is still handled by `Host` and `Origin`
  rather than by a credential. A page's `fetch` carries an `Origin` and is refused
  before the router sees it; a `<form>` POST cannot produce the JSON body the
  handler requires. A token would refuse the same requests for a different reason.

**This does not generalise, and that is normative.** An action that wrote to the
archive, spent money at a provider, or returned transcript text is defensible on
neither ground and requires the token this endpoint still does not have.

The body cap moves from 0 to 512 bytes, because one route now reads
`{"id": "<action>"}`. It stays that small for the reason it was zero: the cap is
what bounds `rfile.read` before anything parses. The posted id is truncated to
`raven.MAX_ACTION_ID` before it is compared or logged.

## What this changes in 009

These 009 statements are superseded, and only these:

- "**No `action` endpoint.** Every row is a link." — now conditional on the
  publisher supplying a handler. A daemon does; anything else still does not.
- "`GET` only. `POST` answers 405." — POST routes at `ACTION_ENDPOINT` when a
  handler exists, and still answers 405 otherwise.
- "The cap is zero — this surface takes no bodies." — 512 bytes, one route.
- Acceptance criteria 4 and 7's `no action endpoint` / `POST → 405` clauses hold
  for a handler-less server, which is what those tests construct.

Everything else in 009 stands unchanged: same descriptor fields, same liveness
rules, same guards, same label sanitising, same "the menubar must never cost the
indexer its ingest".

## Acceptance criteria

1. Lifecycle rows absent by default and present only when asked for; last section;
   both `muted`; both parse under the host's real parser as **enabled** rows.
2. No `Start` row in any payload.
3. Descriptor advertises `endpoints.action` exactly when the server routes it, and
   omits it otherwise.
4. `perform_action` records intent and defers the stop; an unknown id is reported,
   not ignored; a daemon with no running loop refuses rather than appearing to
   succeed.
5. POST: valid id → 200; refused action → 409 (not 200 with `ok:false`); malformed
   or absent body → 400 with a reason; over the cap → 413 without reaching the
   parser; wrong path → 404; guards run before routing; a raising handler → 500,
   never a dropped connection. **Amended by 021:** "POST"/status codes are the
   HTTP-era shape; the current transport reports the same outcomes as
   `{"ok": true/false, ...}` in the reply body, not as a status code, and there
   is no "wrong path" to 404 on a transport with no URL space.
6. Live process: the Quit row leaves neither the descriptor nor the state file
   behind, and the reply arrives before the process goes away.
7. Live process: the Restart row comes back on a **new** port with the same pid, a
   republished descriptor that names the new port, and a daemon that still stops
   cleanly on SIGTERM afterwards. **Amended by 021:** "new port" was true of an
   ephemeral TCP port; the current address (a fixed socket/pipe name) is the
   *same* across a restart by design, and it is the descriptor's `pid`/`started`
   that prove a genuine restart happened rather than a rebind at a new address.
8. `install_termination_handlers` clears the in-teardown flag, so a restarted
   daemon is still stoppable by signal.
9. The restart sentinel never reaches a shell as an exit code.

## Out of scope

- **Any action that touches the archive, a provider, or transcript text.** See the
  security section; such an action reopens the token decision and does not inherit
  this one.
- **A confirmation step.** Roost forwards an action id and does not interpret it;
  Huginn's equivalent rows have no confirmation either, and a stop that is
  recoverable by clicking the app again does not warrant one.
- **`Start`.** See above; it belongs to the OS supervisor.
