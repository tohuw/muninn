"""Structured import results: receipts, not counters.

[[deterministic-imports]] and [[import-ledger-schema]] explain *why* an import
must hand back a self-evident claim instead of a store-relative counter an
agent has to interpret: "0 written, 61 cached" and "this exact export was
already imported at 21:15 by actor A" are not the same fact, and conflating
them is precisely how the claudex incident happened. This module defines the
shapes that make the conflation structurally impossible.

Two rules enforced here, by the type system rather than by convention:

- ``SourceFacts`` (what the source contains) and ``Delta`` (what this run
  changed) are separate dataclasses. A reporting layer that merges them into
  one blended object is a defect (invariant 4) — and it cannot express that
  defect by construction, because there is no single type to put both sets of
  fields on.
- ``Outcome`` and ``SkipReason`` are closed enums. An agent branches on
  ``Outcome`` and never interprets prose; an unrecognized skip reason is a bug
  to fix, not a free-text field to widen.
"""
from __future__ import annotations

import enum
import json
from dataclasses import dataclass, field


class Outcome(str, enum.Enum):
    """Exactly four values, so an agent can branch without reading prose.

    ``DUPLICATE`` is the value that would have prevented the claudex incident:
    a second import of an unchanged source can never be reported as
    ``IMPORTED`` with zero counts, because zero counts is indistinguishable
    from "genuinely nothing new" without this enum to disambiguate it.
    """

    IMPORTED = "imported"      # added + updated > 0
    DUPLICATE = "duplicate"    # source_digest already fully imported; see duplicate_of
    PARTIAL = "partial"        # skipped > 0 and the archive still changed
    REJECTED = "rejected"      # nothing imported: bad source, lock contention, crash


class SkipReason(str, enum.Enum):
    """Closed vocabulary. An unrecognized reason is a bug, not a free-text field.

    ``UNSUPPORTED_SENDER_ROLE`` and ``UNSUPPORTED_CONTENT_TYPE`` exist because
    the predecessors dropped voice-only / image-only conversations silently and
    reported them as "empty" — indistinguishable from genuinely empty content.
    ``SUPERSEDED_BY_RICHER_ORIGIN`` exists so a prose-index backfill declining
    to overwrite a richer raw-derived session is a recorded decision, not a
    silent no-op (see [[archive-of-record]]).
    """

    NO_CONTENT = "no-content"
    UNPARSEABLE_JSON = "unparseable-json"
    UNKNOWN_SCHEMA = "unknown-schema"
    MISSING_ITEM_ID = "missing-item-id"
    MISSING_TIMESTAMP = "missing-timestamp"
    DUPLICATE_ITEM_IN_SOURCE = "duplicate-item-in-source"
    READ_ERROR = "read-error"
    UNSUPPORTED_SENDER_ROLE = "unsupported-sender-role"
    UNSUPPORTED_CONTENT_TYPE = "unsupported-content-type"
    SUPERSEDED_BY_RICHER_ORIGIN = "superseded-by-richer-origin"


# Recognized source kinds. Export importers (claude-export, chatgpt-export) and
# prose-index land in spec 002; the vocabulary is fixed now so the ledger schema
# and digest scheme never need to change shape under them.
SOURCE_KINDS = (
    "claude-transcripts",
    "codex-rollouts",
    "claude-export",
    "chatgpt-export",
    "prose-index",
)


@dataclass(frozen=True)
class SourceFacts:
    """What the source contains — independent of any run, any store.

    Deliberately separate from ``Delta``. "Nothing new in this run" and
    "nothing new in this source" are different facts, and the incident's false
    claim was born from a report that let one masquerade as the other.
    """

    kind: str
    digest: str
    item_count: int
    span_earliest: str | None = None
    span_latest: str | None = None
    windowed: bool = False  # True: absence from source proves nothing about deletion
    file_digest: str | None = None


@dataclass(frozen=True)
class Delta:
    """What this run changed — independent of what the source contains."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0


@dataclass(frozen=True)
class Skip:
    """One enumerated skip. A count cannot be audited; this can."""

    item_id: str
    reason: SkipReason


@dataclass(frozen=True)
class Attribution:
    """Names the prior import a duplicate/overlap refers back to."""

    ledger_id: int
    actor: str
    finished_at: str | None


@dataclass(frozen=True)
class ImportReceipt:
    """The structured result of one import attempt.

    Every import emits both prose (for humans) and this object (for agents).
    Agents parse and relay ``to_json()``; they do not summarize the prose. See
    [[import-ledger-schema]] "Structured output" for the normative shape.
    """

    ledger_id: int
    outcome: Outcome
    source: SourceFacts
    delta: Delta
    skips: list[Skip] = field(default_factory=list)
    duplicate_of: int | None = None
    attribution: Attribution | None = None
    error: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def to_dict(self) -> dict:
        """Exactly the shape in the wiki article's "Structured output" section."""
        out: dict = {
            "schema": "muninn.import-receipt/1",
            "ledger_id": self.ledger_id,
            "outcome": self.outcome.value,
            "duplicate_of": self.duplicate_of,
            "source": {
                "kind": self.source.kind,
                "digest": self.source.digest,
                "item_count": self.source.item_count,
                "span": [self.source.span_earliest, self.source.span_latest],
                "windowed": self.source.windowed,
            },
            "delta": {
                "added": self.delta.added,
                "updated": self.delta.updated,
                "unchanged": self.delta.unchanged,
                "skipped": self.delta.skipped,
            },
            "skips": [{"item_id": s.item_id, "reason": s.reason.value} for s in self.skips],
        }
        if self.source.file_digest:
            out["source"]["file_digest"] = self.source.file_digest
        if self.attribution is not None:
            out["attribution"] = {
                "ledger_id": self.attribution.ledger_id,
                "actor": self.attribution.actor,
                "finished_at": self.attribution.finished_at,
            }
        if self.error is not None:
            out["error"] = self.error
        return out
