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
package's — this repository is public, and that boundary is one CLAUDE.md
records as having been crossed once already.

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

#: The Codex CLI's model, when that provider is selected. ``gpt-5.6-luna`` is
#: Codex's own replacement for GPT-5.4 Mini — its cheap tier — which is the
#: right default for the same arithmetic reason Haiku is the ``claude`` one:
#: enrichment is one call per substantive session, thousands of times.
DEFAULT_CODEX_MODEL = "gpt-5.6-luna"

#: The Codex provider's policy name. Distinct from ``PROVIDER_NAME`` because a
#: policy that permits one must be able to refuse the other: approval is per
#: vendor *and* per pathway in a way that does not reduce to "local CLIs are
#: fine", so the two shipped providers must be nameable apart.
CODEX_PROVIDER_NAME = "codex-cli"

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


@dataclass
class CodexCLIProvider:
    """Shells out to ``codex exec``. The second provider this package ships.

    Same discipline as :class:`ClaudeCLIProvider` — policy first, prompt through
    stdin, ``shell=False``, exit code only on failure — and the same reason for
    being a subprocess rather than an SDK. It is **not** the default: the
    built-in default stays Claude/Haiku, and this is selected with
    ``--provider codex-cli`` or by a distribution that declares it
    (:attr:`muninn.plugins.PluginSpec.default_text_provider`).

    ## Four flags that are not optional, and what each one prevents

    - ``--ephemeral`` — **the important one.** Without it, every enrichment call
      writes a Codex session rollout under ``$CODEX_HOME/sessions``, which is a
      directory Muninn *ingests*. Enriching sessions would manufacture new
      sessions to enrich: a feedback loop that grows the corpus by one session
      per call, forever, and each generation of it costs money.
    - ``-s read-only`` — enrichment wants one completion, not an agent. The
      sandbox mode is pinned rather than inherited because a user's
      ``config.toml`` can set ``sandbox_mode = "danger-full-access"`` globally
      (a reasonable choice for interactive work), and an unattended summariser
      must not inherit it.
    - ``--skip-git-repo-check`` — the daemon's working directory is
      site-packages, not a checkout.
    - ``--color never`` — the last-message file is parsed; escape codes are not
      wanted in it.

    ## Why the output goes through a file

    ``-o/--output-last-message`` writes only the agent's final message. Reading
    ``stdout`` instead would capture the whole event trace, and the parser
    downstream (``enrich.parse_facets``) is given a JSON object, not a
    transcript of one being produced. The file is created by ``mkstemp`` with
    0600 and removed in a ``finally``: it holds model output derived from
    transcript prose, which is the same material the redaction gate exists to
    keep bounded.
    """

    model: str = DEFAULT_CODEX_MODEL
    binary: str = "codex"
    name: str = CODEX_PROVIDER_NAME

    def available(self) -> str | None:
        import shutil

        if shutil.which(self.binary) is None:
            return (f"{self.binary!r} is not on PATH — install the Codex CLI, or "
                    f"select another provider with --provider")
        return None

    def generate(self, prompt: str, *, max_tokens: int = 2048,
                 timeout: float = DEFAULT_TIMEOUT_S) -> str:
        # Before the subprocess, before anything is spent — same rule and same
        # reason as ClaudeCLIProvider.generate.
        policy.check(self.model, self.name)

        import os
        import tempfile

        fd, out_path = tempfile.mkstemp(prefix=".muninn-codex-", suffix=".txt")
        os.close(fd)
        try:
            argv = [
                self.binary, "exec",
                "--model", self.model,
                "--ephemeral",              # see the class docstring — no session files
                "--skip-git-repo-check",
                "--sandbox", "read-only",
                "--color", "never",
                "--output-last-message", out_path,
                "-",                        # read the prompt from stdin
            ]
            try:
                proc = subprocess.run(
                    argv,
                    input=prompt,          # stdin, never argv
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    shell=False,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ProviderError(f"{self.binary!r} not found on PATH") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"{self.binary!r} timed out after {timeout:.0f}s") from exc

            if proc.returncode != 0:
                # Exit code only. Codex writes prompt-derived text to stderr.
                raise ProviderError(f"{self.binary!r} exited {proc.returncode}")

            try:
                out = open(out_path, encoding="utf-8").read().strip()
            except OSError as exc:
                raise ProviderError(
                    f"{self.binary!r} wrote no last-message file "
                    f"({type(exc).__name__})") from exc
            if not out:
                raise ProviderError(f"{self.binary!r} produced no output")
            return out
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass


#: Every provider this package ships, by policy name. A distribution's plugin
#: contributes its own; this is the built-in set ``--provider`` can name without
#: anything installed.
BUILTIN_PROVIDERS = {
    PROVIDER_NAME: ClaudeCLIProvider,
    CODEX_PROVIDER_NAME: CodexCLIProvider,
}


def default_provider(model: str | None = None) -> ClaudeCLIProvider:
    return ClaudeCLIProvider(model=model or DEFAULT_MODEL)


def resolve_provider(model: str | None = None,
                     provider_name: str | None = None) -> TextProvider:
    """The provider to enrich with: named, else a plugin's declared default,
    else the built-in Claude CLI.

    ## This function used to refuse to honour a plugin default. Why it now does

    The previous rule was "a plugin's provider is preferred only when explicitly
    asked for by name", on the reasoning that silently preferring a contributed
    provider would make *which model just read my transcripts* depend on what
    happens to be installed. That concern is real and is **not** abandoned here;
    what changed is the mechanism that answers it.

    A distribution now has to say so **out loud** — one named field,
    ``PluginSpec.default_text_provider``, whose value must match one of that
    spec's own providers or discovery rejects the plugin. It is not "whatever
    got installed wins": it is a declaration, checked at load time, reported by
    ``muninn doctor``, and overridable by ``--provider`` on any single command.
    So the question stays answerable — by reading `doctor` rather than by
    knowing this function's precedence rules.

    The case that forced it: an internal distribution whose approved text path is
    neither the built-in one nor reachable by asking users to remember a flag on
    every invocation — including the daemon's, which nobody types. A default
    nobody can express is a default that gets expressed as a wrapper script, and
    a wrapper script is a precedence rule with no docstring and no doctor line.

    Ambiguity is refused rather than resolved: two plugins both declaring a
    default raises, because picking one by discovery order would be exactly the
    silent-preference failure the original rule was written against.
    """
    from .plugins import discover_plugins

    if provider_name is not None:
        builtin = BUILTIN_PROVIDERS.get(provider_name)
        if builtin is not None:
            # A model override applies to whichever builtin was named; each
            # class carries its own default when none is given.
            return builtin(model=model) if model else builtin()
        for spec in discover_plugins().specs:
            for candidate in spec.text_providers:
                if getattr(candidate, "name", None) == provider_name:
                    return candidate
        raise ProviderError(f"no text provider named {provider_name!r} is installed")

    declared = [(spec, spec.default_text_provider)
                for spec in discover_plugins().specs
                if getattr(spec, "default_text_provider", None)]
    if len(declared) > 1:
        names = ", ".join(sorted(spec.name for spec, _ in declared))
        raise ProviderError(
            f"more than one installed plugin declares a default text provider "
            f"({names}); pass --provider to choose")
    if declared:
        spec, wanted = declared[0]
        for candidate in spec.text_providers:
            if getattr(candidate, "name", None) == wanted:
                return candidate
        # Unreachable via discovery, which validates the reference — kept
        # because a hand-built PluginSpec can reach here and a silent fallback
        # to the built-in provider would be the surprise this whole docstring
        # is about.
        raise ProviderError(
            f"plugin {spec.name!r} declares default text provider {wanted!r}, "
            f"which it does not contribute")
    return default_provider(model)
