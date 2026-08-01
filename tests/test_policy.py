"""The model policy chokepoint contract.

Every LLM/embedding call is supposed to route through ``check()``. These
tests encode the invariants from
.valholl/articles/model-policy-chokepoint.md — see docs/specs/008-plugin-contract.md
acceptance criteria 1-7. Policies intersect; they never union. No policy
matching a (model, provider) pair means refuse, never fall back.

**Read ``ShadowedDistributionTest`` before adding a test here.** Every test in
the original version of this file monkeypatched
``importlib.metadata.entry_points``, and that is exactly why a fail-open in
``resolve()`` sat behind a green suite: the bug was in *how policies are
discovered*, and patching the discovery call replaced the buggy code with a
mock. A mock-only suite can only ever test what happens after discovery
succeeds. So the shadowing test builds real ``.dist-info`` directories on disk
and runs a subprocess, and it is the one test here that could have caught it.
"""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from muninn.policy import (
    DEFAULT_POLICY,
    ModelPolicy,
    PolicyRefused,
    check,
    effective_providers,
    resolve,
    shadowed_distribution_names,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

requires_subprocess = unittest.skipIf(
    sys.platform == "win32",
    "subprocess/thread fan-out wedges the Windows CI runner; the same "
    "properties are covered in-process on all platforms",
)


def _fake_entry_points(*policies: ModelPolicy):
    """Build a fake entry-point list carrying ``policies``.

    Mirrors the shape ``importlib.metadata`` entry points have: objects with
    ``.name`` and ``.load()``. These unit tests patch
    ``muninn.policy._policy_entry_points`` — the seam *below* discovery —
    rather than ``entry_points``, which ``resolve()`` deliberately no longer
    calls. See the module docstring for why that distinction is load-bearing.
    """
    eps = []
    for i, policy in enumerate(policies):
        ep = mock.Mock()
        ep.name = policy.name or f"policy{i}"
        ep.load = mock.Mock(return_value=policy)
        eps.append(ep)
    return eps


def _resolve_with(*policies: ModelPolicy):
    return mock.patch("muninn.policy._policy_entry_points",
                      return_value=(_fake_entry_points(*policies), ()))


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
        with mock.patch("muninn.policy._policy_entry_points", return_value=([], ())):
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
        with mock.patch("muninn.policy._policy_entry_points", return_value=([broken], ())):
            resolved = resolve()
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].allow, ())  # refuses everything
            with self.assertRaises(PolicyRefused):
                check("anything", "anyprovider")


def _write_policy_distribution(root: Path, *, version: str, with_entry_points: bool) -> Path:
    """Write a real ``.dist-info`` for distribution ``muninn-testpolicy`` under ``root``.

    With ``with_entry_points=False`` this is the shadow: same distribution
    name, ``METADATA`` only, no ``entry_points.txt`` — two files that are
    enough to mask the real distribution's entry points from
    ``entry_points(group=...)``.
    """
    root.mkdir(parents=True, exist_ok=True)
    dist_info = root / f"muninn_testpolicy-{version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: muninn-testpolicy\nVersion: {version}\n", encoding="utf-8")
    if with_entry_points:
        (dist_info / "entry_points.txt").write_text(
            "[muninn.policy]\nno-local-model = muninn_testpolicy:POLICY\n", encoding="utf-8")
        (root / "muninn_testpolicy.py").write_text(textwrap.dedent(r'''
            from muninn.policy import ModelPolicy

            # The owner's real use case: not "bedrock only" but "do not use the
            # local model this package offers" — a strict exclusion, expressed
            # as an allowlist of the one vendor prefix that is approved.
            POLICY = ModelPolicy(
                name="no-local-model",
                allow=(r"^us\.anthropic\.",),
                reason="the local model package is excluded",
            )
        ''').lstrip(), encoding="utf-8")
    return root


class ShadowedDistributionTest(unittest.TestCase):
    r"""An installed exclusion policy must survive a distribution that masks its name.

    This is the one test in this file that exercises real
    ``importlib.metadata`` discovery, and the only reason it exists is that the
    mock-based tests above structurally cannot catch what it catches.

    ``entry_points()`` deduplicates by *normalised distribution name, first on
    ``sys.path`` wins*, and applies that rule before looking at which groups a
    distribution contributes. So a directory holding only
    ``muninn_testpolicy-9.9.dist-info/METADATA`` — no ``entry_points.txt`` —
    earlier on ``sys.path`` made ``entry_points(group="muninn.policy")`` return
    ``[]``. ``resolve()`` could not distinguish that from "no policy
    installed" and returned the permissive ``DEFAULT_POLICY``. Measured against
    this module before the fix, with the exclusion policy below installed::

        real policy installed:  resolved ['no-local-model']  local-llama-7b REFUSED
        shadow prepended:       resolved ['default']         local-llama-7b ALLOWED

    Run in a subprocess with ``PYTHONPATH`` set rather than by mutating
    ``sys.path`` in-process, because ``importlib.metadata`` caches its
    ``sys.path`` scan per path entry: the ordering this test depends on is not
    reliably reproducible once the parent interpreter has already scanned. A
    fresh interpreter is the honest way to ask the question.
    """

    def _probe(self, *path_entries: Path) -> dict:
        """Resolve policy in a fresh interpreter with ``path_entries`` prepended."""
        program = textwrap.dedent('''
            import json, sys
            from muninn.policy import PolicyRefused, check, resolve, shadowed_distribution_names
            try:
                check("local-llama-7b", "local")
                refused = False
            except PolicyRefused:
                refused = True
            print(json.dumps({
                "resolved": [p.name for p in resolve()],
                "local_model_refused": refused,
                "shadowed": list(shadowed_distribution_names()),
            }))
        ''')
        env_path = [str(p) for p in path_entries] + [str(REPO_ROOT)]
        proc = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True, text=True, timeout=90,
            env={"PYTHONPATH": ":".join(env_path), "PATH": "/usr/bin:/bin",
                 "SYSTEMROOT": "", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        self.assertEqual(proc.returncode, 0, f"probe failed:\n{proc.stderr}")
        return json.loads(proc.stdout.strip().splitlines()[-1])

    @requires_subprocess
    def test_metadata_only_shadow_cannot_disable_the_installed_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            real = _write_policy_distribution(tmpdir / "site-real", version="1.0", with_entry_points=True)
            shadow = _write_policy_distribution(tmpdir / "site-shadow", version="9.9", with_entry_points=False)

            # Baseline: the exclusion policy binds when nothing shadows it.
            alone = self._probe(real)
            self.assertEqual(alone["resolved"], ["no-local-model"])
            self.assertTrue(alone["local_model_refused"])
            self.assertEqual(alone["shadowed"], [])

            # The bug: shadow first on sys.path. The policy must STILL bind.
            shadowed = self._probe(shadow, real)
            self.assertEqual(shadowed["resolved"], ["no-local-model"],
                             "a metadata-only distribution masked the real one and the "
                             "excluded local model became usable again")
            self.assertTrue(shadowed["local_model_refused"])

    @requires_subprocess
    def test_two_distributions_sharing_a_name_are_reported(self) -> None:
        """The duplication is the shadowing signal, so ``doctor`` must be able to say so."""
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            a = _write_policy_distribution(tmpdir / "site-a", version="1.0", with_entry_points=True)
            b = _write_policy_distribution(tmpdir / "site-b", version="2.0", with_entry_points=True)
            both = self._probe(a, b)
            self.assertEqual(both["shadowed"], ["muninn-testpolicy"])
            self.assertTrue(both["local_model_refused"])

    @requires_subprocess
    def test_no_policy_installed_still_allows_everything(self) -> None:
        """The permissive default is unchanged: absence of a policy is not a restriction."""
        with tempfile.TemporaryDirectory() as tmp:
            nothing = Path(tmp) / "site-empty"
            nothing.mkdir()
            result = self._probe(nothing)
            self.assertEqual(result["resolved"], [DEFAULT_POLICY.name])
            self.assertFalse(result["local_model_refused"])
            self.assertEqual(result["shadowed"], [])


class ShadowedDistributionInProcessTest(unittest.TestCase):
    """The in-process twin of ``ShadowedDistributionTest``, so Windows is covered too.

    Per WINDOWS.md, subprocess fan-out wedges the Windows CI runner, and the
    convention here is that every subprocess-skipped property has an in-process
    equivalent running on all three platforms. This is that equivalent, and it
    also fails against the pre-fix ``resolve()``.

    ``importlib.invalidate_caches()`` after each ``sys.path`` mutation is what
    makes this reliable — ``importlib.metadata`` caches its scan per path entry,
    so without it a freshly added directory may not be seen. The subprocess
    version is kept anyway: a fresh interpreter proves the property with no
    cache-invalidation caveat at all, which is worth having for a security
    control even though it costs a Windows skip.
    """

    def setUp(self) -> None:
        self._saved_path = list(sys.path)
        self._saved_modules = set(sys.modules)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        sys.path[:] = self._saved_path
        for name in set(sys.modules) - self._saved_modules:
            del sys.modules[name]          # drop the fixture policy module
        importlib.invalidate_caches()
        self._tmp.cleanup()

    def _prepend(self, path: Path) -> None:
        sys.path.insert(0, str(path))
        importlib.invalidate_caches()

    def test_metadata_only_shadow_cannot_disable_the_installed_policy(self) -> None:
        tmpdir = Path(self._tmp.name)
        real = _write_policy_distribution(tmpdir / "site-real", version="1.0", with_entry_points=True)
        shadow = _write_policy_distribution(tmpdir / "site-shadow", version="9.9", with_entry_points=False)

        self._prepend(real)
        self.assertEqual([p.name for p in resolve()], ["no-local-model"])
        with self.assertRaises(PolicyRefused):
            check("local-llama-7b", "local")

        self._prepend(shadow)   # masks `real` from entry_points(group=...) entirely
        self.assertEqual([p.name for p in resolve()], ["no-local-model"],
                         "a metadata-only distribution masked the real one and the "
                         "excluded local model became usable again")
        with self.assertRaises(PolicyRefused):
            check("local-llama-7b", "local")

    def test_two_distributions_sharing_a_name_are_reported(self) -> None:
        tmpdir = Path(self._tmp.name)
        self._prepend(_write_policy_distribution(tmpdir / "site-a", version="1.0", with_entry_points=True))
        self._prepend(_write_policy_distribution(tmpdir / "site-b", version="2.0", with_entry_points=True))
        self.assertEqual(shadowed_distribution_names(), ("muninn-testpolicy",))
        with self.assertRaises(PolicyRefused):
            check("local-llama-7b", "local")

    def test_a_deleted_entry_points_file_does_not_widen_the_permitted_set(self) -> None:
        """No adversary needed: the same class of failure if metadata is just lost.

        A distribution whose ``entry_points.txt`` was removed contributes
        nothing — that much is unavoidable, since a missing file cannot say what
        it would have said. What must hold is that the *other* installed
        policy's restriction is unaffected, rather than the whole resolution
        collapsing to the permissive default.
        """
        tmpdir = Path(self._tmp.name)
        intact = _write_policy_distribution(tmpdir / "site-intact", version="1.0", with_entry_points=True)
        broken = _write_policy_distribution(tmpdir / "site-broken", version="2.0", with_entry_points=True)
        (broken / "muninn_testpolicy-2.0.dist-info" / "entry_points.txt").unlink()
        self._prepend(intact)
        self._prepend(broken)
        self.assertEqual([p.name for p in resolve()], ["no-local-model"])
        with self.assertRaises(PolicyRefused):
            check("local-llama-7b", "local")


class ShadowedNamesHelperTest(unittest.TestCase):
    def test_real_environment_reports_no_duplicate_policy_distributions(self) -> None:
        """A sanity check on the live interpreter: the helper runs and finds nothing.

        Cheap, but it is the only test that exercises the real
        ``distributions()`` walk in-process, so a crash in metadata handling
        (a malformed METADATA in some installed dependency, say) shows up here
        rather than only in ``doctor``.
        """
        self.assertEqual(shadowed_distribution_names(), ())


if __name__ == "__main__":
    unittest.main()
