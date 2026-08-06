"""Backfill the predecessors' prose indexes, which hold the only copy of a lot.

`claudex` and `codexdex` indexed the same transcripts Muninn does, into flat
prose files. Muninn was designed to supersede both — but **their indexes are
archives too**. `~/.claudex/index` holds thousands of files covering sessions
whose raw transcripts the vendor swept months ago, and that data is not
recoverable from anywhere else. Archiving the repositories is harmless; anyone
concluding that the *indexes* are therefore disposable would destroy the oldest
part of the corpus. See tohuw/muninn#6 and
.valholl/articles/archive-of-record.md.

So the honest migration order is: backfill first, verify nothing was lost, and
only then retire the predecessors. This module is the first step.

## The format, and why it is parsed rather than trusted

Both tools wrote ``# key: value`` headers followed by the prose body. The shapes
differ (claudex has ``kind``/``parent``/``branch``; codexdex has
``source``/``path``/``model``/``title``; claudex's cloud index has ``name`` and
no ``cwd`` at all), so headers are read generically and every field is optional.
A predecessor's format is exactly as unstable as a vendor's — more so, since
nobody is maintaining it — and the same fail-soft rule applies: a missing or
renamed key is an enumerated skip, never an exception.

## Prose-index sessions never overwrite raw-derived ones

``origin`` distinguishes them, and raw wins. A raw transcript yields tool calls,
files touched, models, token counts and per-turn structure; a prose file yields
text and a handful of headers. Overwriting the first with the second would be a
silent downgrade of an already-ingested session, which is why
``SkipReason.SUPERSEDED_BY_RICHER_ORIGIN`` exists and was reserved for exactly
this before any importer needed it. The skip is *recorded per item*, so "why is
this session not from the backfill" is a query rather than an inference.

## ``source_present = 0``, always

A prose-index entry exists because a predecessor archived a transcript, and for
most of this corpus that transcript is long gone. Muninn cannot see the original
from here — claudex did not even record its path — so claiming presence would
overstate what survives and corrupt the one statistic that matters most, the
count of sessions whose only copy is this archive. Recording absence is the
conservative direction and it is self-correcting: if the raw file does still
exist, the next `muninn index` ingests it as ``origin = 'raw'`` and sets presence
back.

``source_path`` is left ``NULL`` for the same reason, which also keeps these rows
out of the local sweep's reconciler — it only considers rows that name a path —
so nothing flaps them back and forth.

## The body is stored verbatim

Including the predecessors' ``[USER <timestamp>]`` markers, which differ from the
``[USER]`` form Muninn's own adapters emit. Normalising them would be a lossy
rewrite of the only surviving copy of that text to make it cosmetically match
text that is not at risk. The prose is the thing being rescued; it is not the
place to tidy.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sources
from .digest import digest_items, digest_tree
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
from .sources import ParsedSession
from .store import Store

SOURCE_KIND = "prose-index"
ORIGIN = "prose-index"

#: Where the predecessors keep their indexes, and the source each contributes.
#: ``cloud`` is a sub-index rather than a separate tool: claudex archived
#: claude.ai conversations alongside local ones, and they are ``claude-cloud``
#: sessions — the same source string ``exports.parse_claude_export`` writes, so
#: a backfilled conversation and a re-imported vendor export land on one row
#: instead of two.
KNOWN_ROOTS: tuple[tuple[str, str], ...] = (
    (".claudex", "claude"),
    (".codexdex", "codex"),
)

_CLOUD_SOURCE = "claude-cloud"

#: Every directory a predecessor wrote prose into, relative to its root.
#:
#: This list is the whole lesson of tohuw/muninn#6. The first implementation
#: walked ``index`` and ``cloud/index`` and looked complete — it recovered 3,738
#: files and verified 26.4 M words byte-for-byte, which is exactly the kind of
#: result that stops anyone looking further. It was still missing four
#: directories, because a prose index is not one directory: it is one per *kind
#: of thing the tool learned to archive*, added over time, and nothing announces
#: a new one.
#:
#: ``cloud/projects/index-deleted`` is the reason this is not a nice-to-have.
#: Those files are project memory **cleared upstream** — the manifest says so in
#: as many words — so they exist in exactly one place on earth, which is the
#: directory a two-entry list did not walk.
INDEX_DIRS: tuple[str, ...] = (
    "index",                        # local Claude Code sessions and subagents
    "cloud/index",                  # claude.ai conversations
    "cloud/projects/index",         # project definitions, with memory inlined
    "cloud/projects/index-deleted",  # project memory the vendor deleted
    "cloud/memory/index",           # user memory documents
)

#: ``# kind:`` header -> the ``source`` a row gets. Kinds absent here (``session``,
#: ``subagent``) keep the root's default source, because they *are* the thing the
#: root is about; these are the ones that are not conversations at all and would
#: be indistinguishable in a search result if they shared a source with them.
KIND_SOURCE: dict[str, str] = {
    "cloud": _CLOUD_SOURCE,
    "project": "claude-project",
    "project-memory": "claude-project",
    "memory": "claude-memory",
}

#: Kinds that are not conversations and must not be run through the turn-count
#: classifier. See :func:`parse_prose_file` for what happens when they are.
NON_CONVERSATION_KINDS = frozenset({"project", "project-memory", "memory"})

#: claudex's outcome vocabulary -> Muninn's closed one. ``reference`` maps to
#: ``exploratory`` rather than ``fixed``: a lookup that answered a question
#: resolved nothing, and calling it ``fixed`` would make `--outcome fixed`
#: return 258 timezone conversions on this corpus.
_OUTCOME_MAP = {
    "resolved": "fixed",
    "reference": "exploratory",
    "ongoing": "ongoing",
    "abandoned": "abandoned",
    "exploratory": "exploratory",
    "fixed": "fixed",
}


@dataclass(frozen=True)
class BackfillResult:
    """What one backfill did: an import receipt, plus facets harvested.

    A wrapper rather than two extra fields on ``ImportReceipt``, because that
    type is the ledger's closed shape (``muninn.import-receipt/1``) and it is
    frozen for the same reason it is closed — an agent parses it, and a field
    that appears on some receipts and not others is a field nobody can rely on.
    Harvesting a predecessor's summaries is a *second* thing this importer does,
    not a property of the import, so it is reported alongside rather than inside.

    Attribute access falls through to the receipt so every existing caller and
    test reads unchanged.
    """

    receipt: ImportReceipt
    facets_harvested: int = 0

    def __getattr__(self, name: str):
        return getattr(self.receipt, name)


@dataclass(frozen=True)
class ProseIndexCandidate:
    """One predecessor index directory, and the default source for its files."""

    path: Path
    default_source: str

    @property
    def file_count(self) -> int:
        return len(discover(self.path))


def default_home() -> Path:
    return Path.home()


def find_prose_indexes(home: Path | None = None) -> list[ProseIndexCandidate]:
    """Predecessor indexes present under ``home``, in a stable order.

    Only directories that actually hold prose files are returned. An empty
    ``~/.codexdex`` is a real state on this corpus — codexdex was never run on
    the development machine before Muninn's design work — and reporting it as a
    source with nothing in it would be indistinguishable from a parse failure.
    """
    home = default_home() if home is None else home
    found = []
    for name, source in KNOWN_ROOTS:
        root = home / name
        if root.is_dir() and discover(root):
            found.append(ProseIndexCandidate(root, source))
    return found


def discover(root: Path) -> list[Path]:
    """Prose files under ``root``, across every index directory, sorted.

    See :data:`INDEX_DIRS` for why this is a list rather than one path, and for
    which omission made it a list.
    """
    if not root.is_dir():
        return []
    out: list[Path] = []
    for sub in INDEX_DIRS:
        directory = root / sub
        if directory.is_dir():
            out.extend(p for p in sorted(directory.glob("*.txt")) if p.is_file())
    return sorted(out)


# ── Parsing ───────────────────────────────────────────────────────────────────

def parse_headers(text: str) -> tuple[dict[str, str], str]:
    """Split ``# key: value`` headers from the body.

    The header block ends at the first line that is not a ``#`` comment, so a
    body line beginning with ``#`` (a markdown heading in a transcript, which is
    extremely common) cannot be mistaken for a header. Unrecognised keys are
    kept rather than dropped: this is a format nobody maintains, and a caller
    that wants one key should not silently discard the evidence of others.
    """
    headers: dict[str, str] = {}
    lines = text.split("\n")
    body_starts = 0
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            body_starts = i
            break
        key, sep, value = line[1:].partition(":")
        if sep:
            headers.setdefault(key.strip().lower(), value.strip())
        body_starts = i + 1
    return headers, "\n".join(lines[body_starts:]).strip("\n")


def _int_after(turns: str, label: str) -> int:
    """``user=3 assistant=242`` -> the number after ``label``, or 0."""
    for token in turns.split():
        name, sep, value = token.partition("=")
        if sep and name == label:
            try:
                return int(value)
            except ValueError:
                return 0
    return 0


def parse_prose_file(path: Path, default_source: str) -> ParsedSession | Skip:
    """One prose file into a session, or an enumerated skip.

    Never raises. Every failure is a ``Skip`` carrying the file's stem as its id,
    so "which files did not make it, and why" is answerable from the ledger
    afterwards rather than by re-running and watching. A count could not be
    audited; this can.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Skip(item_id=path.stem, reason=SkipReason.READ_ERROR)

    headers, body = parse_headers(text)
    if not headers:
        return Skip(item_id=path.stem, reason=SkipReason.UNKNOWN_SCHEMA)

    kind = headers.get("kind", "")

    # Sessions keep their bare id, because that id is the one the vendor's own
    # tooling uses and `muninn resume` prints. Everything else is namespaced by
    # kind: a project and a project's deleted memory are both keyed on the
    # project uuid, so an un-namespaced id would silently collapse the two —
    # and the surviving one would be whichever was walked last.
    session_id = headers.get("session") or ""
    if not session_id:
        stem = headers.get("project") or path.stem
        session_id = f"{kind}:{stem}" if kind else stem
    if not session_id:
        return Skip(item_id=path.stem, reason=SkipReason.MISSING_ITEM_ID)
    if not body.strip():
        return Skip(item_id=session_id, reason=SkipReason.NO_CONTENT)

    source = headers.get("source") or KIND_SOURCE.get(kind) or default_source
    turns = headers.get("turns", "")

    sess = ParsedSession(
        session_id=session_id,
        source=source,
        text=body,
        parent_id=headers.get("parent"),
        cwd=headers.get("cwd") or None,
        branch=headers.get("branch") or None,
        model=headers.get("model") or None,
        title=headers.get("title") or headers.get("name") or None,
        started_at=_iso_or_none(headers.get("start")),
        ended_at=_iso_or_none(headers.get("end")),
        user_turns=_int_after(turns, "user"),
        assistant_turns=_int_after(turns, "assistant"),
    )
    sess.duration_s = _duration(sess.started_at, sess.ended_at)
    if kind in NON_CONVERSATION_KINDS:
        # A project definition or a memory document is a human-authored
        # artifact, not a conversation — it has no turns at all, which is a
        # different fact from "it had zero user turns".
        #
        # Running the ordinary classifier on one gets this exactly backwards.
        # ``sources.classify`` reads ``user_turns == 0`` as the signature of a
        # programmatic ``claude -p`` call, so every project and every memory
        # file lands in ``tool-invoked`` — and that class is excluded from
        # default search, from every survey statistic, and from enrichment. The
        # real survey caught it: 44 projects holding 586,186 words and 18 memory
        # documents, all silently filed as machine byproducts and unreachable
        # from `muninn search`.
        #
        # This is the length-based-classification trap wearing a different
        # coat: the rule is that provenance is *structural*, and the structure
        # here is the ``kind`` header, not a turn count that was never written.
        sess.provenance = sources.HUMAN
    else:
        # Structural, exactly as for raw transcripts: the same classifier, fed
        # the cwd and turn counts the header preserved.
        sess.provenance = sources.classify(sess, is_subagent=(kind == "subagent"))
    return sess


# ── The predecessor's own enrichment ──────────────────────────────────────────

#: Where claudex wrote its per-session summaries, alongside the index they
#: describe. These are *facets*, in Muninn's sense: YAML frontmatter carrying
#: topic / outcome / keywords, then a prose summary.
SUMMARY_DIRS: tuple[str, ...] = ("summaries", "cloud/summaries")


def find_summaries(root: Path) -> dict[str, Any]:
    """``session_id -> Facets`` for every summary the predecessor already wrote.

    Worth harvesting for two independent reasons, and the second is the one that
    matters more.

    The cheap reason: these were paid for once already. On this corpus there are
    811 of them, so importing them is 811 model calls `muninn enrich` does not
    have to make.

    The real reason: **a summary is prose too.** It was generated from a
    transcript that may no longer exist, by a model that is no longer the
    default, and nothing else in the world has a copy. Regenerating it later is
    not the same artifact, and discarding it because Muninn can produce its own
    would be the archive-of-record mistake wearing a different hat.

    Never raises: a malformed summary is skipped, because losing one summary
    must not cost the backfill the sessions it was attached to.
    """
    from .enrich import Facets, UNCLEAR_OUTCOME

    found: dict[str, Any] = {}
    for sub in SUMMARY_DIRS:
        directory = root / sub
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            meta, body = _split_frontmatter(text)
            topic = meta.get("topic", "").strip()
            if not topic:
                continue
            # claudex's vocabulary is wider than Muninn's and occasionally
            # freeform ("ongoing (collection design articulated)"). Take the
            # first recognised token rather than rejecting the summary — the
            # topic and prose are the valuable parts, and an unmappable outcome
            # is worth less than losing them.
            outcome = UNCLEAR_OUTCOME
            for token in re.split(r"[^a-z]+", meta.get("outcome", "").lower()):
                if token in _OUTCOME_MAP:
                    outcome = _OUTCOME_MAP[token]
                    break
            found[path.stem] = Facets(
                topic=topic[:500],
                outcome=outcome,
                summary=body.strip()[:4000],
                entities=tuple(_split_keywords(meta.get("keywords", ""))),
            )
    return found


#: The three keys a summary carries, tolerating markdown emphasis around them.
#:
#: A strict ``---``-delimited YAML reader looked right and silently dropped 13
#: of 811 summaries on the real corpus, because the model that wrote them
#: drifted between four shapes over time: plain ``topic:``, bold
#: ``**topic:**``, a ``**SUMMARY**`` banner *before* the frontmatter, and bold
#: keys with no delimiters at all. Every one of those is a real summary of a
#: conversation that may no longer exist, and none of them is worth losing to a
#: parser being principled about YAML.
#:
#: This is the same fail-soft rule the transcript adapters follow, applied to a
#: format that is even less of a contract: nobody was maintaining it, and the
#: thing that produced it was itself a language model.
_META_RE = re.compile(
    r"^[ \t>*_]*(topic|outcome|keywords)[*_]*[ \t]*:[ \t]*(.*)$",
    re.IGNORECASE | re.MULTILINE)

#: How far into a file to look for metadata. Past this it is prose that happens
#: to contain the word "topic:", not a header.
_META_SCAN_CHARS = 2000


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """``(metadata, body)`` from a predecessor summary, however it was written.

    Scans the head of the document for the three known keys rather than
    requiring a delimiter block, then treats everything after the last one as
    the summary prose.
    """
    head = text[:_META_SCAN_CHARS]
    meta: dict[str, str] = {}
    end = 0
    for match in _META_RE.finditer(head):
        # Strip whitespace, then emphasis, then whitespace again: `**topic:** T`
        # captures `** T`, so a single pass leaves the value with a leading
        # space and `[a, b]` never loses its bracket.
        meta.setdefault(match.group(1).lower(),
                        match.group(2).strip().strip("*_").strip())
        end = match.end()
    if not meta:
        return {}, text
    # Drop a trailing `---` delimiter and blank lines between the metadata and
    # the prose, so the stored summary starts at the first real sentence.
    body = text[end:].lstrip("\n")
    while body.startswith("---"):
        body = body[3:].lstrip("\n")
    return meta, body


def _split_keywords(raw: str) -> list[str]:
    """``[a, b, c]`` or ``a, b, c`` -> a list, capped."""
    return [k.strip() for k in raw.strip("[]").split(",") if k.strip()][:12]


def _iso_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value


def _duration(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        a = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (b - a).total_seconds()


# ── Import ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def import_prose_index(st: Store, root: Path, *, default_source: str = "claude",
                       actor: str = "cli") -> ImportReceipt:
    """Backfill one predecessor index. Mirrors ``exports.import_export``'s flow.

    ``windowed=False``: a prose index is a complete archive of what its tool ever
    saw, not a rolling window. That said, this importer **never reconciles** —
    it only ever touches sessions the index itself names, so a session absent
    from it is left entirely alone rather than flagged. Absence from a
    predecessor's index proves only that the predecessor did not index it.
    """
    root = Path(root)
    paths = discover(root)
    item_count = len(paths)

    parsed: list[ParsedSession] = []
    skips: list[Skip] = []
    for path in paths:
        result = parse_prose_file(path, default_source)
        (skips if isinstance(result, Skip) else parsed).append(result)

    # Digest over (id, end) pairs, so a re-run of an unchanged index is a
    # DUPLICATE with attribution rather than an "imported, 0 added" line an
    # agent has to interpret. digest_tree covers the bytes; this covers the
    # content, which is what "the same import" means.
    pairs = [(s.session_id, s.ended_at or "") for s in parsed]
    items_digest = digest_items(SOURCE_KIND, pairs)

    started = [s.started_at for s in parsed if s.started_at]
    ended = [s.ended_at for s in parsed if s.ended_at]
    facts = SourceFacts(
        kind=SOURCE_KIND, digest=items_digest, item_count=item_count,
        span_earliest=min(started) if started else None,
        span_latest=max(ended) if ended else None,
        windowed=False,
        file_digest=digest_tree(root, paths) if paths else None,
    )

    prior_completed = st.find_import_by_digest(items_digest)
    ledger_id = st.begin_import(actor=actor, source_kind=SOURCE_KIND,
                                source_ref=str(root), source_digest=items_digest,
                                facts=facts)

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
    summaries = find_summaries(root)
    facets_written = 0

    for sess in parsed:
        existing = st.get_session(sess.session_id)
        if existing is not None and existing.get("origin") != ORIGIN:
            # A raw-derived session is strictly richer. Recorded as a skip with
            # a reason rather than passed over, so the decision is auditable.
            skips.append(Skip(item_id=sess.session_id,
                              reason=SkipReason.SUPERSEDED_BY_RICHER_ORIGIN))
            continue
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
            # Zero because the columns are NOT NULL and a prose index recorded
            # no tool activity — which is "we cannot know", not "there was
            # none". The schema has no way to say the first, and inventing one
            # for a backfill would be a wide change for a narrow gain; the
            # `origin` column is what tells a reader how much to trust these.
            "tool_uses": 0,
            "tool_results": 0,
            "words": sess.words,
            "text": sess.text,
            # NULL path and absent source: see the module docstring. These rows
            # are the irreplaceable ones by construction.
            "source_path": None,
            "source_present": 0,
            "origin": ORIGIN,
            "ingested_at": now if existing is None else None,
            "updated_at": now,
        })
        st.replace_chunks(sess.session_id, sess.text)

        if existing is None:
            added += 1
            item_receipts.append((sess.session_id, "added", None))
        else:
            updated += 1
            item_receipts.append((sess.session_id, "updated", None))

    # Summaries are applied to **every** session the archive holds, not only to
    # the ones this pass added — and that distinction was a real bug, caught by
    # running it for real.
    #
    # A summary and a transcript are different artifacts. When the vendor export
    # supplies a richer copy of a conversation, its 2,649 prose entries are
    # correctly skipped as `superseded-by-richer-origin` — but claudex's
    # *summary* of that conversation is not superseded by anything, because the
    # export contains no summaries at all. Scoping the harvest to added rows
    # dropped 705 of 811 on this corpus: exactly the ones whose transcripts were
    # best covered.
    #
    # The `topic` guard still holds: a session Muninn has already enriched keeps
    # its own richer facets.
    for session_id, facets in summaries.items():
        existing = st.get_session(session_id)
        if existing is None or existing.get("topic"):
            continue
        st.set_facets(session_id, facets)
        facets_written += 1

    for skip in skips:
        item_receipts.append((skip.item_id, "skipped", skip.reason.value))

    assert_conservation(added, updated, unchanged, len(skips), item_count)
    delta = Delta(added=added, updated=updated, unchanged=unchanged, skipped=len(skips))

    duplicate_of: int | None = None
    if added == 0 and updated == 0:
        if skips and unchanged == 0 and item_count > 0 and prior_completed is None:
            outcome = Outcome.REJECTED
        elif prior_completed is not None:
            outcome = Outcome.DUPLICATE
            duplicate_of = prior_completed["ledger_id"]
        else:
            outcome = Outcome.IMPORTED
    else:
        outcome = Outcome.PARTIAL if skips else Outcome.IMPORTED

    st.record_items(ledger_id, item_receipts)
    st.finish_import(ledger_id, outcome=outcome, delta=delta, duplicate_of=duplicate_of)
    st.release_import_lock(ledger_id)

    attribution: Attribution | None = None
    if outcome is Outcome.DUPLICATE and prior_completed is not None:
        attribution = Attribution(ledger_id=prior_completed["ledger_id"],
                                  actor=prior_completed["actor"],
                                  finished_at=prior_completed["finished_at"])

    st.commit()
    return BackfillResult(
        receipt=ImportReceipt(ledger_id=ledger_id, outcome=outcome, source=facts,
                              delta=delta, skips=skips, duplicate_of=duplicate_of,
                              attribution=attribution),
        facets_harvested=facets_written,
    )
