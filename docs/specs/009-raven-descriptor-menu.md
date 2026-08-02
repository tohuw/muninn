# Spec 009 — Raven descriptor and `/api/menu`

**Status:** implemented (producer side only).
**Read first:** `.valholl/articles/shared-menubar.md` — normative for *why* there
is one menubar rather than two. Then the host's `SPEC.md` ("The Raven Protocol",
version 1), which is normative for the **wire format** and outranks this document
on any question of shape. Verified against that repository at commit
`137ea7e3d8efed1b764cf267daf2a1d685f2c577`, when it was named `tohuw/appistry`; it
is now **[`tohuw/roost`](https://github.com/tohuw/roost)** and the host is called
**Roost**. "Appistry" below is that same host under its old name — the protocol did
not change with the rename, so the commit above is still the verified reference
point.

## Why

`docs/specs/README.md` listed the shared menubar as "not yet spec'd", and Muninn
published nothing at all. Meanwhile Appistry became a shared status menu bar for
two ravens — Huginn (live activity) and Muninn (history) — that discovers
participants by reading self-published JSON descriptors and then fetches a
declarative menu from each. A raven that publishes no descriptor is simply absent,
and absent is indistinguishable from never installed.

This spec is **Muninn's producer side only**: the descriptor, the `/api/menu`
payload, and the loopback surface that serves it. Appistry is a *consumer* of this
protocol and is not a dependency of Muninn — nothing in `muninn/` imports it, and
nothing may start to.

## The lifecycle question, and the honest answer

**Muninn has no daemon, and this spec does not add one.**

Every entry point is a one-shot CLI invocation that exits in milliseconds. The
architecture is deliberate about this: `muninn/queue.py` exists precisely so the
`SessionEnd` hook touches a directory and nothing else, and `muninn/paths.py` is
split out to keep `sqlite3` off that path (see
`.valholl/articles/session-lifecycle-facts.md`). Note that `fastapi` and `uvicorn`
are declared dependencies but **no module imports either** — they are reserved for
the console, which is also unspec'd.

A `menu` endpoint requires *something* listening. The one process that already
runs for as long as the user's machine is up is `muninn index --watch` (spec 003),
so:

> The descriptor is published, and `/api/menu` served, **for exactly as long as
> `muninn index --watch` runs.** Nothing else publishes.

**The consequence, stated rather than hidden: when the watcher is not running,
Muninn is absent from the menubar.** That is acceptable, and pretending otherwise
would be worse. Appistry treats an unreachable raven as a disabled section with a
visible reason, and a *missing* descriptor as a raven that was never installed —
both legitimate steady states.

### What was rejected, and what is still open

Rejected: a second always-on daemon whose only job is answering a menu fetch.
That is a new subsystem, a new lifecycle, and a new loopback port on a machine
that did not ask for one, in exchange for a menu section. It is not a trade this
spec is entitled to make quietly.

**Still an owner decision:** whether Muninn *should* be present in the menubar
independently of the indexer. If the answer is yes, the honest shape is a
documented service unit (launchd/systemd) running `muninn index --watch`, which
makes the indexer the daemon Muninn already almost has — not a new process.
`muninn doctor` prints whether a descriptor is currently published precisely so
this gap is visible rather than discovered.

## Files

| File | Action |
|---|---|
| `muninn/raven.py` | **new** — descriptor, menu payload, label sanitising |
| `muninn/ravenserve.py` | **new** — loopback listener, publish/withdraw lifecycle |
| `muninn/cli.py` | `index --watch` attaches the service; `--no-menubar`; `doctor` section |
| `muninn/indexer.py` | fix: an exhausted event source must end `watch()` (see below) |
| `tests/test_raven.py` | **new** |
| `tests/test_indexer.py` | three tests for the `watch()` termination fix |

No new dependencies. Everything here is stdlib (`http.server`, `socketserver`).

## The descriptor

One file, `muninn.json`, in the **shared ravens directory** — resolved by this
rule, which every participant must implement identically:

1. `$RAVENS_STATE_DIR`, if set and non-empty.
2. Windows: `%LOCALAPPDATA%\Ravens` (`~\AppData\Local\Ravens` if unset).
3. POSIX: `$XDG_STATE_HOME/ravens`, else `~/.local/state/ravens`.

**This is not `muninn/paths.py`'s `STATE_DIR`.** That resolves to `.../muninn`,
and a descriptor written there is one the host never looks at — a silently empty
menubar with nothing on screen to explain it. `muninn/paths.py` is still the model
to follow for *conventions* (it honours `XDG_STATE_HOME`, it special-cases
Windows); it is the wrong *location*.

```json
{
  "api_version": 1,
  "display": "Muninn",
  "endpoints": { "menu": "/api/menu" },
  "host_priority": 50,
  "max_api": 1,
  "min_api": 1,
  "name": "muninn",
  "pid": 7092,
  "port": 61968,
  "started": 1785619470.680397
}
```

Four field-level decisions, each of which the protocol permits a raven to make
for itself:

- **`host_priority` 50**, against Huginn's 100. Huginn leads when both run;
  Muninn's section sorts first — alone — when it does not. Neither raven knows the
  other exists; these two numbers are the whole of the ordering.
- **`min_api`/`max_api` as a range, never an equality.** Huginn #38: one routine
  bump silently disabled every participant with nothing on screen to say why.
- **No `token_path`, no `token_header`.** See "Security" below. This is a decision,
  not an omission.
- **No `action` endpoint.** Every row is a link. A history console has nothing that
  should be mutated from a menu, and adding an action "just to open a session"
  would put a POST endpoint on this port permanently.

`started` is marked optional by the protocol and is **supplied anyway**: the host
cross-checks it against the OS's record of when `pid` began, and without it a
recycled PID passes as a live raven.

### Writing and removing it

- **Atomic**, via a temp file in the same directory, `fsync`, then `os.replace`.
- **0600 file in a 0700 directory**, chmodded *before* the replace. Creating the
  final file first and chmodding after leaves a world-readable window. Matches
  `store.py`'s discipline for the archive. No-op on Windows, which uses ACLs.
- **Published after the bind, never before.** A descriptor naming a port that is
  not yet listening makes the host report a healthy Muninn as unreachable.
- **Removed on clean shutdown**, including on SIGTERM (which is how a service
  manager stops a background indexer). Best-effort and no more: a `SIGKILL` skips
  it, and the host's pid + `started` check renders that as "Not running (its
  recorded process is gone)." with the reason on screen.

## The menu

`GET /api/menu` returns the shape Appistry's `menu_spec.parse_menu` accepts. Two
sections, both link-only:

```json
{
  "api_version": 1,
  "title": "Muninn",
  "sections": [
    { "id": "recent", "title": "Recent sessions", "items": [
        { "label": "Fixed the JSONL parser", "detail": "claude · 12h ago",
          "url": "/session/abc123", "style": "muted" },
        { "separator": true },
        { "label": "Search history…", "url": "/" } ] },
    { "id": "archive", "title": "Archive", "items": [
        { "label": "1,234 sessions · 98,765 chunks", "url": "/", "style": "muted" },
        { "label": "7 files not yet indexed", "url": "/", "style": "attention" },
        { "label": "2 sessions queued to index", "url": "/", "style": "muted" },
        { "label": "Last full scan", "detail": "11h ago", "url": "/", "style": "muted" } ] }
  ]
}
```

Content decisions:

- **Recent sessions**, capped at 8 (`RECENT_LIMIT`), from `store.log()`. Label
  falls back topic → title → basename of `cwd` → id prefix. Only the *basename* of
  `cwd` is used: a full path in a menubar row is unreadable at that width and
  discloses the user's directory layout.
- **Index freshness**, at `style: "attention"` — the one row a user might need to
  act on, and the same "staleness must be visible" principle `doctor` is built on.
- **No `badge`.** The host *sums* badges across ravens, so a corpus count here
  would render as thousands of pending decisions beside Huginn's approvals.
- **"never" is printed** when the archive has never been swept. A blank detail
  cannot be told from a missing one, which is exactly the invisible staleness this
  project has already been bitten by.
- An empty archive still emits the `archive` section, so "up but silent" is
  distinguishable from unreachable.

The payload is built by `raven.build_menu()` from plain data — no `Store`, no
I/O — so it can be checked against the host's parser with no database and no port.
Index lag is **not** recomputed per fetch: `ingest.index_lag` stats every
transcript in every root, and the host's menu budget is 2 seconds. Queue depth
stands in for it.

### Budgets

Appistry's caps are 12 sections, 50 items per section, 200 items total, 120-char
labels, 80-char details — and everything over is **dropped**, silently. Muninn
stays well inside all of them; `tests/test_raven.py` asserts that every non-
separator row Muninn emits survives the host's parser unchanged.

## Security

Bound loopback only, on an ephemeral port. Every rule the protocol requires:

- **`Host` must name a loopback address.** A missing `Host` is refused too —
  treating absence as acceptable is a one-line bypass. This is the DNS-rebinding
  defence: a page served from any other hostname carries that hostname here even
  when it resolves to 127.0.0.1.
- **Any `Origin` is refused**, including Muninn's own. Allowlisting
  `http://127.0.0.1:{port}` would let a page served from this very port script the
  endpoint, and an ephemeral port published in a file is not a trust boundary.
- **`Content-Length` is guarded before anything is read.** Negative is the case
  that matters: `read(-1)` means "until EOF", i.e. no bound at all. The cap is
  zero — this surface takes no bodies.
- **`GET` only.** `POST` answers 405 (after the guards, so a cross-origin POST
  gets 403 rather than a route-shaped answer that confirms what the port is).
- **Response headers from a fixed set**, never copied from the request. `nosniff`
  and `no-store` on everything; `default-src 'none'` CSP on HTML.
- A provider failure answers **500, not a dropped connection** — the host reports
  a closed connection as "not answering on its recorded port", which points the
  user at the wrong problem.

### The token decision

**Muninn advertises no `token_path`, so `/api/menu` is unauthenticated.**
Appistry's `raven_client.py` documents that a raven publishing no token gets an
unauthenticated request and that whether to accept that is the raven's call. This
spec accepts it, on the record:

- The endpoint is read-only and emits counts, relative times, and short labels —
  **no prose and no transcript text.** `/session/<id>` is deliberately a stub that
  echoes the id for `muninn show`; it does not render the session.
- Any local process that could read a 0600 token could equally read `muninn.db`,
  which is also 0600 and holds the entire corpus. A token would defend against
  nothing the archive's own permissions do not already decide.
- **What a token would not buy is the point.** The threat a loopback port faces is
  a web page in the user's browser, and a token does nothing about that — the page
  is refused for lacking one, the same outcome `Host`/`Origin` already produce.
  Those checks are therefore *not optional here*: with no credential they are the
  only thing between this port and any page the user has open, which is the
  opposite of the intuition that "no secret to steal" means less to defend.

**If a menu row ever carries prose, or an action, this decision must be revisited
in the same change.** `tests/test_raven.py` has a test that fails when prose
reaches the payload.

## Sanitisation

Session titles, topics, and `cwd` paths come from transcripts, which hold whatever
a user pasted and whatever a tool printed. In an archive of agent history that is
*mostly* terminal output, so hostile text is the normal case, not a contrived one.

Everything that becomes a label goes through `raven.safe_label()`: ANSI/OSC
escapes removed, C0/C1 control characters and DEL stripped, bidi overrides and
zero-width characters removed, whitespace collapsed, length capped. A non-string
becomes `""` rather than being coerced — `str()` on a dict would put `repr()`'s
attacker-chosen punctuation on screen. A label that sanitises to nothing falls
through to the next candidate, so no row is ever a blank clickable.

Appistry sanitises host-side too. That is not a reason to skip it, and the reader
who thinks it is has the threat model backwards: Appistry defends *itself* from a
hostile raven; this defends Muninn's users from hostile transcript content Muninn
is the one that read.

**Secrets: no redaction, by construction rather than by choice.** `corvidae`'s
`redact_secrets` is not used and Muninn does not depend on it. The reason is that
there is nothing here for a redactor to scan — a label is a topic, a title, a
`cwd` basename, or a count, and prompt/transcript text (where a leaked credential
would actually live) never reaches this payload. Adding a redactor would imply
prose is expected here, which is the opposite of the invariant. Enforced by test,
not asserted.

## Acceptance criteria

1. Descriptor written when serving begins, at the shared-directory path, 0600 in
   a 0700 directory — including under `umask 0`.
2. Descriptor removed on clean shutdown, on SIGINT, and on SIGTERM; `withdraw` is
   idempotent.
3. A stale descriptor (crash) carries `pid` and `started` sufficient for the
   host's liveness check to refuse it.
4. Field values exactly as tabulated; declared range overlaps the host's; no
   `token_path`; no `action` endpoint.
5. The advertised port is listening by the time the descriptor exists.
6. The payload parses under Appistry's real `menu_spec.parse_menu` with **no row
   dropped and no string repaired**.
7. `Host` non-loopback → 400; missing `Host` → 400; any `Origin` → 403; bad or
   negative `Content-Length` → 413; `POST` → 405; guards apply to every route.
8. Hostile transcript text cannot put a control character, ANSI escape, bidi
   override, CR or LF into any label.
9. `attach()` returns `None` rather than raising when it cannot bind or publish —
   a menubar section must never cost the indexer its ingest.
10. The menu is rebuilt per request, and the provider works from a request thread
    (its own `Store`, since `sqlite3` connections are not thread-safe).

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn doctor                     # shared-menubar section, no errors
uv run muninn index --watch              # descriptor appears; Ctrl-C removes it
```

Verified additionally against the real consumer: `roost.ravens.discover`,
`roost.raven_client.fetch_menu`, and `roost.menu_spec.parse_menu` at the commit
named above, over a live loopback fetch.

## The `muninn/indexer.py` fix, and why it is in this spec

`watch()` read its event source with `next(generator, None)`. But
`watchfiles.watch` is called with `raise_interrupt=False`, so Ctrl-C makes its
generator **return** rather than propagate — and `None` is also what an ordinary
timeout tick yields. So an interrupted watcher spun forever at full speed,
ignored SIGINT, and had to be SIGKILLed. That bug predates this spec (reproduced
on `main`), but it is fixed here because spec 009 is what makes it *matter*: a
watcher that cannot be stopped cleanly strands a descriptor naming a dead port.

The fix uses a sentinel object distinct from every value the generator can yield.
`None` is not usable as "exhausted" here, and neither is falsiness — `set()` is
falsy and is exactly what an idle poll produces, so testing truthiness would trade
a spin for a watcher that quietly stops watching.

**This required editing `tests/test_indexer.py`, which spec 008 lists as
do-not-modify.** Only additions: one new `WatchTerminationTest` class, no existing
test changed. Flagged rather than done quietly. The existing suite could not have
caught this, because every test passes `max_iterations`, which bounds the loop and
therefore hides a loop that never ends on its own.

## Guardrails

- **Do not import Appistry.** It is a consumer of this protocol, not a dependency.
  `tests/test_raven.py` reimplements its parser locally and says why.
- **Do not publish to `muninn/paths.py`'s `STATE_DIR`.** The host does not look
  there and the failure is silent.
- **Do not compare `api_version` for equality.**
- **Do not add an `action` endpoint** without revisiting the token decision in the
  same change.
- **Do not put prose, transcript text, or a full path in a menu label.**
- **Do not let the menubar break ingest.** `attach()` returns `None`; it never
  raises into the watcher.
- **Do not invent a daemon.** If Muninn needs to be present independently of the
  indexer, that is an owner decision (see above), not an implementation detail.
- **Do not modify** `tests/test_losslessness.py`, `tests/test_ledger.py`,
  `tests/test_query.py`, `tests/test_queue.py`, `tests/test_exports.py`,
  `tests/test_version.py`.
- Windows CI note: this repo pins `shell: bash`. The tests here bind loopback
  sockets in-process (no subprocess fan-out) and skip mode-bit assertions on
  Windows; see `WINDOWS.md`.

## Out of scope

- **The console.** `/` and `/session/<id>` are deliberately stubs. A real UI on
  this port would carry prose and would force the token decision to be reopened.
- **Host election.** Ravens do not participate; Appistry elects its own host by
  lock file.
- **Huginn's side.** Implemented separately in its own repository.
- **Actions of any kind.** Link rows only.
