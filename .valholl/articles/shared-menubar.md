---
type: "Knowledge Article"
title: "The shared menubar (menu-as-data)"
description: "One raven in the menubar hosting both Huginn and Muninn, driven by a declarative JSON menu spec."
tags: ["menubar", "macos", "swift", "extensibility", "huginn"]
timestamp: "2026-07-30T00:00:00Z"
category: "extensibility"
status: "current"
updated: "2026-07-30"
summary: "Nobody wants two ravens in their menubar. A shared RavenMenuBar renders a JSON menu spec fetched from each discovered raven, so Huginn and Muninn coexist in one surface with host election, and either can run standalone."
related: ["what-muninn-is", "lessons-for-huginn"]
---

# The shared menubar (menu-as-data)

Huginn is Thought, Muninn is Memory. They are complementary, so they share one
menubar surface. Nobody wants two ravens up there — let alone fifty apps.

Huginn's existing menubar cannot host a companion: `HuginnMenuBar.swift` is ~275
lines of imperative AppKit that calls `menu.removeAllItems()` and re-adds every
item from scratch on a 3-second timer, with no injection point. This is a
**rewrite**, deliberately, not a patch.

## Menu-as-data

A shared `RavenMenuBar` component renders a **declarative JSON menu spec** served
by each raven. Companions control their own menu content with zero Swift changes,
and the Swift layer stays a dumb view — preserving the property that makes
Huginn's app easy to reason about.

Each raven publishes a descriptor into a shared directory — `$RAVENS_STATE_DIR` if
set, else `%LOCALAPPDATA%\Ravens` on Windows, else `$XDG_STATE_HOME/ravens`
falling back to `~/.local/state/ravens`. Muninn's, as actually shipped:

```json
{
  "api_version": 1,
  "min_api": 1,
  "max_api": 1,
  "name": "muninn",
  "display": "Muninn",
  "pid": 7092,
  "port": 61968,
  "started": 1785619470.680397,
  "host_priority": 50,
  "endpoints": { "menu": "/api/menu" }
}
```

The host fetches `/api/menu` from each live raven and renders the returned
sections in a stable order.

Three corrections to the sketch this article originally carried, learned from the
implementation (docs/specs/009-raven-descriptor-menu.md, and Appistry's `SPEC.md`
which is normative for the wire format):

- **`max_api` and `started` are both needed.** `min_api` alone is not a range, and
  without `started` a recycled PID passes the host's liveness check — so a user
  sees a Muninn section that is not backed by anything running.
- **`host_priority` is what orders the menu**, not the host's knowledge of who
  should lead. Muninn declares 50 against Huginn's 100.
- **Muninn publishes no `token_path`.** The sketch assumed one. The endpoint is
  read-only, emits no prose, and any process that could read a 0600 token could
  read `muninn.db` — which is also 0600 and holds the whole corpus. What a token
  would not buy is the point: the real threat to a loopback port is a web page in
  the user's browser, and the `Host`/`Origin` checks are what stop that, token or
  no token.

## Host election

- A single lock decides the host, and **the ravens do not participate in it.** The
  menubar app elects its own host; neither Huginn nor Muninn is ever asked to be
  one, and neither needs to know who is. (The original sketch had Muninn "detect
  Huginn and defer", which is a coordination step the protocol removed entirely —
  neither raven knows the other exists.)
- Which raven *leads the menu* is a separate question, answered by data:
  `host_priority`, descending. Huginn leads when both run; Muninn's section sorts
  first, alone, when it does not.
- Descriptor liveness is checked by pid *and* `started`, cross-checked against the
  OS's record of when that process began. `kill(pid, 0)` alone cannot tell a live
  raven from an unrelated process that inherited a recycled PID.
- One raven in the menubar, two minds behind it.

## Muninn is only present while its indexer runs

Muninn has no daemon, and the shared menubar did not justify inventing one. The
descriptor is published by `muninn index --watch` — the one process that already
runs for as long as the machine is up — and withdrawn when it stops. So **Muninn
is absent from the menubar whenever the watcher is not running**, which the host
renders as a raven that was never installed, and a crashed watcher's stale
descriptor renders as "Not running" with the reason on screen. Both are legitimate
steady states; a raven that lied about being reachable would be worse. Whether
Muninn *should* be present independently of the indexer is an open owner decision
recorded in docs/specs/009.

## Design rules

- **Compatibility ranges, not exact match.** Huginn's plugin registry requires
  `api_version` to match exactly, which silently disables plugins on any bump.
  The raven protocol declares `min_api`/`max_api` and degrades loudly.
- **Dumb view.** The host never interprets a companion's data; it renders labels
  and forwards action ids back to the owning raven.
- **Fail soft.** An unreachable raven yields a disabled section with a reason,
  never a broken menu or a hung host.
- **Auth per raven, and the host never mints one.** Each raven owns its own
  loopback credential; the host reads that raven's `token_path`, sends it only to
  that raven's port, and never caches or shares one. A raven publishing no
  `token_path` gets unauthenticated requests — whether that is acceptable is the
  raven's decision, and Muninn's answer is recorded in docs/specs/009.
- **Every raven defends its own port.** The host is not a security boundary on a
  raven's behalf: it never forwards an inbound request, so anything reaching a
  raven's port came from somewhere else. Bind loopback, validate `Host`, refuse any
  `Origin`, guard `Content-Length`.
- Fix Huginn's stale hardcoded `repoPath` as part of this work.

## Scope note

The menubar itself shipped as a separate Python project (`tohuw/appistry`), not as
a rewrite inside Huginn's Swift app — so the "coordinated change to public Huginn
with a version bump on both sides" this article originally anticipated became three
independent implementations of one documented protocol instead. Appistry's
`SPEC.md` is normative for the wire format; Muninn's producer side is
docs/specs/009 and Huginn's is its own.
