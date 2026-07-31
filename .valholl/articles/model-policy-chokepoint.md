---
type: "Knowledge Article"
title: "Model policy chokepoint"
description: "A fail-closed, intersecting policy resolver lets a distribution restrict which models may be used; an additive plugin registry cannot."
tags: ["policy", "security", "governance", "plugins"]
timestamp: "2026-07-30T00:00:00Z"
category: "governance"
status: "current"
updated: "2026-07-30"
summary: "Muninn routes every LLM and embedding call through one ModelPolicy resolver. Policies intersect and never union, so a distribution can narrow the allowed model set and nothing downstream can widen it. This makes a restricted-model contract testable rather than aspirational."
related: ["lessons-for-huginn", "what-muninn-is"]
---

# Model policy chokepoint

Some deployments must enforce an explicit contract: **only approved model
providers may be used**, with no fallback to others — including local ones. An
organization may be entitled to exactly one hosted provider and required to route
all inference through it.

## Why this cannot be a plugin

An additive plugin registry cannot express a restriction. Plugins contribute
providers, and nothing can veto a contribution, because:

- Plugin load order is arbitrary.
- A memoized registry fixes state at first read.
- Anything one plugin removes, another plugin can add back.

Enforcement therefore belongs in **core**, as a chokepoint a distribution pins
closed.

## The design

Every LLM and embedding call routes through one resolver. Contributed policies
**intersect** — a contributor may only *narrow* the allowed set, never widen it.
Config, environment variables, and CLI flags may narrow but never widen. No match
means **refuse**, not fall back.

```python
@dataclass(frozen=True)
class ModelPolicy:
    name: str
    allow: tuple[str, ...]          # regex allowlist
    require_provider: str | None    # a specific approved provider
    reason: str                     # shown verbatim on refusal
```

Declared in package metadata rather than registered by plugin code, so the
restriction is a property of what is installed:

```toml
[project.entry-points."muninn.policy"]
restricted = "my_distribution.policy:APPROVED_ONLY"
```

## Three-layer enforcement

1. **Dependency** — a restricted distribution does not install the optional
   providers it forbids, so those code paths are absent and cannot be selected.
2. **Runtime** — the resolver refuses any provider or model id outside the
   allowlist, including one contributed by a future plugin.
3. **CI** — a test asserts the resolved policy set is exactly what is expected and
   that no other provider is importable. Drift becomes a build failure rather
   than a silent capability.

## Consequence: subprocess providers may be disallowed

Shelling out to a vendor CLI is attractive — it can ride a subscription instead
of per-token billing — but such a subprocess **inherits the user's own
configuration** and can egress to an endpoint the policy would otherwise forbid,
entirely outside Muninn's control.

Under a restricted contract, providers must call approved APIs directly. Two
implications:

- Enrichment there incurs real API spend rather than riding a subscription.
- Core must expose enrichment behind the same provider protocol so both the
  permissive and restricted paths work — which it needs anyway for the optional
  local-embedding split.

## A failed policy load must refuse, not disappear

Discovered during implementation, and it corrects an omission in the design above.

If a policy entry point raises while loading, the tempting behaviour is to skip it
— that is what a plugin registry does for a broken *capability* plugin, and it is
right there, because a missing provider only removes an option.

A policy is the opposite. Dropping a broken **restrictive** policy *widens* the
effective permission set, which is the one thing this module exists to prevent. A
deployment that installed a Bedrock-only policy and then shipped a version where
that policy fails to import would silently become unrestricted.

So a failed policy load becomes a synthetic policy that refuses everything, whose
`reason` names the entry point and the exception class:

```python
ModelPolicy(name=..., allow=(), reason="policy entry point 'x' failed to load: ValueError")
```

The failure mode is then loud and safe rather than quiet and permissive. This
deserves its own test, because it is exactly the behaviour a later refactor would
"simplify" away — the code looks like a special case for an error path, and only
the reasoning above explains why it is load-bearing.

Note the asymmetry with the plugin registry, which *does* isolate and skip a
broken plugin (see `plugins.py`). Same word, opposite correct answer, because one
adds capability and the other removes permission.

## Honest scope

This is a **strong contract, not a sandbox**. It governs Muninn's own calls. It
cannot stop a user running any model in a different tool, and anyone with write
access to the environment can edit anything. What it provides is an explicit,
testable, CI-verified statement about what Muninn-as-shipped will talk to — which
is what makes it reviewable. The goal is preventing accidental violation and
making drift detectable, not idiot-proofing.
