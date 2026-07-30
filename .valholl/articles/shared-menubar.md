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

Each raven publishes a descriptor at `~/.local/state/ravens/<name>.json`:

```json
{
  "name": "muninn",
  "display": "Muninn",
  "api_version": 1,
  "min_api": 1,
  "port": 47101,
  "token_path": "~/.local/state/muninn/token",
  "endpoints": { "menu": "/api/menu", "open": "/" },
  "pid": 12345
}
```

The host fetches `/api/menu` from each live raven and renders the returned
sections in a stable order.

## Host election

- A single lock decides the host. **Huginn hosts when running**; Muninn detects
  it, defers, and contributes its section.
- When Huginn is absent, Muninn ships and runs the *same binary* standalone.
- Descriptor liveness is checked by pid (`kill(pid, 0)`), matching how Huginn
  already discovers a pre-existing daemon via `daemon.json`.
- One raven in the menubar, two minds behind it.

## Design rules

- **Compatibility ranges, not exact match.** Huginn's plugin registry requires
  `api_version` to match exactly, which silently disables plugins on any bump.
  The raven protocol declares `min_api`/`max_api` and degrades loudly.
- **Dumb view.** The host never interprets a companion's data; it renders labels
  and forwards action ids back to the owning raven.
- **Fail soft.** An unreachable raven yields a disabled section with a reason,
  never a broken menu or a hung host.
- **Auth per raven.** Each raven keeps its own loopback token; the host reads each
  descriptor's `token_path` and never shares tokens across ravens.
- Fix Huginn's stale hardcoded `repoPath` as part of this work.

## Scope note

This is a coordinated change to public Huginn (branch `master`) and must land
with a version bump on both sides. The Swift app currently has no tests, which is
a risk worth addressing in the same change.
