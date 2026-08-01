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
3. **A failure to *discover* a policy narrows, never widens.** Not just a
   failure to load one. "I could not read the metadata that would have told me
   a policy exists" and "no policy exists" are different facts, and treating
   the first as the second is how a restricted build silently unrestricts
   itself. See model-policy-chokepoint.md, "Discovery is the attack surface,
   not just loading" — that section records a working exploit against the
   first implementation of this module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import distributions
from typing import Any, Iterable

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


def _normalise(name: str) -> str:
    """PyPA's distribution-name normalisation: runs of ``-_.`` collapse, lowercased."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _policy_entry_points() -> tuple[list[Any], tuple[str, ...]]:
    """Every ``muninn.policy`` entry point on ``sys.path``, plus shadowed dist names.

    **Why this walks ``distributions()`` instead of calling
    ``entry_points(group=...)``.** ``entry_points()`` deduplicates by
    *normalised distribution name, first on ``sys.path`` wins*, and it applies
    that rule before anything looks at which groups a distribution actually
    contributes. So a directory containing nothing but
    ``<same-name>-9.9.dist-info/METADATA`` — no ``entry_points.txt`` at all —
    placed earlier on ``sys.path`` masks the real distribution entirely, and
    ``entry_points(group="muninn.policy")`` returns ``[]``. ``resolve()`` then
    cannot tell that from "no policy is installed" and hands back the
    permissive ``DEFAULT_POLICY``: an installed exclusion policy is defeated by
    a directory of two files and a ``PYTHONPATH`` entry, with no error anywhere.

    Verified against this module before the fix, with a real policy installed
    that allowed only ``^us\\.anthropic\\.``::

        A. real policy installed:  resolved ['no-local-model']  local-llama-7b REFUSED
        B. shadow dist prepended:  resolved ['default']         local-llama-7b ALLOWED

    Walking distributions ourselves keeps both, so a masked distribution's
    policy still binds. The cost is real: a genuinely duplicate-installed
    distribution now contributes its policy twice. That is the right direction
    to be wrong in — policies intersect, so a duplicate is redundant rather
    than dangerous, whereas a dropped one is the fail-open above.

    The duplicate normalised names are returned rather than merely tolerated,
    because two distributions claiming one name *is* the shadowing signal.
    ``doctor`` prints them; see ``cli._print_policy_section``.
    """
    eps: list[Any] = []
    seen: dict[str, int] = {}
    for dist in distributions():
        try:
            contributed = [ep for ep in dist.entry_points if ep.group == POLICY_ENTRY_POINT_GROUP]
            # .metadata can raise on a malformed/truncated METADATA file, so it
            # is read inside the guard: a distribution we cannot name must not
            # cost us the policies we already collected.
            name = _normalise(str(dist.metadata["Name"] or ""))
        except Exception:   # noqa: BLE001 - see below
            # A distribution whose metadata cannot be parsed is skipped, not
            # fatal. This is the one place a *discovery* failure loses a
            # policy, and it is unavoidable — an unreadable entry_points.txt
            # cannot tell us what it would have said. It does not widen
            # anything relative to the alternative, which is propagating and
            # taking down every policy including the ones that parsed.
            #
            # ``Exception`` only, unlike the load guard in ``resolve()``:
            # reading and parsing metadata does not execute third-party code,
            # so ``SystemExit`` is not a realistic outcome here, and swallowing
            # a ``KeyboardInterrupt`` per-distribution would make this loop
            # resist Ctrl-C for no benefit.
            continue
        if not contributed:
            continue
        eps.extend(contributed)
        if name:   # a nameless distribution cannot be reported as a duplicate of anything
            seen[name] = seen.get(name, 0) + 1
    return eps, tuple(sorted(n for n, count in seen.items() if count > 1))


def shadowed_distribution_names() -> tuple[str, ...]:
    """Normalised names contributing a policy from more than one distribution.

    A restricted build should have exactly one distribution per name. Two means
    either a broken install or something deliberately shadowing the policy
    distribution (see ``_policy_entry_points``), and both deserve a loud line
    in ``doctor`` rather than silence.
    """
    return _policy_entry_points()[1]


def resolve() -> tuple[ModelPolicy, ...]:
    """Discover contributed policies via entry points; fall back to the default.

    Deliberately uncached (unlike ``plugins.discover_plugins()``): policy
    resolution is cheap — no I/O beyond entry-point metadata already loaded by
    the interpreter — and leaving it uncached means a test can point
    ``sys.path`` at a fixture distribution per-test without also having to
    manage cache invalidation.

    Note for anyone writing a test here: monkeypatching
    ``muninn.policy.entry_points`` no longer works, because this function does
    not call it. That is not incidental — the old tests all patched it, which
    is exactly why the masking bug documented in ``_policy_entry_points`` was
    invisible to a green suite. Patch ``muninn.policy._policy_entry_points``
    for unit tests, and see ``ShadowedDistributionTest`` in tests/test_policy.py
    for the one that builds real ``.dist-info`` directories on disk.
    """
    policies: list[ModelPolicy] = []
    for ep in _policy_entry_points()[0]:
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
