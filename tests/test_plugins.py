"""The plugin contract: protocols, discovery, isolation.

See docs/specs/008-plugin-contract.md acceptance criteria 8-14 and
.valholl/articles/lessons-for-huginn.md #1 (compatibility must be a range,
checked loudly, never an exact match that silently disables every plugin).

Entry-point discovery is exercised by monkeypatching
``importlib.metadata.entry_points`` with fakes, never by installing a real
distribution or shelling out — this repo's Windows CI cannot host
subprocess/thread fan-out (see WINDOWS.md), and there is no need for either
here.
"""
from __future__ import annotations

import unittest
from unittest import mock

from muninn.plugins import (
    PluginLoadError,
    PluginSpec,
    SourceContext,
    discover_plugins,
)
from muninn.sources import ParsedSession


def _fake_entry_point(name: str, loader):
    """One fake entry point: ``.name`` plus a ``.load()`` returning/raising ``loader``."""
    ep = mock.Mock()
    ep.name = name
    if isinstance(loader, BaseException):
        ep.load = mock.Mock(side_effect=loader)
    else:
        ep.load = mock.Mock(return_value=loader)
    return ep


def _discover_with(*fake_eps):
    discover_plugins.cache_clear()
    patcher = mock.patch("muninn.plugins.entry_points", return_value=list(fake_eps))
    patcher.start()
    try:
        return discover_plugins()
    finally:
        patcher.stop()
        discover_plugins.cache_clear()


class WellFormedPluginTest(unittest.TestCase):
    def test_capabilities_appear_in_the_registry(self) -> None:
        # Criterion 8.
        class FakeEmbedder:
            name = "fake-embedder"
            model = "fake-model"
            dim = 8

            def available(self) -> str | None:
                return None

            def embed(self, texts):
                return [[0.0] * self.dim for _ in texts]

        spec = PluginSpec(name="wellformed", version="1.0.0", embedders=(FakeEmbedder(),))
        result = _discover_with(_fake_entry_point("wellformed", spec))
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.specs), 1)
        self.assertEqual(result.specs[0].name, "wellformed")
        self.assertEqual(len(result.specs[0].embedders), 1)
        self.assertEqual(result.specs[0].embedders[0].name, "fake-embedder")


class RangeCompatibilityTest(unittest.TestCase):
    def test_overlapping_range_loads_and_non_overlapping_fails_loudly(self) -> None:
        # Criterion 9. core API_VERSION == 1.
        compatible = PluginSpec(name="compatible", version="1.0.0", min_api=1, max_api=2)
        incompatible = PluginSpec(name="incompatible", version="1.0.0", min_api=2, max_api=5)

        result = _discover_with(
            _fake_entry_point("compatible", compatible),
            _fake_entry_point("incompatible", incompatible),
        )

        loaded_names = {s.name for s in result.specs}
        self.assertIn("compatible", loaded_names)
        self.assertNotIn("incompatible", loaded_names)

        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertIsInstance(err, PluginLoadError)
        self.assertEqual(err.entry_point, "incompatible")
        self.assertEqual(err.error_class, "IncompatibleApiVersion")


class FailureIsolationTest(unittest.TestCase):
    def test_one_broken_entry_point_does_not_block_a_healthy_sibling(self) -> None:
        # Criterion 10.
        healthy = PluginSpec(name="healthy", version="1.0.0")
        result = _discover_with(
            _fake_entry_point("broken", RuntimeError("boom, has a socket address 10.0.0.1 in it")),
            _fake_entry_point("healthy", healthy),
        )
        self.assertEqual([s.name for s in result.specs], ["healthy"])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].entry_point, "broken")
        self.assertEqual(result.errors[0].error_class, "RuntimeError")


class ErrorTextIsClassNameOnlyTest(unittest.TestCase):
    def test_surfaced_detail_has_no_spaces_and_matches_class_name_shape(self) -> None:
        # Criterion 11. The exception message ("credentials sk-abc123 leaked")
        # must never appear in what doctor would print — only the class name.
        import re

        class_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

        result = _discover_with(
            _fake_entry_point("leaky", ValueError("credentials sk-abc123 leaked")),
        )
        self.assertEqual(len(result.errors), 1)
        err = result.errors[0]
        self.assertNotIn(" ", err.error_class)
        self.assertRegex(err.error_class, class_name_re)
        self.assertEqual(err.error_class, "ValueError")
        # And the leaked credential must not appear anywhere doctor would
        # print from this object — i.e. not in error_class at all.
        self.assertNotIn("sk-abc123", err.error_class)


class NameValidationTest(unittest.TestCase):
    def test_invalid_duplicate_and_reserved_names_are_each_rejected(self) -> None:
        # Criterion 12.
        bad_shape = PluginSpec(name="Bad-Name!", version="1.0.0")
        reserved = PluginSpec(name="claude", version="1.0.0")
        dup_a = PluginSpec(name="dupplugin", version="1.0.0")
        dup_b = PluginSpec(name="dupplugin", version="2.0.0")

        result = _discover_with(
            _fake_entry_point("bad_shape_ep", bad_shape),
            _fake_entry_point("reserved_ep", reserved),
            _fake_entry_point("dup_a_ep", dup_a),
            _fake_entry_point("dup_b_ep", dup_b),
        )

        errors_by_ep = {e.entry_point: e for e in result.errors}
        self.assertEqual(errors_by_ep["bad_shape_ep"].error_class, "InvalidPluginName")
        self.assertEqual(errors_by_ep["reserved_ep"].error_class, "ReservedPluginName")
        # entry points are processed in sorted-name order (dup_a_ep, dup_b_ep),
        # so dup_a loads first and dup_b is the duplicate rejected.
        self.assertEqual(errors_by_ep["dup_b_ep"].error_class, "DuplicatePluginName")
        self.assertEqual([s.name for s in result.specs], ["dupplugin"])


class AvailableNotCalledDuringDiscoveryTest(unittest.TestCase):
    def test_discovery_never_invokes_available(self) -> None:
        # Criterion 13. A provider whose available() would do I/O (here:
        # raising, standing in for "opens a socket") must never have
        # available() invoked by discover_plugins() at all.
        class SocketOpeningEmbedder:
            name = "socket-embedder"
            model = "m"
            dim = 4

            def available(self) -> str | None:
                raise AssertionError("available() must not be called during discovery")

            def embed(self, texts):
                return []

        provider = SocketOpeningEmbedder()
        spec = PluginSpec(name="ioplugin", version="1.0.0", embedders=(provider,))
        result = _discover_with(_fake_entry_point("ioplugin", spec))
        self.assertEqual(result.errors, ())
        self.assertEqual(len(result.specs), 1)
        # If available() had been called, constructing the fake would have
        # already raised inside discover_plugins() and this assertion would
        # never be reached with a clean result.


class NamespacingTest(unittest.TestCase):
    def test_contributed_session_id_is_namespaced_and_cannot_collide(self) -> None:
        # Criterion 14.
        ctx = SourceContext(plugin="acme", source="tickets")
        namespaced = ctx.namespaced_id("12345")
        self.assertEqual(namespaced, "plugin:acme.tickets:12345")

        local_id = "12345"  # a local claude/codex session could plausibly be this
        self.assertNotEqual(namespaced, local_id)

        class FakeHistorySource:
            name = "tickets"

            def available(self) -> str | None:
                return None

            def fetch(self, context: SourceContext):
                yield ParsedSession(
                    session_id=context.namespaced_id("12345"), source="acme.tickets")

        source = FakeHistorySource()
        sessions = list(source.fetch(ctx))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].session_id, "plugin:acme.tickets:12345")
        self.assertTrue(sessions[0].session_id.startswith("plugin:"))


if __name__ == "__main__":
    unittest.main()
