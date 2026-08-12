# Spec 015 — Provider selection: a second CLI provider, and a declared default

**Status: implemented.** Depends on 005 (enrichment and the `TextProvider`
contract) and 008 (the plugin contract).

**Read first:**
[`model-policy-chokepoint`](../../.valholl/articles/model-policy-chokepoint.md).
This spec adds a provider and changes how one is *chosen*; it does not touch what
is *permitted*, and the difference is the whole reason the chokepoint exists.

## Why

Two gaps, found by an internal distribution trying to express something the
contract could not.

**1. `claude -p` was the only text path this repo shipped.** That is right as a
*default* — the archive is made of Claude Code transcripts, so the provider the
user already has is the one that costs a default install nothing. It is wrong as
the only option: a user whose approved path is a different vendor's CLI had to
write a plugin to say so, and the plugin then could not become the default (gap
2). `codex exec` is the other CLI in this position — `~/.codex/sessions` is
already a source Muninn ingests, so on any machine where Codex sessions exist,
the Codex CLI exists too.

**2. A plugin could not declare the default text provider.**
`providers.resolve_provider` honoured a contributed provider *only* when named
with `--provider`, and its docstring recorded why: silently preferring whatever
is installed would make "which model just read my transcripts" depend on the
install set. That reasoning is sound and is preserved — but the conclusion was
too strong, and the failure it produced was worse than the one it prevented.

A distribution whose approved text path is not the built-in one had two options,
both bad: tell every user to pass `--provider` on every command (the daemon
passes nothing, so background enrichment would silently use the wrong provider),
or ship a wrapper script that injects the flag. **A wrapper script is a
precedence rule with no docstring and no `doctor` line** — strictly worse for the
question the original rule was protecting.

## Scope

**In:** `CodexCLIProvider`; `PluginSpec.default_text_provider` with load-time
validation and `doctor` reporting; `resolve_provider` honouring it; `--provider`
accepting either built-in by name.

**Out:** any change to `muninn.policy` or to what models are permitted; a
non-CLI (SDK/HTTP) provider; making Codex the default here.

## Files

| File | Action |
|---|---|
| `muninn/providers.py` | `CodexCLIProvider`, `BUILTIN_PROVIDERS`, `resolve_provider` rewritten |
| `muninn/plugins.py` | `PluginSpec.default_text_provider`, `UnknownDefaultTextProvider`, validation |
| `muninn/cli.py` | `doctor` prints a declared default |
| `tests/test_enrich.py`, `tests/test_plugins.py` | new provider and seam tests |

## The Codex provider

`codex exec`, prompt on stdin, `shell=False`, `policy.check` before the
subprocess — identical discipline to `ClaudeCLIProvider`, for identical reasons.
Default model `gpt-5.6-luna` (Codex's cheap tier, its own replacement for GPT-5.4
Mini), by the same arithmetic that makes Haiku the `claude` default: one call per
substantive session, thousands of sessions.

Four flags are not optional, and one of them is the reason this provider needs a
spec rather than a patch:

| Flag | Prevents |
|---|---|
| `--ephemeral` | **A feedback loop.** Without it, every call writes a session rollout under `$CODEX_HOME/sessions` — a directory Muninn ingests. Enrichment would manufacture one new session to enrich per call, forever, each generation billable. |
| `--sandbox read-only` | Inheriting a user's `sandbox_mode = "danger-full-access"`, which is a reasonable interactive setting and an unreasonable one for an unattended summariser. |
| `--skip-git-repo-check` | The daemon's working directory is site-packages, not a checkout. |
| `--color never` | Escape codes in a file that gets parsed. |

Output is read from `--output-last-message`, not stdout: stdout carries the whole
event trace, and `enrich.parse_facets` is given a JSON object, not a transcript of
one being produced. The file is `mkstemp` 0600 and removed in a `finally`.

**It is not the default.** Adding a provider must not change what an existing
install enriches with; a test asserts that.

## The declared default

`PluginSpec.default_text_provider: str | None = None`. Three properties make it a
declaration rather than a silent preference, and all three are load-bearing —
remove any one and the original objection is correct again:

1. **Validated at load.** A value not matching one of that spec's own providers is
   `UnknownDefaultTextProvider`, collected like any other plugin load error. The
   failure this prevents: a typo falling through to the built-in provider at
   enrichment time, so a distribution enriches through the exact model it
   declared it did not want.
2. **Printed by `doctor`.** "Which model just read my transcripts" is answered by
   reading one line of output, not by knowing this function's precedence rules.
3. **Overridable per command.** `--provider` always wins.

Resolution order: `--provider` → a single declared default → the built-in Claude
CLI. **Two declared defaults raise** rather than resolving by discovery order —
picking one would be precisely the silent preference the original rule refused.

## Acceptance criteria

1. Prompt reaches Codex through stdin, never argv; `shell=False`.
2. `--ephemeral` is passed (the feedback-loop guard).
3. Sandbox is pinned `read-only`, not inherited.
4. Output comes from the last-message file, not stdout.
5. The temp file is removed even when the call fails.
6. A failure carries no provider output (stderr can echo transcript prose).
7. `policy.check` runs before the subprocess exists.
8. `available()` does no I/O.
9. Codex is **not** the default with no plugin installed.
10. A valid declaration loads and resolves.
11. A typo is `UnknownDefaultTextProvider` at load, not a fallthrough at use.
12. Declaring nothing leaves the built-in default unchanged.
13. `--provider` overrides a declared default, for both built-ins.
14. Two declared defaults raise, naming both plugins.

## Definition of done

- `uv run pytest` green with and without the `[semantic]` extra; `ruff` clean.
- `muninn doctor` shows a declared default when one is installed.
- Real check: enrichment through `--provider codex-cli` produces parseable facets
  on a real session, and `$CODEX_HOME/sessions` gains no new file.

## Guardrails

- **Do not** let this spec widen what models are permitted. Selection and
  permission are separate, and `policy.check` stays the only chokepoint.
- **Do not** infer a default from the install set. It is declared, validated,
  printed, and overridable — or it does not happen.
- **Do not** resolve two declared defaults by ordering.
- **Do not** drop `--ephemeral`, and do not "simplify" the last-message file to
  stdout capture.
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
