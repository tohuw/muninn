"""Strip secrets before any transcript text reaches a model provider.

This is a **hard gate, not best-effort**. Muninn's archive is full of terminal
output, config files and pasted credentials — it holds whatever the user and
their agents typed, including the things they immediately regretted typing. The
one operation that sends that text somewhere else is enrichment, so this module
is the boundary it crosses.

Two design choices worth stating, because both look like over-engineering until
they aren't:

**Redaction happens on the way *out*, never on the way in.** The archive keeps
the raw prose, secrets and all. That is deliberate: the archive is a system of
record for data that exists nowhere else (.valholl/articles/archive-of-record.md),
and a redacting *ingest* would destroy the only copy of a transcript to protect
a credential that was already leaked to disk by the tool that wrote it. Redacting
at the provider boundary protects the thing that is actually new — the network
call — without touching the thing that is irreplaceable.

**Patterns over-match on purpose.** A false positive costs a model a few
characters of context in one session's summary. A false negative posts someone's
production key to an API. Those are not comparable, so every rule here is written
to catch the shape rather than to be precise about the vendor, and the assignment
rule fires on any `password`/`secret`/`token`/`api_key`-ish name regardless of
what follows it.

The pattern set is ported from Huginn's ``llm/context.py:redact_secrets()`` so
both ravens redact the same things. It is *not* imported from ``corvidae``: that
package is stdlib-only and shared, and a redaction list is the one thing where
two independent copies drifting apart is safer than one copy that a dependency
bump could weaken silently. If that judgement is wrong, the fix is to move it
upstream deliberately — not to let this file rot.
"""
from __future__ import annotations

import re

#: What replaces a match. Fixed-width and obvious, so a reader of a summary can
#: tell "this was redacted" from "this was never there" — and so a model reading
#: the transcript is not tempted to treat the placeholder as a value.
PLACEHOLDER = "[REDACTED]"

# Each entry is (name, compiled pattern). The name is reported in counts so a
# caller can say *what kind* of secret was stripped without ever holding the
# secret. Order matters only in that more specific vendor rules run before the
# generic assignment rule, so a match is attributed to the narrower name.
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # -- private keys: whole block, not just the header ---------------------
    ("private-key", re.compile(
        r"-----BEGIN[ A-Z]*PRIVATE KEY-----.*?-----END[ A-Z]*PRIVATE KEY-----",
        re.DOTALL)),

    # -- vendor-shaped API keys --------------------------------------------
    ("anthropic-key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}")),
    ("openai-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}")),
    ("xai-key", re.compile(r"xai-[A-Za-z0-9_\-]{16,}")),
    ("google-key", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # Classic AKIA/ASIA plus the newer prefixes; 16-char tail is the AWS shape.
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b")),
    ("github-fine-grained", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{8,}\b")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),

    # -- structured credentials --------------------------------------------
    # A JWT is three base64url segments; the leading `eyJ` is the encoded `{"`.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b")),
    ("bearer-token", re.compile(r"(?i)\b(?:bearer|authorization:\s*bearer)\s+[A-Za-z0-9._\-+/=]{12,}")),
    # user:password@host in any URL.
    ("credential-url", re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@")),

    # -- the catch-all: a secret-ish name being assigned a value ------------
    # Deliberately last and deliberately broad. Covers `API_KEY=x`,
    # `"password": "x"`, `--token x`, `secret: x`. The value class excludes
    # whitespace and quotes so the match stops at the end of the value.
    ("assignment", re.compile(
        r"(?i)\b([A-Za-z0-9_.\-]*(?:passwd|password|secret|token|api[_\-]?key|"
        r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth)[A-Za-z0-9_.\-]*)"
        r"(\s*[:=]\s*|\s+)"
        r"[\"']?([^\s\"',;)]{6,})[\"']?")),
)

#: Values the assignment rule must not redact: they are placeholders, not
#: secrets, and blanking them makes a transcript *harder* to summarise while
#: protecting nothing. ``null``/``none``/``true`` show up constantly in config
#: dumps, and the redaction marker itself appears when text is redacted twice.
_ASSIGNMENT_ALLOW = frozenset({
    "null", "none", "true", "false", "nil", "undefined", "empty", "unset",
    "changeme", "password", "secret", "token", "redacted", PLACEHOLDER.lower(),
    "your_api_key_here", "xxxxxx", "<value>", "...",
})


def _redact_assignment(match: re.Match[str]) -> str:
    """Keep the key, replace the value — unless the value is a known placeholder.

    Keeping the key is the point. ``[REDACTED]`` on its own tells a summariser
    nothing, whereas ``AWS_SECRET_ACCESS_KEY=[REDACTED]`` still says *this
    session was configuring AWS credentials*, which is exactly the kind of fact
    enrichment exists to capture.
    """
    key, sep, value = match.group(1), match.group(2), match.group(3)
    if value.lower() in _ASSIGNMENT_ALLOW:
        return match.group(0)
    return f"{key}{sep}{PLACEHOLDER}"


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return ``(redacted_text, {rule_name: count})``.

    The counts exist so a caller can report *that* secrets were stripped, and of
    what kind, without ever holding one. Nothing in Muninn stores or prints the
    matched values — the same rule the ledger follows for exception messages,
    applied to the one text path that leaves the machine.
    """
    counts: dict[str, int] = {}
    for name, pattern in _RULES:
        repl = _redact_assignment if name == "assignment" else PLACEHOLDER
        text, n = pattern.subn(repl, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    # The assignment rule's allow-list means subn's count can exceed the number
    # of actual replacements; recount honestly rather than over-report.
    if "assignment" in counts:
        actual = text.count(f"={PLACEHOLDER}") + text.count(f": {PLACEHOLDER}")
        counts["assignment"] = min(counts["assignment"], max(actual, 1))
    return text, counts


def contains_secret(text: str) -> bool:
    """Whether any rule matches. For tests and for asserting the gate held."""
    return any(pattern.search(text) for _, pattern in _RULES)
