---
type: "Knowledge Article"
title: "Model policy chokepoint"
description: "A fail-closed, intersecting policy resolver lets a distribution restrict which models may be used; an additive plugin registry cannot."
tags: ["policy", "security", "governance", "plugins", "fail-closed"]
timestamp: "2026-07-30T00:00:00Z"
category: "governance"
status: "current"
updated: "2026-08-01"
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

## Discovery is the attack surface, not just loading

The section above got the rule right and the *scope* wrong. It guarded the
moment a policy **loads**. The first implementation was defeated a layer
earlier, at the moment a policy is **discovered** — and there the fail-open was
total and silent.

`importlib.metadata.entry_points()` deduplicates by **normalised distribution
name, first on `sys.path` wins**, and applies that rule *before* anything
examines which entry-point groups a distribution contributes. So a directory
holding nothing but `<same-name>-9.9.dist-info/METADATA` — no
`entry_points.txt` at all — placed earlier on `sys.path` masks the real
distribution entirely. `entry_points(group="muninn.policy")` returns `[]`,
`resolve()` cannot distinguish that from "no policy is installed," and it hands
back the permissive `DEFAULT_POLICY`.

Measured against `muninn/policy.py`, with an exclusion policy installed that
allowed only `^us\.anthropic\.`:

```
A. real policy installed:   resolved ['no-local-model']  -> local-llama-7b REFUSED
B. shadow dist prepended:   resolved ['default']         -> local-llama-7b ALLOWED
```

Two files and a `PYTHONPATH` entry, no error printed anywhere. The same class of
failure applies if the real distribution's `entry_points.txt` is merely deleted
or corrupt — no adversary required.

**The rule this generalizes to: a failure to *discover* a policy must narrow,
never widen.** "I could not read the metadata that would have told me a policy
exists" and "no policy exists" are different facts. Every default in this module
already assumed the second when it saw the first.

The fix is to walk `importlib.metadata.distributions()` and collect each
distribution's `entry_points` filtered to the policy group, rather than trusting
`entry_points(group=...)` to have kept them. Against the shadow setup above, the
deduplicating call saw `[]` while the distribution walk saw the real policy.

This trades one wrongness for a much better one. A genuinely
duplicate-installed distribution now contributes its policy **twice** — which
is harmless, because policies intersect, so a duplicate is redundant rather
than permissive. A dropped policy is the fail-open. When two distributions do
share a normalised name, that duplication *is* the shadowing signal, so
`doctor` reports it by name rather than tolerating it quietly.

### Why the tests could not have caught this

Every test in `tests/test_policy.py` monkeypatched
`importlib.metadata.entry_points`. The bug was *in how policies are
discovered*, so patching the discovery call replaced the buggy code with a
mock. A mock-only suite can only test what happens after discovery succeeds,
and the suite was green throughout.

The lesson is not "mock less." It is that **the seam you mock is the seam you
stop testing**, and for a security control the discovery seam is the one that
matters most. `tests/test_policy.py::ShadowedDistributionTest` therefore builds
real `.dist-info` directories on disk and probes them in a subprocess — the
only test in that file exercising real metadata resolution, and the only one
that fails against the old code.

## Two ways an `allow` tuple fails open

Both found by review of the same module, and both silent.

**The missing comma.** `ModelPolicy.allow` is annotated `tuple[str, ...]`, but
a frozen dataclass validates nothing. Writing:

```python
allow=r"^us\.anthropic\."        # str, not a 1-tuple — no comma
```

makes the permit check iterate **characters**. Since `.` matches any character,
almost any realistic vendor prefix degenerates to allow-everything: an
exclusion policy written this way permitted `gpt-4o-EVIL`,
`totally-unapproved`, and `x`. `bool(allow)` stays truthy, so an
"empty allow-list" guard passes too. Neither Python nor ruff objects.

**The malformed pattern.** Patterns compiled lazily at check time meant
`allow=("[unclosed",)` raised `re.error` *out of the permit check*. `re.error`
is not `PolicyRefused`, so a caller catching only `PolicyRefused` got a crash
rather than a refusal — and a caller whose broad handler treats unrecognised
exceptions as transient **retried** instead of refusing.

`__post_init__` now rejects a non-`tuple` `allow`, rejects non-`str` elements,
and `re.compile`s each pattern eagerly. A policy that fails validation raises
during `resolve()`'s load, which routes it to the refuse-everything path above
— it is never a policy that got skipped.

## `SystemExit` is not an `Exception`

`ep.load()` imports arbitrary third-party code, and a guard written
`except Exception` does not cover `SystemExit` or `KeyboardInterrupt`, neither
of which inherits from `Exception`. A stray `sys.exit()` at a policy module's
import time therefore propagated out of every function in `policy.py`,
including `check()` — turning the chokepoint into an interpreter shutdown
rather than a refusal.

`plugins.py` had the identical gap around its own `ep.load()`, where the
consequence was that one plugin author's debugging leftover killed the `doctor`
command whose entire job is to report the plugin is broken. Both are fixed the
same way, and deliberately kept in step so the two entry-point loaders cannot
diverge on what counts as isolable.

The guards spell out `except (Exception, SystemExit, KeyboardInterrupt)` rather
than `except BaseException`, so the choice reads as per-exception and
deliberate, and a genuinely unrecoverable `BaseException` still propagates.

The mirrored rule for the *permit check* is the opposite one, and the asymmetry
is intentional: `_permits` catches `Exception` only. Nothing a regex can do
raises `SystemExit`, but a pathological pattern can backtrack for a long time —
which is exactly when a human reaches for Ctrl-C, and swallowing that to report
a refusal would make the function unkillable.

## Flatten errors at the boundary, not at the call

A provider that flattens exceptions from its *requests* is only half-protected.
Client or connection construction throws too, and those exceptions are often the
chattiest ones available: botocore's `NoRegionError`, `ProfileNotFound`,
`InvalidConfigError` and `EndpointConnectionError` can carry profile names,
config file paths and endpoint hostnames.

Found by testing rather than by reading. The Bedrock plugin correctly wrapped its
`converse` call, but its client constructor sat outside the `try`, so:

```python
with mock.patch("boto3.client", raises(ValueError("secret sk-ant-LEAK123"))):
    provider.generate("hi")
# -> ValueError('secret sk-ant-LEAK123')   raw message, planted secret intact
```

**The rule: flatten at the boundary of the provider, so every path out is
covered by construction rather than by remembering.** Flattening inside the
client helper protects every call site; flattening around one call protects one
call.

The test that pins this must exercise **both** paths — an exception from
construction and an exception from the request — and assert in each case that the
message matches `^<Provider> request failed \(\w+\)$` and does not contain a
planted secret. A single-path test passes while the other path leaks, which is
exactly what happened here.

This generalizes past model providers: it applies to any plugin that talks to a
remote system, including a history source, and it is the same discipline the
import ledger already follows in storing only exception class names.

## Honest scope

This is a **strong contract, not a sandbox**. It governs Muninn's own calls. It
cannot stop a user running any model in a different tool, and anyone with write
access to the environment can edit anything. What it provides is an explicit,
testable, CI-verified statement about what Muninn-as-shipped will talk to — which
is what makes it reviewable. The goal is preventing accidental violation and
making drift detectable, not idiot-proofing.
