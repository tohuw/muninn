"""The model policy chokepoint contract.

Every LLM/embedding call is supposed to route through ``check()``. These
tests encode the invariants from
.valholl/articles/model-policy-chokepoint.md — see docs/specs/008-plugin-contract.md
acceptance criteria 1-7. Policies intersect; they never union. No policy
matching a (model, provider) pair means refuse, never fall back.
"""
from __future__ import annotations

import unittest
from unittest import mock

from muninn.policy import DEFAULT_POLICY, ModelPolicy, PolicyRefused, check, effective_providers, resolve


def _fake_entry_points(*policies: ModelPolicy):
    """Build a fake ``entry_points(group=...)`` result carrying ``policies``.

    Mirrors the shape ``importlib.metadata.entry_points(group=...)`` returns:
    an iterable of objects with ``.name`` and ``.load()``. Monkeypatching this
    function (rather than installing a real distribution) is what the spec
    asks for — no subprocess, no real package on sys.path.
    """
    eps = []
    for i, policy in enumerate(policies):
        ep = mock.Mock()
        ep.name = policy.name or f"policy{i}"
        ep.load = mock.Mock(return_value=policy)
        eps.append(ep)
    return eps


def _resolve_with(*policies: ModelPolicy):
    return mock.patch("muninn.policy.entry_points", return_value=_fake_entry_points(*policies))


class IntersectionTest(unittest.TestCase):
    def test_only_the_overlap_of_two_policies_passes(self) -> None:
        # Criterion 1: A allows {a,b}, B allows {b,c} -> only b passes.
        a = ModelPolicy(name="a", allow=("^a$", "^b$"), reason="policy a")
        b = ModelPolicy(name="b", allow=("^b$", "^c$"), reason="policy b")
        with _resolve_with(a, b):
            check("b", "anyprovider")  # permitted by both -> no raise
            with self.assertRaises(PolicyRefused):
                check("a", "anyprovider")  # b refuses
            with self.assertRaises(PolicyRefused):
                check("c", "anyprovider")  # a refuses


class FailClosedTest(unittest.TestCase):
    def test_model_matching_no_policy_is_refused(self) -> None:
        # Criterion 2: no policy addresses this model -> refuse, not fall back.
        only = ModelPolicy(name="only", allow=("^known-model$",), reason="only known-model")
        with _resolve_with(only):
            with self.assertRaises(PolicyRefused):
                check("unlisted-model", "anyprovider")


class ReasonSurfacedTest(unittest.TestCase):
    def test_refusal_message_contains_each_refusing_reason_verbatim(self) -> None:
        # Criterion 3.
        a = ModelPolicy(name="a", allow=("^ok$",), reason="REASON_ALPHA_TOKEN")
        b = ModelPolicy(name="b", allow=("^ok$",), reason="REASON_BETA_TOKEN")
        with _resolve_with(a, b):
            with self.assertRaises(PolicyRefused) as ctx:
                check("not-ok", "anyprovider")
            message = str(ctx.exception)
            self.assertIn("REASON_ALPHA_TOKEN", message)
            self.assertIn("REASON_BETA_TOKEN", message)


class ConfigCannotWidenTest(unittest.TestCase):
    def test_explicitly_configured_model_still_refused(self) -> None:
        # Criterion 4: "config" here is simulated by calling check() with a
        # model a human/CLI flag explicitly requested. The policy still wins.
        restrictive = ModelPolicy(name="restrictive", allow=("^approved-model$",),
                                   reason="only approved-model may be used")
        requested_by_flag = "forbidden-model"  # what --model on the CLI named
        with _resolve_with(restrictive):
            with self.assertRaises(PolicyRefused):
                check(requested_by_flag, "anyprovider")


class RequireProviderTest(unittest.TestCase):
    def test_right_model_wrong_provider_is_refused(self) -> None:
        # Criterion 5.
        policy = ModelPolicy(name="provider-locked", allow=("^claude-.*",),
                              require_provider="bedrock", reason="bedrock only")
        with _resolve_with(policy):
            check("claude-sonnet-5", "bedrock")  # fine
            with self.assertRaises(PolicyRefused):
                check("claude-sonnet-5", "anthropic-api")  # right model, wrong provider


class PermissiveDefaultTest(unittest.TestCase):
    def test_default_applies_with_no_policy_plugins_and_is_a_real_policy(self) -> None:
        # Criterion 6: no policy plugins installed -> exactly (DEFAULT_POLICY,),
        # and it is a real ModelPolicy object, not a special-cased bypass.
        with mock.patch("muninn.policy.entry_points", return_value=[]):
            resolved = resolve()
            self.assertEqual(resolved, (DEFAULT_POLICY,))
            self.assertIsInstance(resolved[0], ModelPolicy)
            check("literally-anything", "literally-any-provider")  # does not raise


class RegexAnchoringTest(unittest.TestCase):
    def test_prefix_anchor_rejects_lookalike_with_leading_junk(self) -> None:
        # Criterion 7. Documented behaviour (see ModelPolicy docstring in
        # muninn/policy.py): allow patterns use re.search, so an UNANCHORED
        # pattern matches anywhere in the string, but a pattern the author
        # anchors with "^" is anchored to the start exactly as re.search
        # honors "^". "evil-us.anthropic.foo" does not start with
        # "us.anthropic." so "^us\\.anthropic\\." must not match it, even
        # though the literal substring "us.anthropic." appears inside it.
        policy = ModelPolicy(name="anthropic-only", allow=(r"^us\.anthropic\.",),
                              reason="only us.anthropic.* model ids")
        with _resolve_with(policy):
            check("us.anthropic.claude-sonnet-5", "anyprovider")  # permitted
            with self.assertRaises(PolicyRefused):
                check("evil-us.anthropic.foo", "anyprovider")  # must NOT match


class EffectiveProvidersTest(unittest.TestCase):
    def test_filters_candidates_to_the_intersection(self) -> None:
        a = ModelPolicy(name="a", allow=("^x$", "^y$"), reason="a")
        b = ModelPolicy(name="b", allow=("^y$", "^z$"), reason="b")
        with _resolve_with(a, b):
            candidates = [("x", "p"), ("y", "p"), ("z", "p")]
            self.assertEqual(effective_providers(candidates), (("y", "p"),))


class BrokenPolicyEntryPointTest(unittest.TestCase):
    """A policy entry point that fails to load must not simply vanish.

    Dropping a broken policy would silently widen the effective permission
    set — exactly what this module exists to prevent, since the broken
    policy might have been the one thing narrowing a restricted build. So a
    load failure becomes a synthetic policy that refuses everything, not an
    absent one. This pins that behaviour so a future refactor cannot
    "simplify" it away without a red test.
    """

    def test_load_failure_refuses_rather_than_vanishes(self) -> None:
        broken = mock.Mock()
        broken.name = "broken-policy"
        broken.load = mock.Mock(side_effect=RuntimeError("credentials missing"))
        with mock.patch("muninn.policy.entry_points", return_value=[broken]):
            resolved = resolve()
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].allow, ())  # refuses everything
            with self.assertRaises(PolicyRefused):
                check("anything", "anyprovider")


if __name__ == "__main__":
    unittest.main()
