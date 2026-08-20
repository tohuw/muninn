# 021 — Unix socket transport, and the console decision it reopens

**Status:** implemented.
**Supersedes:** parts of 009, 010, and 017 (see "What this changes" below).
**Read first:** 009-raven-descriptor-menu.md for the descriptor, the payload,
and the HTTP-era security model this spec replaces; 010-daemon.md for who runs
the surface; 017-menu-lifecycle-actions.md for the action op and the
reply-before-signal ordering, both unchanged in substance here.

## Why

Spec 009's whole security section exists to answer one question: what stops a
web page open in the user's browser from reaching this raven's loopback port?
The answer was `Host` validation, blanket `Origin` rejection, and a payload
with "no prose and no transcript text" — because a token buys nothing against
that specific threat, and the checks above are what closes it.

A TCP port on `127.0.0.1` is reachable by definition from anything on the
machine that can open a socket, including a browser tab running code from any
site the user has open. A Unix domain socket is not. Opening one requires
filesystem permission on the socket's own inode — there is no browser API that
lets a web page do that, no DNS-rebinding trick that makes a foreign origin
look local, nothing to check because there is nothing to be tricked into
letting through. The `Host`/`Origin` machinery in 009 was working around the
absence of that guarantee; a Unix socket has the guarantee, so the machinery
is retired rather than ported.

That is a smaller platform than Muninn ships on. Python's `socket` module has
no `AF_UNIX` on Windows, so Windows keeps a listener — a named pipe — but a
named pipe's default security descriptor does not hand out the same
same-user-only guarantee for free. Getting an equivalent guarantee there costs
a credential, which is new, and a real ACL on the file holding it, which 009
and 010 never needed because nothing they wrote down was a secret.

## What ships

**POSIX:** a Unix domain socket at `<ravens-state-dir>/muninn.sock`, 0600, in
the existing 0700 `ravens/` directory. No token. Trust is the socket file's
own permission bits, full stop.

**Windows:** a named pipe (`\\.\pipe\muninn-raven`), plus a fresh 32-byte
authkey generated per run, written to `<ravens-state-dir>/muninn.token`, and
restricted to the current user by a real access-control entry —
`_restrict_to_current_user` in `ravenserve.py`, raw `ctypes` calls into
`advapi32`/`kernel32`, not `raven._restrict`'s best-effort mode bit (a no-op on
Windows, which was fine for every file it protects and is not fine for this
one). If the ACL cannot be proven set, no descriptor is published — the same
"no menu row rather than a broken one" rule 009's `attach()` already applies to
a bind failure, extended to an ACL failure.

**Both platforms** use `multiprocessing.connection` (`Listener`/`Client`,
`family="AF_UNIX"` or `"AF_PIPE"`) rather than two hand-written server
implementations. It is stdlib, it already abstracts exactly this platform
difference, and its `send_bytes`/`recv_bytes` give length-prefixed framing for
free — this spec does not add a second wire format on top.

**The request shape changes** from an HTTP verb and a path to a JSON body
naming an op, because there is no URL space on a socket or a pipe:

```json
{"op": "menu"}
{"op": "action", "id": "quit"}
```

and a reply of `{"ok": true, "body": ...}` or `{"ok": false, "error": "..."}`
— one message in, one message out, one connection, then close. No
keep-alive, no pipelining; the only client ever built against this transport
already opened one connection per call.

**The descriptor's shape changes.** `port` is gone. In its place:

```json
{
  "api_version": 1,
  "display": "Muninn",
  "endpoints": {"menu": "menu", "action": "action"},
  "host_priority": 50,
  "max_api": 1,
  "min_api": 1,
  "name": "muninn",
  "pid": 7092,
  "transport": "unix",
  "address": "/Users/you/.local/state/ravens/muninn.sock",
  "pages_dir": "/Users/you/.local/state/ravens/muninn/pages",
  "started": 1785619470.680397
}
```

`token_path` is added, Windows-only, alongside `transport: "pipe"` and
`address: "\\\\.\\pipe\\muninn-raven"`. `endpoints`' values are now op names,
not paths — `"menu"` is what a client sends as `{"op": "menu"}`, not a route to
GET.

## The console decision, reopened

009 stubbed `/` and `/session/<id>` on the explicit theory that a real UI
there would carry prose and would force the token decision back open — and
said so, in "Out of scope": *"a real UI on this port would carry prose and
would force the token decision to be reopened."* This spec is that reopening,
on exactly the terms 009 anticipated.

**The token conclusion does not change.** Nothing above weakens it: POSIX
still has no token, for the same reason 009 gave (the only mutation available
is stopping a process any local user can already `kill`, and the socket's own
permission bits are the entire defence for the read-only op). What changes is
the premise that made "no prose" load-bearing in the first place — a page a
user has open reaching this surface — which a Unix socket eliminates
structurally rather than the HTTP surface merely mitigating.

So each `url` a menu row emits (`build_menu` is unchanged — still `"/"`,
still `"/session/<id>"`) now resolves to a real file under `pages_dir`,
rendered fresh on every menu build by `ravenserve._write_pages`:

- `/session/<id>` → the actual transcript, via the same `Store.session_text`
  and the same metadata fields `cli.cmd_show` already prints — not a new
  rendering path, the existing one, wrapped in escaped `<pre>`.
- `/` → the Archive section's own labels, restated as a page, so it cannot
  drift from the menu that links to it.

**Roost resolves a `url` against `pages_dir`, not against an address, when
`transport` is `"unix"` or `"pipe"`.** The client-side rule, stated once so a
third-party unix-transport bird has it in one place: for a `url` of `/`, the
candidate file is `pages_dir/index.html`; for any other `url`, it is
`pages_dir/<url without its leading slash>.html`. The candidate's *realpath*
must have `pages_dir`'s own realpath as a prefix, and must name a file that
exists — anything else is refused. This is the direct descendant of 009's
"a menu item cannot navigate the user anywhere except the bird that offered
it," restated for a filesystem instead of a port: a bird can only ever open
something it itself rendered under its own directory.

**Filenames carry no path-traversal risk independent of that check.**
`raven.SESSION_ID_RE` requires the first character to be alphanumeric and
allows only `[A-Za-z0-9._-]` after it — no `/`, and a leading character
requirement that makes a bare `..` impossible to match. `_write_pages` re-applies
this pattern before treating any `url` as a session id, rather than trusting
that `build_menu` already filtered the list it was given.

**Content is escaped, not sanitised, and that distinction is the point.**
`raven.safe_label` strips hostile *formatting* — ANSI, control characters, bidi
overrides — because a label is displayed as a menu row a spoof could disguise.
A rendered transcript is prose in a `<pre>` block, and `html.escape` is what
that content actually needs: neutralising `<`, `&`, and quotes so nothing in a
transcript can inject markup, which is a complete defence for inert preformatted
text and is not the same job `safe_label` does. Session metadata (the
`cwd`/`branch`/`model`/etc. list) goes through `html.escape` for the same
reason, not through `safe_label` — it is displayed as text on a page, not as a
menu label a spoof could hide behind.

## What this changes in 009

- **"Security"**, in full, on POSIX: `Host`, `Origin`, `Content-Length`,
  `GET`-only — none of it applies to a transport with no headers and no verb.
  Retained in 009 as the historical record of the HTTP surface's own rules.
- **"The token decision"**: re-derived for the new threat model, not carried
  over. Conclusion unchanged on POSIX; Windows now has a `token_path`.
- **The descriptor's `port` field**: replaced by `transport`/`address`, plus
  `pages_dir` and (Windows only) `token_path`.
- **"No prose, no transcript text"**: reversed, on the record, per "The
  console decision, reopened" above.
- **Acceptance criteria 1, 4, 5, 7**: superseded per the inline notes left in
  009 itself.

## What this changes in 010

- **"Security"**: re-derives 009's conclusion rather than inheriting it
  unexamined, per the note left in 010 itself.
- **The state file's `port` field**: renamed `address`. The parity with
  Huginn's `daemon.json` this field name existed for no longer holds for this
  one field, and a field called `port` holding a socket path would be a worse
  lie than an honest divergence.
- **`doctor`'s "menu port" line**: reads "menu", prints the address.

## What this changes in 017

- **The action op's transport**: `POST`/status codes become a reply body
  (`{"ok": ..., ...}`); there is no path to 404 on. The reply-before-signal
  ordering and the `(reply, followup)` return shape are unchanged — 017's
  argument for why that ordering matters is transport-independent and is not
  restated here.
- **"A new port" on restart**: the address is now a fixed name, the same
  across a restart. `pid`/`started` are what prove a restart happened; a
  restart was never proven by the address changing so much as accompanied by
  it, and that accompaniment stops holding when the address is not ephemeral
  to begin with.
- **The token decision**: see above; unchanged on POSIX, new on Windows, for
  platform reasons rather than for anything specific to Quit/Restart.

## Windows: what a token buys here, and why it costs an ACL

`multiprocessing.connection`'s own documentation is blunt about this: an
`authkey` is recommended for `AF_PIPE` because a named pipe's default security
descriptor does not restrict connections to the pipe's creator. Unlike the
POSIX case, where "can open the socket file" and "is authorised" are the same
fact because the filesystem enforces it before our code ever runs, a Windows
client that merely knows the pipe's name can attempt to connect regardless of
who it is — the authkey is what turns "attempted" into "accepted."

That makes the file holding the authkey the entire credential, and a
best-effort mode bit is not an acceptable posture for it — NTFS ACLs, not
mode bits, are Windows's actual access-control mechanism, and `raven._restrict`
no-ops on Windows precisely because nothing else it touches needed one.
`ravenserve._restrict_to_current_user` sets a real, replacing DACL naming only
the calling process's own SID, via `OpenProcessToken`/`GetTokenInformation`
(to get that SID) and `InitializeAcl`/`AddAccessAllowedAce`/
`SetNamedSecurityInfoW` (to apply it) — `ctypes` calls into `advapi32`/
`kernel32`, not `pywin32`, matching this project's existing stdlib-only stance
(`raven._launch_block`'s docstring on why `corvidae`'s helper is preferred
over a new dependency makes the same call for a different file).

**This code is unverified on real Windows.** It was written and reviewed
without access to a Windows machine, and Win32 security-descriptor
construction is exactly the kind of code that can be subtly wrong in ways a
review cannot catch without exercising it. `_restrict_to_current_user`
reports failure honestly rather than assuming success — see `_listen`, which
turns a `False` there into an `OSError` that costs the raven its menu row
rather than publishing a token nothing protects — but "fails safe" is not the
same claim as "works correctly," and this must be exercised by Windows CI, or
by hand on a Windows box, before anyone treats the Windows transport as
trusted.

## Acceptance criteria

1. POSIX: the listener binds a Unix domain socket at `raven.socket_path()`,
   0600, in the existing 0700 `ravens/` directory — including under `umask 0`.
2. POSIX: no token is generated, written, or advertised.
3. Windows: the listener binds a named pipe; a fresh 32-byte authkey is
   generated per run, written to `raven.token_path()`, and
   `_restrict_to_current_user` is called and its result checked before the
   listener is trusted.
4. Windows: `_restrict_to_current_user` returning `False` results in no
   descriptor being published and the token file being removed — the same
   "no row rather than a broken one" contract `attach()` already has for a
   bind failure.
5. A stale socket file (a crash) does not prevent a subsequent `serve()` from
   binding — `_listen` unlinks it first, safe because `SingleInstance`'s lock
   already rules out a second live listener.
6. `{"op": "menu"}` returns the same payload shape `raven.build_menu` always
   produced; `{"op": "action", "id": ...}` dispatches through
   `raven.perform_action` unchanged; any other or malformed request gets a
   `{"ok": false, "error": ...}` reply, never a dropped connection.
7. A message over `MAX_REQUEST_BODY` is refused without being parsed.
8. The reply is written and flushed before an action's `followup` runs — the
   transport changed; this ordering, inherited from 017, did not.
9. Every `url` a payload emits (`/`, `/session/<id>`) has a corresponding file
   under `pages_dir` by the time the payload is returned — enforced by
   `_write_pages` running inside the same `provide()` call that builds the
   payload, before either is handed back.
10. `/session/<id>`'s rendered page contains the real `Store.session_text`
    output, HTML-escaped, plus the same metadata fields `cli.cmd_show` prints —
    not the id-echoing stub.
11. A session id that does not match `raven.SESSION_ID_RE` never reaches a
    filename, independent of whether the payload that referenced it was
    trusted.
12. `RavenService.stop()` removes the socket file (POSIX) or the token file
    (Windows) in addition to withdrawing the descriptor — no credential or
    stale address survives a clean shutdown.

## Guardrails

- **Do not reintroduce `Host`/`Origin` checking on POSIX.** There is no
  header to check on a Unix socket; adding one back would be motion, not
  defence.
- **Do not use `raven._restrict` for the Windows token file.** It is a
  documented no-op there. Use `_restrict_to_current_user` and check its
  return value.
- **Do not add `pywin32`.** The ACL helper is `ctypes`-only on purpose; see
  "Windows" above.
- **Do not let `_write_pages` render a page for a `url` `build_menu` did not
  just emit.** The Roost-side containment check treats "no file" as "refused"
  — a page written for a stale or hypothetical row is a page that check
  cannot protect.
- **Do not put anything through `html.escape` that should have gone through
  `raven.safe_label` first, or vice versa.** They defend different things —
  see "Content is escaped, not sanitised" above — and using one where the
  other belongs is the kind of substitution that looks fine in review and
  fails only on hostile input.
- **Do not trust the Windows ACL path without exercising it on real
  Windows.** See "Windows" above.

## Out of scope

- **A general console.** This spec renders exactly the pages a menu row can
  already link to — a static transcript view and a static archive summary —
  not a search UI, not pagination, not anything interactive. The threat model
  changed; the scope did not.
- **Migrating Huginn or any third-party bird to this transport.** Roost
  dispatches on the descriptor's `transport` field and treats its absence as
  `"http"`, so nothing else has to change for this to ship. Whether Huginn
  ever adopts a Unix-socket transport of its own is that project's decision.
- **A Windows named-pipe SID restriction set at pipe-creation time instead of
  an authkey.** `multiprocessing.connection` does not expose passing a custom
  security descriptor to `CreateNamedPipe` through its public API, and
  reaching past it to do so would mean not using
  `multiprocessing.connection` at all — a larger, differently-risky rewrite
  for a guarantee the authkey already provides.
