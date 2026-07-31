"""Export importers: claude.ai and ChatGPT vendor data exports.

See docs/specs/002-export-importers.md and, more importantly,
.valholl/articles/deterministic-imports.md /
.valholl/articles/import-ledger-schema.md — this spec is where the incident in
those articles actually happened, so their requirements are not optional here.

The one fact everything downstream depends on: **a claude.ai (or ChatGPT)
export is a windowed snapshot, not a complete history.** Absence of a
conversation from an export is not evidence of anything, so this module never
calls the transcript-tree reconciler (``ingest._reconcile_missing``) — it only
ever touches the items the current export actually names.

Six correctness rules, each traced to a real predecessor bug (see the spec's
"Six correctness rules" section for the incident each one prevents):

1. Never coerce a missing item id — skip it as ``missing-item-id``.
2. Never normalize timestamps before hashing — hash ``str(raw_value)`` verbatim.
3. Always include the source kind in the digest preimage.
4. ``windowed=True`` for both vendors, unconditionally.
5. Conservation must close: every input item appears exactly once across
   sessions + skips.
6. "Empty" and "could not read" must be distinguishable skip reasons.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sources
from .digest import digest_file, digest_items
from .ingest import assert_conservation
from .receipt import (
    Attribution,
    Delta,
    ImportReceipt,
    Outcome,
    Skip,
    SkipReason,
    SourceFacts,
)
from .store import Store

# Attachment prose is included, truncated at this length — the predecessor's
# limit, chosen because 200K-char single-message attachments cannot be split
# at turn boundaries without inventing a chunking scheme this module does not
# own.
ATTACHMENT_LIMIT = 30_000


class ExportReadError(Exception):
    """The source could not be loaded at all: no readable JSON array found.

    Covers "no conversations.json in this zip/directory" and "the payload
    is not a top-level JSON array" — both must surface as a named rejection
    (see acceptance criterion 9), never a crash.
    """


# -- Step 1: detection and discovery ----------------------------------------


def detect_kind(payload: list) -> str | None:
    """'claude-export' | 'chatgpt-export' | None. Structural, never filename.

    Both vendors ship a top-level JSON array in a file literally named
    ``conversations.json``, so the filename tells them apart not at all.
    ``mapping`` is checked first: ChatGPT conversations never carry it, and a
    claude.ai payload could in principle carry a coincidental top-level
    ``uuid``-shaped key on some future field, so checking the more specific,
    always-present ChatGPT marker first is what keeps a claude-shaped payload
    from ever being misdetected as ChatGPT (acceptance criterion 1).
    """
    if not isinstance(payload, list) or not payload:
        return None
    sample = [item for item in payload[:20] if isinstance(item, dict)]
    if not sample:
        return None
    if any("mapping" in item for item in sample):
        return "chatgpt-export"
    if any("chat_messages" in item or "uuid" in item for item in sample):
        return "claude-export"
    return None


@dataclass(frozen=True)
class ExportCandidate:
    """One discovered export, ready to hand to ``load_payload``.

    ``path`` always points at something ``load_payload`` can open directly:
    a ``conversations.json`` file (bare, or found inside a directory) or a
    ``.zip``. It never points at a bare directory without having first
    confirmed a ``conversations.json`` lives inside it.
    """

    path: Path
    mtime: float


def find_exports(downloads: Path) -> list[ExportCandidate]:
    """Discover export candidates directly under ``downloads`` (non-recursive).

    Matches: a directory containing ``conversations.json`` (covers both the
    claude.ai ``data-*-batch-0000`` shape and any ChatGPT directory export), a
    bare ``conversations.json``, or a ``.zip`` whose name contains ``chatgpt``
    or ``openai``. Newest first by mtime, so an omitted CLI path picks the
    freshest export automatically.
    """
    downloads = Path(downloads)
    if not downloads.is_dir():
        return []
    candidates: list[ExportCandidate] = []
    for entry in downloads.iterdir():
        try:
            if entry.is_dir():
                conv = entry / "conversations.json"
                if conv.is_file():
                    candidates.append(ExportCandidate(path=conv, mtime=conv.stat().st_mtime))
            elif entry.is_file():
                if entry.name == "conversations.json":
                    candidates.append(ExportCandidate(path=entry, mtime=entry.stat().st_mtime))
                elif entry.suffix == ".zip":
                    lowered = entry.name.lower()
                    if "chatgpt" in lowered or "openai" in lowered:
                        candidates.append(ExportCandidate(path=entry, mtime=entry.stat().st_mtime))
        except OSError:
            continue  # unreadable entry (permissions, broken symlink); not our problem to solve
    candidates.sort(key=lambda c: c.mtime, reverse=True)
    return candidates


def default_downloads_dir() -> Path:
    return Path.home() / "Downloads"


def _read_json_array(fh: Any, label: str) -> list:
    try:
        data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ExportReadError(f"{label} is not valid JSON") from exc
    if not isinstance(data, list):
        raise ExportReadError(f"{label} does not contain a top-level JSON array")
    return data


def _load_from_zip(path: Path) -> tuple[list, str]:
    """Extract in-memory, picking the shallowest ``conversations.json``.

    A zip may contain a decoy nested copy (e.g. a backup-of-a-backup); the
    shallowest path — fewest directory separators, then shortest string — is
    the real export root, matching the predecessor's rule (acceptance
    criterion 9).
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ExportReadError(f"{path} is not a readable zip") from exc
    with zf:
        candidates = [n for n in zf.namelist() if n.endswith("conversations.json")]
        if not candidates:
            raise ExportReadError(f"no conversations.json found inside {path}")
        chosen = min(candidates, key=lambda n: (n.count("/"), len(n)))
        with zf.open(chosen) as fh:
            data = _read_json_array(fh, f"{chosen!r} inside {path.name}")
    return data, digest_file(path)


def load_payload(path: Path) -> tuple[list, str | None]:
    """Return ``(payload, file_digest)``. Handles zip extraction in-memory.

    ``path`` may be a directory (containing ``conversations.json``), a bare
    ``conversations.json``, or a ``.zip``. Raises :class:`ExportReadError` (or
    lets an underlying ``OSError``/``json.JSONDecodeError`` propagate) when
    nothing readable is found — the caller turns that into a named rejection,
    never a crash.
    """
    path = Path(path)
    if path.is_dir():
        candidate = path / "conversations.json"
        if not candidate.is_file():
            raise ExportReadError(f"no conversations.json under {path}")
        with candidate.open("r", encoding="utf-8") as fh:
            data = _read_json_array(fh, str(candidate))
        return data, digest_file(candidate)

    if path.suffix == ".zip":
        return _load_from_zip(path)

    with path.open("r", encoding="utf-8") as fh:
        data = _read_json_array(fh, str(path))
    return data, digest_file(path)


# -- Step 2: parsers ---------------------------------------------------------


def parse_claude_export(
    payload: list,
) -> tuple[list[sources.ParsedSession], list[Skip], list[tuple[str, str]]]:
    """Parse a claude.ai ``conversations.json`` array.

    Every input item appears exactly once across the returned sessions and
    skips (conservation, rule 5). ``pairs`` carries ``(uuid, str(updated_at))``
    for every conversation that has a real ``uuid`` — never a synthetic one —
    which is exactly what ``digest_items`` needs and no more.
    """
    parsed: list[sources.ParsedSession] = []
    skips: list[Skip] = []
    pairs: list[tuple[str, str]] = []

    for i, conv in enumerate(payload):
        if not isinstance(conv, dict):
            skips.append(Skip(item_id=f"item#{i}", reason=SkipReason.UNKNOWN_SCHEMA))
            continue

        item_id = conv.get("uuid")
        if not item_id:
            # Rule 1: never coerce. codexdex's literal "unknown" made every
            # id-less conversation overwrite the last one on disk.
            skips.append(Skip(item_id=f"item#{i}", reason=SkipReason.MISSING_ITEM_ID))
            continue

        updated_raw = conv.get("updated_at")
        if updated_raw is None:
            # The spec's table says this field exists; if it is absent, that
            # is a counted, named discrepancy, not a silent drop.
            skips.append(Skip(item_id=item_id, reason=SkipReason.MISSING_TIMESTAMP))
            continue
        # Rule 2: hash the source's own raw representation, never a
        # converted one. claude.ai's updated_at is already an ISO string, but
        # str() is applied uniformly so this line never has to know that.
        pairs.append((item_id, str(updated_raw)))

        parts: list[str] = []
        saw_unsupported_role = False
        saw_unsupported_content = False
        user_turns = assistant_turns = 0

        for msg in conv.get("chat_messages") or []:
            if not isinstance(msg, dict):
                continue
            sender = msg.get("sender")
            if sender not in ("human", "assistant"):
                saw_unsupported_role = True
                continue
            label = "USER" if sender == "human" else "ASSISTANT"
            if sender == "human":
                user_turns += 1
            else:
                assistant_turns += 1

            text = msg.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(f"[{label}] {text}")

            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        btext = block.get("text")
                        if isinstance(btext, str) and btext.strip():
                            parts.append(f"[{label}] {btext}")
                    else:
                        # Non-text blocks (voice, image, etc.) are multimodal
                        # content this parser cannot extract prose from —
                        # rule 6: that is a distinguishable reason, not "empty".
                        saw_unsupported_content = True

            for att in msg.get("attachments") or []:
                if not isinstance(att, dict):
                    continue
                extracted = att.get("extracted_content")
                if isinstance(extracted, str) and extracted.strip():
                    fname = att.get("file_name") or ""
                    truncated = extracted[:ATTACHMENT_LIMIT]
                    parts.append(f"[{label} ATTACHMENT {fname}] {truncated}")

        if not parts:
            # Rule 6: prefer the specific cause over the generic "no-content"
            # so "4 empty" is never an unauditable blend of genuinely-empty
            # and silently-dropped content.
            reason = SkipReason.NO_CONTENT
            if saw_unsupported_content:
                reason = SkipReason.UNSUPPORTED_CONTENT_TYPE
            elif saw_unsupported_role:
                reason = SkipReason.UNSUPPORTED_SENDER_ROLE
            skips.append(Skip(item_id=item_id, reason=reason))
            continue

        parsed.append(
            sources.ParsedSession(
                session_id=item_id,
                source="claude-cloud",
                provenance="human",
                title=conv.get("name") or None,
                started_at=str(conv["created_at"]) if conv.get("created_at") is not None else None,
                ended_at=str(updated_raw),
                text="\n\n".join(parts),
                user_turns=user_turns,
                assistant_turns=assistant_turns,
            )
        )

    return parsed, skips, pairs


def _chatgpt_node_sort_key(message: dict) -> float:
    ct = message.get("create_time")
    return ct if isinstance(ct, (int, float)) else float("inf")


def parse_chatgpt_export(
    payload: list,
) -> tuple[list[sources.ParsedSession], list[Skip], list[tuple[str, str]]]:
    """Parse a ChatGPT ``conversations.json`` array.

    ``mapping`` is a dict of node-id -> node; messages are a graph, not a
    list. This flattens by sorting on ``message.create_time`` — never by
    walking parent/child pointers — so branches and regenerations collapse by
    timestamp, matching the predecessor (spec: "Graph flattening").
    """
    parsed: list[sources.ParsedSession] = []
    skips: list[Skip] = []
    pairs: list[tuple[str, str]] = []

    for i, conv in enumerate(payload):
        if not isinstance(conv, dict):
            skips.append(Skip(item_id=f"item#{i}", reason=SkipReason.UNKNOWN_SCHEMA))
            continue

        # Rule 1: id -> conversation_id -> uuid, first PRESENT wins. All
        # missing is a named skip, never the literal "unknown" that made
        # codexdex overwrite every id-less conversation onto the last.
        item_id = conv.get("id") or conv.get("conversation_id") or conv.get("uuid")
        if not item_id:
            skips.append(Skip(item_id=f"item#{i}", reason=SkipReason.MISSING_ITEM_ID))
            continue

        create_time = conv.get("create_time")
        raw_created = create_time if create_time is not None else conv.get("created_at")
        update_time = conv.get("update_time")
        raw_updated = update_time if update_time is not None else conv.get("updated_at")

        if raw_updated is None:
            skips.append(Skip(item_id=item_id, reason=SkipReason.MISSING_TIMESTAMP))
            continue
        # Rule 2: ChatGPT's own representation is an epoch FLOAT. str() it
        # verbatim -- converting to ISO here would make the digest depend on
        # sub-second float formatting, and therefore on the Python version.
        pairs.append((item_id, str(raw_updated)))

        mapping = conv.get("mapping")
        nodes: list[dict] = []
        if isinstance(mapping, dict):
            for node in mapping.values():
                if not isinstance(node, dict):
                    continue
                message = node.get("message")
                if isinstance(message, dict):
                    nodes.append(message)
            nodes.sort(key=_chatgpt_node_sort_key)

        parts: list[str] = []
        saw_unsupported_role = False
        saw_unsupported_content = False
        user_turns = assistant_turns = 0

        for message in nodes:
            author = message.get("author")
            role = author.get("role") if isinstance(author, dict) else None
            if role not in ("user", "assistant"):
                saw_unsupported_role = True
                continue
            label = "USER" if role == "user" else "ASSISTANT"
            if role == "user":
                user_turns += 1
            else:
                assistant_turns += 1

            content = message.get("content")
            msg_parts = content.get("parts") if isinstance(content, dict) else None
            for part in msg_parts or []:
                if isinstance(part, str):
                    if part.strip():
                        parts.append(f"[{label}] {part}")
                else:
                    # dict parts are multimodal (image/audio asset refs) --
                    # rule 6: a distinguishable reason, not silent "empty".
                    saw_unsupported_content = True

        if not parts:
            reason = SkipReason.NO_CONTENT
            if saw_unsupported_content:
                reason = SkipReason.UNSUPPORTED_CONTENT_TYPE
            elif saw_unsupported_role:
                reason = SkipReason.UNSUPPORTED_SENDER_ROLE
            skips.append(Skip(item_id=item_id, reason=reason))
            continue

        parsed.append(
            sources.ParsedSession(
                session_id=item_id,
                source="chatgpt-cloud",
                provenance="human",
                title=conv.get("title") or conv.get("name") or None,
                started_at=str(raw_created) if raw_created is not None else None,
                ended_at=str(raw_updated),
                text="\n\n".join(parts),
                user_turns=user_turns,
                assistant_turns=assistant_turns,
            )
        )

    return parsed, skips, pairs


_PARSERS = {"claude-export": parse_claude_export, "chatgpt-export": parse_chatgpt_export}


# -- Step 3: import entry point ----------------------------------------------


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _best_effort_digest(path: Path) -> str:
    """A digest to attach to a ledger row when the source could not be read.

    Never raises: a load failure must still produce a visible, append-only
    ledger row (rule: even a rejection is a recorded claim, not a crash).
    """
    try:
        if Path(path).is_file():
            return digest_file(path)
    except OSError:
        pass
    return "file-sha256:unreadable"


def import_export(st: Store, path: Path, *, actor: str = "cli") -> ImportReceipt:
    """Import one vendor export. Mirrors ``ingest.ingest_path``'s flow:
    load -> detect kind -> digest -> begin_import -> lock -> upsert sessions
    -> record_items -> assert conservation -> finish_import.

    ``windowed=True`` unconditionally for both vendors (rule 4): a claude.ai
    export is a ~30-day window, and nothing proves a ChatGPT export is any
    more complete, so assuming completeness is the dangerous direction. This
    function never calls the transcript-tree reconciler — it only ever
    touches items the export itself names, so a previously-known session
    absent from this export is left completely untouched (acceptance
    criterion 3).
    """
    path = Path(path)

    try:
        payload, file_digest = load_payload(path)
    except Exception as exc:
        digest_for_row = _best_effort_digest(path)
        facts = SourceFacts(kind="unknown", digest=digest_for_row, item_count=0,
                            file_digest=None)
        ledger_id = st.begin_import(actor=actor, source_kind="claude-export",
                                    source_ref=str(path), source_digest=digest_for_row,
                                    facts=facts)
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta(),
                         error=type(exc).__name__)
        return ImportReceipt(ledger_id=ledger_id, outcome=Outcome.REJECTED,
                             source=facts, delta=Delta(), error=type(exc).__name__)

    kind = detect_kind(payload)
    if kind is None:
        digest_for_row = file_digest or _best_effort_digest(path)
        facts = SourceFacts(kind="unknown", digest=digest_for_row, item_count=len(payload),
                            file_digest=file_digest)
        ledger_id = st.begin_import(actor=actor, source_kind="claude-export",
                                    source_ref=str(path), source_digest=digest_for_row,
                                    facts=facts)
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta(),
                         error="UnknownSchema")
        return ImportReceipt(ledger_id=ledger_id, outcome=Outcome.REJECTED,
                             source=facts, delta=Delta(), error="UnknownSchema")

    parser = _PARSERS[kind]
    parsed_sessions, skips, pairs = parser(payload)
    item_count = len(payload)
    items_digest = digest_items(kind, pairs)

    started_ats = [s.started_at for s in parsed_sessions if s.started_at]
    ended_ats = [s.ended_at for s in parsed_sessions if s.ended_at]
    span_earliest = min(started_ats) if started_ats else None
    span_latest = max(ended_ats) if ended_ats else None

    facts = SourceFacts(kind=kind, digest=items_digest, item_count=item_count,
                        span_earliest=span_earliest, span_latest=span_latest,
                        windowed=True, file_digest=file_digest)

    # Look this up before creating our own row, exactly as ingest_path does,
    # so a run never matches against the row it is about to create.
    prior_completed = st.find_import_by_digest(items_digest)

    ledger_id = st.begin_import(actor=actor, source_kind=kind, source_ref=str(path),
                                source_digest=items_digest, facts=facts)

    holder = st.acquire_import_lock(ledger_id, actor, os.getpid())
    if holder is not None:
        st.finish_import(ledger_id, outcome=Outcome.REJECTED, delta=Delta())
        return ImportReceipt(
            ledger_id=ledger_id, outcome=Outcome.REJECTED, source=facts, delta=Delta(),
            attribution=Attribution(ledger_id=holder["ledger_id"], actor=holder["actor"],
                                    finished_at=holder["acquired_at"]),
        )

    now = _now()
    added = updated = unchanged = 0
    item_receipts: list[tuple[str, str, str | None]] = []

    for sess in parsed_sessions:
        existing = st.get_session(sess.session_id)
        # Change detection is per item: an existing session whose stored
        # ended_at already equals the source's updated_at is unchanged --
        # never re-written, never counted as new work.
        if existing is not None and existing.get("ended_at") == sess.ended_at:
            unchanged += 1
            item_receipts.append((sess.session_id, "unchanged", None))
            continue

        st.upsert_session({
            "session_id": sess.session_id,
            "source": sess.source,
            "provenance": sess.provenance,
            "parent_id": sess.parent_id,
            "cwd": sess.cwd,
            "branch": sess.branch,
            "model": sess.model,
            "title": sess.title,
            "started_at": sess.started_at,
            "ended_at": sess.ended_at,
            "duration_s": sess.duration_s,
            "user_turns": sess.user_turns,
            "assistant_turns": sess.assistant_turns,
            "tool_uses": sess.tool_uses,
            "tool_results": sess.tool_results,
            "words": sess.words,
            "tokens": sess.tokens,
            "text": sess.text,
            "source_path": str(path),
            "source_present": 1,
            "origin": "raw",
            "ingested_at": now if existing is None else None,
            "updated_at": now,
        })
        st.replace_chunks(sess.session_id, sess.text)
        st.set_files(sess.session_id, sess.files)
        st.set_tools(sess.session_id, sess.tools)

        if existing is None:
            added += 1
            item_receipts.append((sess.session_id, "added", None))
        else:
            updated += 1
            item_receipts.append((sess.session_id, "updated", None))

    for skip in skips:
        item_receipts.append((skip.item_id, "skipped", skip.reason.value))

    # Rule 5: the arithmetic must close. A mismatch leaves this ledger row
    # unfinished rather than finished with fabricated counts.
    assert_conservation(added, updated, unchanged, len(skips), item_count)

    delta = Delta(added=added, updated=updated, unchanged=unchanged, skipped=len(skips))

    duplicate_of: int | None = None
    if added == 0 and updated == 0:
        if skips and unchanged == 0 and item_count > 0:
            # Every item failed outright and there is no prior successful
            # import to attribute this to: a rejection, not an empty success.
            outcome = Outcome.REJECTED
        elif prior_completed is not None:
            # The exact actor-A/actor-B scenario this whole subsystem exists
            # to fix: the claim is "already imported", never "imported, with
            # zero counts" (which reads as empty).
            outcome = Outcome.DUPLICATE
            duplicate_of = prior_completed["ledger_id"]
        else:
            # Genuinely nothing to change (e.g. a source with zero items) and
            # no prior row to attribute it to.
            outcome = Outcome.IMPORTED
    else:
        outcome = Outcome.PARTIAL if skips else Outcome.IMPORTED

    st.record_items(ledger_id, item_receipts)
    st.finish_import(ledger_id, outcome=outcome, delta=delta, duplicate_of=duplicate_of)
    st.release_import_lock(ledger_id)

    attribution: Attribution | None = None
    if outcome is Outcome.DUPLICATE and prior_completed is not None:
        attribution = Attribution(
            ledger_id=prior_completed["ledger_id"],
            actor=prior_completed["actor"],
            finished_at=prior_completed["finished_at"],
        )

    receipt = ImportReceipt(
        ledger_id=ledger_id, outcome=outcome, source=facts, delta=delta,
        skips=skips, duplicate_of=duplicate_of, attribution=attribution,
    )
    st.commit()
    return receipt


# -- Deletion receipts --------------------------------------------------------


def verify_safe_to_delete(st: Store, path: Path) -> tuple[bool, str]:
    """Answer **only** from the ledger whether ``path`` is safe to delete.

    Recomputes the digest from the source itself and confirms a completed
    (``finished_at IS NOT NULL``) row with outcome in (imported, duplicate)
    covers it, and that every item the source names has an ``import_items``
    row somewhere with a non-skipped disposition. Anything else is a refusal
    with the reason -- the caller must never fall back to printing a bare
    "yes" (see .valholl/articles/import-ledger-schema.md, "Deletion receipts").
    """
    try:
        payload, _file_digest = load_payload(path)
    except Exception as exc:
        return False, f"cannot read source: {type(exc).__name__}"

    kind = detect_kind(payload)
    if kind is None:
        return False, "source format not recognized (neither claude-export nor chatgpt-export)"

    parser = _PARSERS[kind]
    _sessions, _skips, pairs = parser(payload)
    items_digest = digest_items(kind, pairs)

    row = st.find_import_by_digest(items_digest)
    if row is None:
        return False, "no completed import in the ledger matches this exact source content"
    if row["outcome"] not in (Outcome.IMPORTED.value, Outcome.DUPLICATE.value):
        return False, f"the matching ledger row's outcome is {row['outcome']!r}, not a success"

    item_ids = sorted({item_id for item_id, _updated_at in pairs})
    missing = []
    for item_id in item_ids:
        cur = st.conn.execute(
            "SELECT 1 FROM import_items WHERE item_id = ? AND disposition != 'skipped' LIMIT 1",
            (item_id,),
        )
        if cur.fetchone() is None:
            missing.append(item_id)
    if missing:
        return False, f"{len(missing)} item(s) in this source have no recorded successful import"

    return True, f"every item in this source (import #{row['ledger_id']}) is recorded as imported"
