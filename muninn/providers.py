"""Text generation providers, and the one that ships: the `claude` CLI.

Spec 005 names a subprocess rather than an SDK, and the reason is a dependency
rule rather than a preference: Muninn's runtime dependencies are `corvidae` and
`watchfiles`, and adding an HTTP client plus a vendor SDK to summarise
transcripts would put a network stack in a tool whose entire job is reading local
files. `claude -p` is also already installed for anyone Muninn is useful to —
the archive is *made of* Claude Code transcripts — so the provider that ships is
the one the user already has, authenticated the way they already authenticate.

An SDK-backed or Bedrock-backed provider is a plugin's job
(``muninn.plugins.TextProvider``) or the internal distribution's, not this
package's. See CLAUDE.md, "Don't put Cisco-specific anything in this repo."

## Every call routes through the policy chokepoint

:func:`policy.check` is called *before* the subprocess starts, not after and not
around it. A provider that reaches the network without passing through it is a
defect — `.valholl/articles/model-policy-chokepoint.md` exists because a
plugin-only design cannot express "only these models may be used", and a
chokepoint with one bypass is not a chokepoint.

## No shell

``subprocess.run`` with an argv list and ``shell=False`` (the default, stated
here because the mistake is invisible). The prompt contains transcript text —
attacker-controlled by construction, since transcripts hold web content and other
agents' output — and it reaches the process through **stdin**, never as an argv
element. Even without a shell, a megabyte of prose in argv would hit
``E2BIG``; through stdin it is just a pipe.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Protocol

from . import policy

#: The model enrichment uses unless told otherwise. Haiku by spec-005 decree,
#: and the reason is arithmetic rather than taste: enrichment is one call per
#: substantive session and the development corpus has thousands, so this is the
#: single most cost-sensitive call site in the tool. Override with `--model`.
DEFAULT_MODEL = "claude-haiku-4-5"

#: The provider name policies match against. A distribution restricting Muninn
#: to an approved endpoint writes ``require_provider="claude-cli"``.
PROVIDER_NAME = "claude-cli"

DEFAULT_TIMEOUT_S = 120.0


class ProviderError(RuntimeError):
    """A provider could not produce text. Carries a class name, never output.

    The message deliberately names only what went wrong structurally — a timeout,
    a non-zero exit, a missing binary. Provider stderr can echo the prompt, and
    the prompt is transcript text, so putting it in an exception would route
    conversation prose into logs and receipts by a side door. Same rule the
    ledger follows.
    """


class TextProvider(Protocol):
    """Structurally identical to ``plugins.TextProvider``, and that is the point.

    Enrichment accepts either: the built-in :class:`ClaudeCLIProvider` or one
    contributed by a plugin. Both are duck-typed against this shape, so core
    never branches on which kind it has.
    """

    name: str
    model: str

    def available(self) -> str | None:
        """``None`` if usable, else a human-readable reason. No I/O, no network."""
        ...

    def generate(self, prompt: str, *, max_tokens: int = 2048,
                 timeout: float = DEFAULT_TIMEOUT_S) -> str: ...


@dataclass
class ClaudeCLIProvider:
    """Shells out to ``claude -p``. The only provider this package ships.

    ``available()`` does no I/O — not even a ``--version`` probe — because
    ``plugins`` calls the equivalent method during discovery and a slow probe
    there is a hang rather than a diagnostic (spec 008, criterion 13). So it
    reports only what can be known for free: whether the binary is on ``PATH``.
    Whether it is *authenticated* is discovered by the first real call, which is
    the honest place for it.
    """

    model: str = DEFAULT_MODEL
    binary: str = "claude"
    name: str = PROVIDER_NAME

    def available(self) -> str | None:
        import shutil

        if shutil.which(self.binary) is None:
            return (f"{self.binary!r} is not on PATH — install Claude Code, or "
                    f"point --model at a provider contributed by a plugin")
        return None

    def generate(self, prompt: str, *, max_tokens: int = 2048,
                 timeout: float = DEFAULT_TIMEOUT_S) -> str:
        # Before the subprocess, before anything is spent. A refusal here raises
        # PolicyRefused, which enrichment lets propagate rather than counting as
        # a per-session failure: a refused model is a configuration answer for
        # the whole run, not a bad session.
        policy.check(self.model, self.name)

        argv = [self.binary, "-p", "--model", self.model]
        try:
            proc = subprocess.run(
                argv,
                input=prompt,          # stdin, never argv — see the module docstring
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,           # the default; stated because the mistake is invisible
                check=False,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"{self.binary!r} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"{self.binary!r} timed out after {timeout:.0f}s") from exc

        if proc.returncode != 0:
            # Exit code only. stderr can echo the prompt, and the prompt is
            # transcript text.
            raise ProviderError(f"{self.binary!r} exited {proc.returncode}")
        out = (proc.stdout or "").strip()
        if not out:
            raise ProviderError(f"{self.binary!r} produced no output")
        return out


def default_provider(model: str | None = None) -> ClaudeCLIProvider:
    return ClaudeCLIProvider(model=model or DEFAULT_MODEL)


def resolve_provider(model: str | None = None,
                     provider_name: str | None = None) -> TextProvider:
    """The provider to enrich with: a plugin's if named, else the built-in one.

    A plugin's provider is preferred only when explicitly asked for by name.
    Silently preferring a contributed provider over the built-in would make
    "which model just read my transcripts" depend on what happens to be
    installed — and that is the question the policy chokepoint exists to keep
    answerable.
    """
    if provider_name is None:
        return default_provider(model)

    from .plugins import discover_plugins

    for spec in discover_plugins().specs:
        for candidate in spec.text_providers:
            if getattr(candidate, "name", None) == provider_name:
                return candidate
    raise ProviderError(f"no text provider named {provider_name!r} is installed")
