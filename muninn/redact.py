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

**Patterns over-match on purpose, with one measured exception.** A false positive
costs a model a few characters of context in one session's summary. A false
negative posts someone's production key to an API. Those are not comparable, so
every rule here is written to catch the shape rather than to be precise about the
vendor.

The exception is the assignment rule's **whitespace-separated** form, and it was
found by running the gate over a real transcript rather than by reasoning about
it. On one 2,259-word session the rule fired 15 times and **every match was
prose** — `token storage`, `OAuth refresh`, `authoritative source`. That is not
the cheap kind of false positive: the summariser received `OAuth [REDACTED]
tokens`, so the sessions hollowed out worst were the ones *about* credential
handling, which are the sessions whose technical specificity matters most. Worse,
the count reported it as **one** redaction, so the over-matching was invisible
from the outside — see :func:`redact` for the recount bug that caused that. The
`=` and `:` forms still over-match freely; only the bare-space branch now asks
whether the value looks like a secret at all (:func:`_secret_shaped`).

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
    # The ``["']?`` before the separator is load-bearing and was missing: this
    # docstring has always claimed to cover ``"password": "x"``, and it did not.
    # A quoted JSON key puts a ``"`` between the key and the ``:``, so neither
    # separator branch could match and the most common secret shape in a config
    # dump — a JSON credentials blob — passed through untouched. Verified before
    # the fix: `"password": "correct-horse"` produced no redaction at all.
    ("assignment", re.compile(
        r"(?i)\b(?P<key>[A-Za-z0-9_.\-]*(?:passwd|password|secret|token|api[_\-]?key|"
        r"access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth)[A-Za-z0-9_.\-]*)"
        r"(?P<keyquote>[\"']?)(?P<sep>\s*[:=]\s*|\s+)"
        r"[\"']?(?P<value>[^\s\"',;)]{6,})[\"']?")),
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

#: An all-alphabetic value this long is treated as secret-shaped even with no
#: digit in it. English words reach 20 characters rarely; opaque tokens do so
#: constantly.
_LONG_ENOUGH = 20


def _secret_shaped(value: str) -> bool:
    """Whether a **whitespace-separated** value looks like a secret at all.

    Only consulted for the ``\\s+`` separator branch, and it exists because that
    branch cannot otherwise tell ``--token abc123`` from the English phrase
    ``token storage``. Measured on a real session: the rule fired 15 times on one
    2,259-word transcript and **every match was prose** — ``token storage``,
    ``OAuth refresh``, ``authoritative source``, ``1-token probe``. The
    summariser received ``OAuth [REDACTED] tokens``, which protected nothing and
    removed exactly the technical specificity enrichment is for. Sessions *about*
    credential handling are the ones most likely to discuss auth in prose, so
    this was worst precisely where it mattered most.

    The test is deliberately crude — a digit, a non-letter, or real length —
    because the alternative is an entropy heuristic that needs its own
    calibration. **The trade-off, stated plainly:** an all-lowercase-alphabetic
    secret under 20 characters passed as a bare flag value (``--token
    hunterhunter``) is now missed. The ``=`` and ``:`` forms are unchanged, and
    the narrow vendor patterns above (``openai-key``, ``anthropic-key``, ``jwt``,
    ``bearer-token``, ``credential-url``) never consult this at all — so what
    narrowed is one heuristic branch of the catch-all, not the gate.
    """
    return len(value) >= _LONG_ENOUGH or any(ch.isdigit() for ch in value)


def _assignment_replacer():
    """``(repl, counter)`` — a substitution function and its honest count.

    The count has to come from the replacer rather than from ``subn``, because
    this rule *declines* matches (placeholders, and now prose), and it cannot
    come from counting ``[REDACTED]`` in the output either — see
    :func:`redact`, where that is the bug this replaces.
    """
    replaced = [0]

    def repl(match: re.Match[str]) -> str:
        """Keep the key, replace the value — unless it is not a secret.

        Keeping the key is the point. ``[REDACTED]`` on its own tells a
        summariser nothing, whereas ``AWS_SECRET_ACCESS_KEY=[REDACTED]`` still
        says *this session was configuring AWS credentials*, which is exactly the
        kind of fact enrichment exists to capture. That argument holds for a real
        assignment and collapses when the "key" is the word ``token`` inside a
        sentence, which is why :func:`_secret_shaped` guards the whitespace form.
        """
        key = match.group("key")
        keyquote, sep, value = (match.group("keyquote"), match.group("sep"),
                                match.group("value"))
        if value.lower() in _ASSIGNMENT_ALLOW:
            return match.group(0)
        if not sep.strip() and not _secret_shaped(value):
            # Whitespace separator and a word-shaped value: prose.
            return match.group(0)
        replaced[0] += 1
        # ``keyquote`` is re-emitted so a JSON key survives intact as
        # ``"password": [REDACTED]`` rather than the malformed ``"password:``.
        return f"{key}{keyquote}{sep}{PLACEHOLDER}"

    return repl, replaced


def redact(text: str) -> tuple[str, dict[str, int]]:
    """Return ``(redacted_text, {rule_name: count})``.

    The counts exist so a caller can report *that* secrets were stripped, and of
    what kind, without ever holding one. Nothing in Muninn stores or prints the
    matched values — the same rule the ledger follows for exception messages,
    applied to the one text path that leaves the machine.
    """
    counts: dict[str, int] = {}
    for name, pattern in _RULES:
        if name == "assignment":
            # Counted by the replacer, which is the only thing that knows which
            # matches it declined.
            repl, replaced = _assignment_replacer()
            text, _n = pattern.subn(repl, text)
            if replaced[0]:
                counts[name] = counts.get(name, 0) + replaced[0]
            continue
        text, n = pattern.subn(PLACEHOLDER, text)
        if n:
            counts[name] = counts.get(name, 0) + n
    return text, counts


def contains_secret(text: str) -> bool:
    """Whether redaction would remove anything. For asserting the gate held.

    Defined in terms of :func:`redact` rather than by re-running the patterns,
    so the two cannot disagree. They *did* disagree the moment the assignment
    rule learned to decline a match: a pattern-only version answers "something
    matched", while the caller is always asking "would anything be stripped",
    and prose about tokens matches without being stripped.
    """
    return bool(redact(text)[1])
