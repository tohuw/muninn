"""The plugin contract: three capability protocols, discovery, isolation.

See docs/specs/008-plugin-contract.md and
.valholl/articles/lessons-for-huginn.md #1 and #6 for the two upstream
mistakes this module exists not to repeat:

1. **Compatibility is a declared range, checked loudly.** Huginn's
   ``api_version ==`` exact match silently disables every plugin on a routine
   version bump (tohuw/huginn#38). Here, a plugin declares ``[min_api,
   max_api]``; a mismatch becomes a ``PluginLoadError`` collected and surfaced
   in ``doctor``, never a silent skip.
2. **A plugin contributes capabilities; it cannot restrict what other code
   may call.** That veto power belongs to ``muninn.policy`` exclusively
   (tohuw/huginn#41) — this module has no opinion about which models are
   *allowed*, only about which providers *exist*.

This module ships **no concrete provider**. It is the socket, not the plug:
no boto3, no MLX, no HTTP client, nothing that ships a working
``EmbeddingProvider``/``TextProvider``/``HistorySource`` implementation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.metadata import entry_points
from typing import Any, Iterable, Protocol, Sequence

from .sources import ParsedSession

API_VERSION = 1
ENTRY_POINT_GROUP = "muninn.plugins"

# Names match this shape and are unique and non-reserved across all loaded
# plugins. Enforced once, here, rather than by convention per plugin author.
_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
RESERVED_NAMES = frozenset({"claude", "codex"})


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int

    def available(self) -> str | None:
        """``None`` if usable, else a human-readable reason. No I/O — see module docstring."""
        ...

    def embed(self, texts: Sequence[str]) -> Any:
        """Return an (n, dim) float32 array, L2-normalized. Shape, not dtype, is enforced by callers."""
        ...


class TextProvider(Protocol):
    """Text generation, for enrichment and briefs."""

    name: str
    model: str

    def available(self) -> str | None: ...

    def generate(self, prompt: str, *, max_tokens: int = 2048, timeout: float = 60.0) -> str: ...


class HistorySource(Protocol):
    """Contributes sessions Muninn cannot discover locally."""

    name: str

    def available(self) -> str | None: ...

    def fetch(self, context: "SourceContext") -> Iterable[ParsedSession]: ...


@dataclass(frozen=True)
class SourceContext:
    """Given to ``HistorySource.fetch()`` so contributed ids cannot collide with local ones.

    A locally discovered session id is a bare filename stem (see
    ``sources.parse_claude``/``parse_codex``). A plugin's ``external_id`` is
    whatever the remote system uses for its own primary key — no promise it
    is even in the same format. Namespacing every contributed id with the
    plugin and source name, rather than trusting the id space, is what makes
    "cannot collide" a structural guarantee instead of a hope: see acceptance
    criterion 14 in docs/specs/008-plugin-contract.md.
    """

    plugin: str
    source: str

    def namespaced_id(self, external_id: str) -> str:
        return f"plugin:{self.plugin}.{self.source}:{external_id}"


@dataclass(frozen=True)
class PluginSpec:
    name: str
    version: str
    api_version: int = API_VERSION
    min_api: int = API_VERSION
    max_api: int = API_VERSION
    embedders: tuple[EmbeddingProvider, ...] = ()
    text_providers: tuple[TextProvider, ...] = ()
    history_sources: tuple[HistorySource, ...] = ()


@dataclass(frozen=True)
class PluginLoadError:
    """One plugin's failure to load, isolated so it cannot block a healthy sibling.

    ``detail`` is for local diagnostics only and must never be rendered by
    anything Muninn surfaces (CLI, ``doctor``, ``--json``) — only
    ``error_class`` may appear there. An entry point can point at arbitrary
    third-party code; its exception's message can embed transcript text or
    credentials, per the same rule .valholl/articles/deterministic-imports.md
    applies to import errors. Even for the validation failures raised in this
    module (bad name, duplicate, reserved, version range — where we control
    every character of ``detail``) doctor still only prints ``error_class``,
    so there is exactly one code path for "what gets shown," not one safe
    path and one that has to remember to stay safe.
    """

    entry_point: str
    error_class: str
    detail: str = ""


@dataclass(frozen=True)
class DiscoveryResult:
    specs: tuple[PluginSpec, ...] = ()
    errors: tuple[PluginLoadError, ...] = ()


class InvalidPluginName(ValueError):
    """Plugin name does not match ``^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$``."""


class DuplicatePluginName(ValueError):
    """Plugin name collides with one already loaded from another entry point."""


class ReservedPluginName(ValueError):
    """``claude``/``codex`` are reserved for the built-in local sources."""


class IncompatibleApiVersion(ValueError):
    """``[min_api, max_api]`` does not overlap core's ``API_VERSION``."""


class PluginSpecNotInstance(TypeError):
    """The entry point resolved to something other than a ``PluginSpec`` instance.

    The one shape discovery accepts is a module-level ``PluginSpec`` instance
    (the same convention ``muninn.policy`` uses for its entry-point group) —
    never a zero-arg factory function. Huginn accepts either a spec or a
    factory, and that ambiguity buys nothing: a single accepted shape means a
    misconfigured entry point fails loudly with a name that says exactly what
    went wrong, rather than surfacing as a bare ``TypeError`` a plugin author
    has to go read our source to decode. The most likely mistake this catches
    is pointing the entry point at a *function* that builds a ``PluginSpec``
    instead of at the built instance itself.
    """


def _validate(spec: PluginSpec, seen_names: set[str]) -> None:
    """Raise one of the four validation errors above, or return.

    Order is deliberate: a malformed name is reported as malformed even if it
    also happens to be reserved or a duplicate, so the error a plugin author
    sees names the *first* thing wrong rather than picking one that happens to
    win a set-membership race.
    """
    if not _NAME_RE.match(spec.name):
        raise InvalidPluginName(f"{spec.name!r} does not match {_NAME_RE.pattern}")
    if spec.name in RESERVED_NAMES:
        raise ReservedPluginName(f"{spec.name!r} is reserved for a built-in source")
    if spec.name in seen_names:
        raise DuplicatePluginName(f"{spec.name!r} is already used by another loaded plugin")
    if not (spec.min_api <= API_VERSION <= spec.max_api):
        raise IncompatibleApiVersion(
            f"{spec.name!r} declares [{spec.min_api}, {spec.max_api}], "
            f"which does not cover core API_VERSION={API_VERSION}"
        )


@lru_cache(maxsize=1)
def discover_plugins() -> DiscoveryResult:
    """Load every ``muninn.plugins`` entry point once per process.

    Cached deliberately: a plugin registry that could change mid-process
    would mean two calls to "what providers exist" could disagree within a
    single run. A change to installed plugins requires a restart — this is
    surfaced in ``doctor`` output so the behaviour is not a surprise.

    Each entry point is expected to resolve to a ``PluginSpec`` instance
    (constructed at import time by the plugin's own module, the same
    convention ``muninn.policy`` uses for ``muninn.policy`` entry points) —
    not a factory function. Discovery only constructs/validates the spec: it
    never calls ``available()`` on any contributed provider (acceptance
    criterion 13), because ``available()`` may probe credentials or sockets
    and a slow probe during discovery is a hang, not a diagnostic.
    """
    specs: list[PluginSpec] = []
    errors: list[PluginLoadError] = []
    seen_names: set[str] = set()

    eps = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda ep: ep.name)
    for ep in eps:
        try:
            candidate = ep.load()
        except Exception as exc:  # noqa: BLE001 - isolate one plugin's failure from the rest
            errors.append(PluginLoadError(entry_point=ep.name, error_class=type(exc).__name__))
            continue

        if not isinstance(candidate, PluginSpec):
            hint = " (looks like a factory function, not an instance)" if callable(candidate) else ""
            exc = PluginSpecNotInstance(
                f"entry point did not resolve to a PluginSpec instance: "
                f"got {type(candidate).__name__}{hint}"
            )
            errors.append(PluginLoadError(
                entry_point=ep.name, error_class=type(exc).__name__, detail=str(exc)))
            continue

        try:
            _validate(candidate, seen_names)
        except (InvalidPluginName, DuplicatePluginName, ReservedPluginName, IncompatibleApiVersion) as exc:
            errors.append(PluginLoadError(
                entry_point=ep.name, error_class=type(exc).__name__, detail=str(exc)))
            continue

        seen_names.add(candidate.name)
        specs.append(candidate)

    return DiscoveryResult(specs=tuple(specs), errors=tuple(errors))
