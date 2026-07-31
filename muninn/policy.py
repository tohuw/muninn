"""The model policy chokepoint: every LLM and embedding call routes through here.

Normative source: .valholl/articles/model-policy-chokepoint.md. The problem it
solves: Huginn's plugin registry is purely additive
(.valholl/articles/lessons-for-huginn.md #6, filed as tohuw/huginn#41) — plugins
contribute providers and nothing can veto one, so a distribution cannot say
"only these models may be used." Enforcement therefore cannot live in a plugin;
it has to be a chokepoint in core that a distribution pins closed.

Two rules that are not negotiable, because either one being wrong defeats the
whole point of a "restricted contract":

1. **Policies intersect, never union.** A call is permitted only if *every*
   loaded policy permits it. A contributor, a config value, an env var, or a
   CLI flag may narrow the allowed set; none of them may widen it.
2. **No match means refuse, not fall back.** If no policy addresses a
   (model, provider) pair, that pair is refused. There is no permissive
   fallback path distinct from the explicit default policy below.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Iterable

POLICY_ENTRY_POINT_GROUP = "muninn.policy"


@dataclass(frozen=True)
class ModelPolicy:
    """One contributor's statement of which (model, provider) pairs it permits.

    ``allow`` patterns are matched with ``re.search``, not ``re.fullmatch`` —
    a policy author who wants a strict prefix restriction must anchor the
    pattern with ``^`` themselves (see ``allow=("^us\\.anthropic\\.",)`` in
    tests/test_policy.py). ``re.search`` without an explicit ``^`` matches
    anywhere in the model id, which is deliberate: it lets a policy allow-list
    a vendor prefix embedded in a longer qualified id
    (e.g. ``bedrock/us.anthropic.claude-...``) without every author having to
    remember to write ``.*`` first. The anchoring behaviour is a per-pattern
    choice, not a global fullmatch-vs-search switch, because a global
    fullmatch would silently break every existing unanchored pattern the
    moment one policy author wanted a suffix-only match.
    """

    name: str
    allow: tuple[str, ...]          # regex allowlist of model ids, re.search semantics
    require_provider: str | None = None    # None = any provider
    reason: str = ""                       # shown verbatim on refusal


# The permissive default is a real ModelPolicy, not a bypass branch, so the
# unrestricted path exercises the exact same intersection/fail-closed code as
# a restricted build. A permissive default that were merely the *absence* of
# a policy would mean restricted and unrestricted builds run different code —
# and the restricted path is precisely the one nobody can afford to be untested
# in normal use. See model-policy-chokepoint.md, "The default is permissive
# but real."
DEFAULT_POLICY = ModelPolicy(
    name="default",
    allow=(".*",),
    require_provider=None,
    reason="no restriction configured",
)


class PolicyRefused(RuntimeError):
    """Raised by ``check()`` when at least one loaded policy refuses the pair."""


def _permits(policy: ModelPolicy, model: str, provider: str) -> bool:
    if policy.require_provider is not None and provider != policy.require_provider:
        return False
    return any(re.search(pattern, model) for pattern in policy.allow)


def _fail_closed(name: str, exc: BaseException) -> ModelPolicy:
    """A policy entry point that fails to load must not silently vanish.

    Dropping a broken *restrictive* policy would widen what is permitted —
    exactly the failure mode this whole module exists to prevent. So a load
    failure becomes a policy that allows nothing, rather than a policy that
    is simply absent. Only the exception class name is embedded in the
    reason: a message can carry transcript text or credentials, per the same
    rule receipt.py follows for import errors.
    """
    return ModelPolicy(
        name=f"{name}(load-error)",
        allow=(),
        require_provider=None,
        reason=f"policy entry point {name!r} failed to load: {type(exc).__name__}",
    )


def resolve() -> tuple[ModelPolicy, ...]:
    """Discover contributed policies via entry points; fall back to the default.

    Deliberately uncached (unlike ``plugins.discover_plugins()``): policy
    resolution is cheap — no I/O beyond entry-point metadata already loaded by
    the interpreter — and leaving it uncached means tests can monkeypatch
    ``importlib.metadata.entry_points`` per-test without also having to manage
    cache invalidation.
    """
    policies: list[ModelPolicy] = []
    for ep in entry_points(group=POLICY_ENTRY_POINT_GROUP):
        try:
            candidate = ep.load()
        except Exception as exc:  # noqa: BLE001 - must not propagate; see _fail_closed
            policies.append(_fail_closed(ep.name, exc))
            continue
        if not isinstance(candidate, ModelPolicy):
            policies.append(_fail_closed(ep.name, TypeError(f"not a ModelPolicy: {candidate!r}")))
            continue
        policies.append(candidate)

    if not policies:
        return (DEFAULT_POLICY,)
    return tuple(policies)


def check(model: str, provider: str) -> None:
    """Raise ``PolicyRefused`` unless every resolved policy permits the pair.

    This is the entire chokepoint. A provider that calls out without routing
    through ``check()`` first is a defect — there is deliberately no other
    supported way to ask "is this call allowed."
    """
    refusing = [p for p in resolve() if not _permits(p, model, provider)]
    if refusing:
        reasons = "; ".join(p.reason for p in refusing)
        raise PolicyRefused(f"refused {provider}:{model} — {reasons}")


def effective_providers(candidates: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """Filter ``(model, provider)`` candidates to those every policy permits.

    Named ``effective_providers`` (not the spec draft's ``effective_allowed``)
    to match what the Cisco distribution's policy tests already call; the two
    names were reconciled to this one rather than shipped as two competing
    spellings.
    """
    policies = resolve()
    return tuple(
        (model, provider)
        for model, provider in candidates
        if all(_permits(p, model, provider) for p in policies)
    )
