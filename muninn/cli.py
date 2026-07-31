"""Command line interface.

The CLI is Muninn's public boundary. Agents and humans both go through it; it is
the only supported way to ask the archive questions, so that internals stay free
to change. (Huginn's skill takes the same stance, and it is a good one.)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

from . import __version__, exports, indexer, ingest, queue, store
from .hooks import install as hooks_install
from .paths import DB_PATH, QUEUE_DIR, STATE_DIR, default_roots
from .query import Filters
from .receipt import Outcome

# Re-exported for backward compatibility: STATE_DIR/DB_PATH/default_roots used
# to live here. They now live in muninn/paths.py so muninn/hooks/cli.py (the
# SessionEnd hook) can resolve QUEUE_DIR without importing this module, which
# transitively imports sqlite3 via muninn.store — see paths.py.
__all__ = ["STATE_DIR", "DB_PATH", "QUEUE_DIR", "default_roots", "main", "build_parser"]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def cmd_index(args: argparse.Namespace) -> int:
    """Ingest transcripts and report both the source facts and this run's delta.

    ``--json`` prints only the receipt(s) — nothing else on stdout — so an
    agent can parse and relay a claim the ledger can prove, rather than
    summarizing prose (see .valholl/articles/deterministic-imports.md). The
    human-readable form keeps "what the source contains" and "what this run
    changed" as two labelled sections that are never blended into one line
    (invariant 4) — that blending is exactly what let claudex's "0 written, 61
    cached" read as "nothing new" when it meant "already imported".
    """
    roots = default_roots()
    if args.source:
        roots = {k: v for k, v in roots.items() if k == args.source}
    st = store.open_store(args.db)

    if args.watch:
        # Continuous mode: sweep, then drain the queue and react to file
        # events indefinitely. See .valholl/articles/continuous-ingest-not-periodic.md
        # — this is the long-running "background indexer" layer, not a
        # one-shot sweep. Import receipts are logged one line per import
        # rather than collected, since the process never terminates on its
        # own to print a final summary.
        def _log(receipts: list) -> None:
            for r in receipts:
                print(f"import #{r.ledger_id} {r.outcome.value} "
                      f"added={r.delta.added} updated={r.delta.updated} "
                      f"skipped={r.delta.skipped}")

        print(f"muninn indexer watching {', '.join(str(p) for p in roots.values())}")
        try:
            indexer.watch(st, roots, on_receipts=_log)
        finally:
            st.close()
        return 0

    # A one-shot `muninn index` walks every configured root exactly like
    # indexer.sweep() does (that function IS ingest_path over every root) —
    # so it must also record a sweep timestamp. Without this, doctor would
    # report "last sweep: never" moments after a full reconciling scan just
    # ran via the CLI, which is exactly the kind of invisible staleness
    # continuous-ingest-not-periodic.md warns against.
    receipts = []
    for source, root in roots.items():
        if not root.is_dir():
            if not args.json:
                print(f"{source:7} no transcripts at {root}")
            continue
        result = ingest.ingest_path(st, root, source, actor="cli")
        if result.receipt is not None:
            receipts.append(result.receipt)
        if not args.json:
            _print_index_result(source, result)
    st.record_sweep(_now_iso())
    if args.json:
        print(json.dumps([r.to_dict() for r in receipts]))
    else:
        print(f"\narchive: {st.count_sessions():,} sessions · {st.count_chunks():,} chunks "
              f"· {_size(args.db)}")
    st.close()
    return 0


def _print_index_result(source: str, result: ingest.IngestResult) -> None:
    receipt = result.receipt
    if receipt is None:
        # Should not happen once ingest_path always populates a receipt; fall
        # back to the legacy line rather than crashing the CLI on a defect
        # elsewhere.
        print(f"{source:7} scanned {result.scanned:,} · new {result.ingested:,} · "
              f"updated {result.updated:,} · unchanged {result.skipped_unchanged:,}")
        return

    src = receipt.source
    span = ""
    if src.span_earliest and src.span_latest:
        span = f" · {src.span_earliest[:10]} .. {src.span_latest[:10]}"
    print(f"source   {src.kind} · {src.item_count:,} items{span}")

    d = receipt.delta
    line = (f"this run added {d.added:,} · updated {d.updated:,} · "
            f"unchanged {d.unchanged:,} · skipped {d.skipped:,}")
    if result.marked_missing:
        line += f" · source gone {result.marked_missing:,}"
    print(line)

    if receipt.outcome is Outcome.DUPLICATE and receipt.attribution is not None:
        a = receipt.attribution
        print(f"duplicate of import #{a.ledger_id} (actor {a.actor}, "
              f"finished {a.finished_at})")
    elif receipt.outcome is Outcome.REJECTED and receipt.attribution is not None:
        a = receipt.attribution
        print(f"rejected: import in progress by actor {a.actor} since {a.finished_at}")
    elif receipt.outcome is Outcome.REJECTED:
        print("rejected: no items could be imported")


def _filters_from_args(args: argparse.Namespace) -> Filters:
    return Filters(
        repo=args.repo,
        branch=args.branch,
        file=args.file,
        tool=args.tool,
        model=args.model,
        provenance=args.provenance,
        source=args.source,
        since=args.since,
        until=args.until,
        outcome=args.outcome,
    )


def cmd_import(args: argparse.Namespace) -> int:
    """Import a claude.ai or ChatGPT vendor export.

    ``--verify-safe-to-delete`` answers only from the ledger (never by the
    agent's own judgement, per .valholl/articles/deterministic-imports.md
    "Deletion receipts") and never emits a bare "yes": the reason is always
    printed alongside the verdict.
    """
    if args.path:
        path = Path(args.path)
    else:
        candidates = exports.find_exports(exports.default_downloads_dir())
        if not candidates:
            print("no export found under ~/Downloads")
            return 1
        path = candidates[0].path

    if args.verify_safe_to_delete:
        st = store.open_store(args.db)
        safe, reason = exports.verify_safe_to_delete(st, path)
        st.close()
        if args.json:
            print(json.dumps({"safe_to_delete": safe, "reason": reason}))
        else:
            verdict = "safe to delete" if safe else "NOT safe to delete"
            print(f"{verdict}: {reason}")
        return 0 if safe else 1

    st = store.open_store(args.db)
    receipt = exports.import_export(st, path, actor=args.actor)
    st.close()

    if args.json:
        print(receipt.to_json())
        return 0

    _print_export_result(receipt)
    return 0


def _print_export_result(receipt) -> None:
    src = receipt.source
    span = ""
    if src.span_earliest and src.span_latest:
        span = f" · {src.span_earliest[:10]} .. {src.span_latest[:10]}"
    windowed = " · windowed (~30d)" if src.windowed else ""
    print(f"source   {src.kind} · {src.item_count:,} items{span}{windowed}")

    d = receipt.delta
    print(f"this run added {d.added:,} · updated {d.updated:,} · "
          f"unchanged {d.unchanged:,} · skipped {d.skipped:,}")

    if receipt.outcome is Outcome.DUPLICATE and receipt.attribution is not None:
        a = receipt.attribution
        print(f"         duplicate of import #{a.ledger_id} "
              f"(actor {a.actor}, finished {a.finished_at})")
    elif receipt.outcome is Outcome.REJECTED and receipt.attribution is not None:
        a = receipt.attribution
        print(f"         rejected: import in progress by actor {a.actor} "
              f"since {a.finished_at}")
    elif receipt.outcome is Outcome.REJECTED:
        err = f" ({receipt.error})" if receipt.error else ""
        print(f"         rejected: source could not be imported{err}")

    if receipt.skips:
        by_reason: dict[str, int] = {}
        for skip in receipt.skips:
            by_reason[skip.reason.value] = by_reason.get(skip.reason.value, 0) + 1
        parts = ", ".join(f"{reason} ×{n}" for reason, n in sorted(by_reason.items()))
        print(f"skips    {len(receipt.skips)} items: {parts}")

    if src.windowed:
        print("note: absence from a windowed export does not indicate upstream deletion.")


def cmd_search(args: argparse.Namespace) -> int:
    st = store.open_store(args.db)
    try:
        hits = st.search(args.query, limit=args.limit, filters=_filters_from_args(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        st.close()
        return 2
    if not hits:
        if args.json:
            print(json.dumps([]))
        else:
            print("no matches")
        st.close()
        return 0 if args.json else 1

    if args.json:
        # Stable keys, one object per session (spec 004 acceptance #12) — an
        # agent parses this instead of scraping the human-readable form.
        out = [{
            "session_id": hit["session_id"],
            "source": hit.get("source"),
            "provenance": hit.get("provenance"),
            "started_at": hit.get("started_at"),
            "cwd": hit.get("cwd"),
            "words": hit.get("words"),
            "excerpt": " ".join((hit.get("excerpt") or "").split()),
            "score": hit.get("score"),
            "chunk_hits": hit.get("chunk_hits"),
        } for hit in hits]
        print(json.dumps(out))
        st.close()
        return 0

    for hit in hits:
        sid = hit["session_id"]
        rec = st.get_session(sid) or {}
        when = (rec.get("started_at") or "")[:10]
        # A subagent inherits its parent's working directory for display; Codex
        # rollouts may have no cwd at all.
        cwd = rec.get("cwd")
        if not cwd and rec.get("parent_id"):
            parent = st.get_session(rec["parent_id"]) or {}
            cwd = parent.get("cwd")
        where = Path(cwd).name if cwd else "-"
        tag = " (subagent)" if rec.get("provenance") == "subagent" else ""
        hits_note = f"  [{hit['chunk_hits']} hits]" if hit.get("chunk_hits", 1) > 1 else ""
        print(f"\n{sid[:8]}  {rec.get('source','?'):6} {when}  {where}{tag}"
              f"  ({rec.get('words',0):,}w){hits_note}")
        excerpt = " ".join((hit.get("excerpt") or "").split())
        if excerpt:
            print(f"    {excerpt}")
    st.close()
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """Reverse-chronological "what did I do last week" view.

    Deliberately a separate command from search rather than `search ""`: it
    answers a different question (what happened, in order) and the spec asks
    for its own flag surface (--repo, --since, --limit) rather than every
    search flag. See docs/specs/004-structured-filters.md, "muninn log".
    """
    st = store.open_store(args.db)
    filters = Filters(repo=args.repo, since=args.since)
    try:
        rows = st.log(limit=args.limit, filters=filters)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        st.close()
        return 2
    if args.json:
        print(json.dumps(rows))
        st.close()
        return 0
    if not rows:
        print("no sessions")
        st.close()
        return 0
    for row in rows:
        when = (row.get("started_at") or "")[:10] or "?"
        where = Path(row["cwd"]).name if row.get("cwd") else "-"
        tag = " (subagent)" if row.get("provenance") == "subagent" else ""
        topic = f"  {row['topic']}" if row.get("topic") else ""
        print(f"{when}  {row['session_id'][:8]}  {row.get('source','?'):6} "
              f"{where}{tag}  ({row.get('words',0):,}w){topic}")
    st.close()
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    st = store.open_store(args.db)
    rows = st.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        (args.session_id + "%",)).fetchall()
    if not rows:
        print(f"no session matching {args.session_id!r}")
        st.close()
        return 1
    if len(rows) > 1:
        print(f"{len(rows)} sessions match {args.session_id!r}:")
        for row in rows[:20]:
            print(f"  {row['session_id']}")
        st.close()
        return 1
    sid = rows[0]["session_id"]
    rec = st.get_session(sid) or {}
    print(f"# session: {sid}")
    for key in ("source", "provenance", "cwd", "branch", "model",
                "started_at", "ended_at", "words"):
        if rec.get(key) not in (None, ""):
            print(f"# {key}: {rec[key]}")
    if not rec.get("source_present"):
        print("# note: the original transcript no longer exists on disk")
    print()
    print(st.session_text(sid))
    st.close()
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    """Wire ``muninn-hook session-end`` into ``~/.claude/settings.json``.

    ``--check`` is read-only by construction: it calls ``install()`` with
    ``check_only=True``, which never reaches the atomic-write path — see
    hooks/install.py. This guarantee is exercised directly by
    tests/test_indexer.py rather than trusted by inspection alone.
    """
    result = hooks_install.install(check_only=args.check)
    if args.check:
        if result.already_installed:
            print(f"installed: {result.command}")
            print(f"           in {result.settings_path}")
        else:
            print("not installed")
            print(f"  would add: {result.command}")
            print(f"  to:        {result.settings_path}")
        return 0

    if result.already_installed and not result.changed:
        print(f"already installed: {result.command}")
    elif result.changed:
        print(f"installed: {result.command}")
        print(f"  wrote:   {result.settings_path}")
        if result.backup_path:
            print(f"  backup:  {result.backup_path}")
    else:
        print(f"no change needed: {result.command}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report archive health. Staleness must be visible, never silent."""
    st = store.open_store(args.db)
    print(f"muninn {__version__}")
    print(f"archive  {args.db}  {_size(args.db)}")
    print(f"         {st.count_sessions():,} sessions · {st.count_chunks():,} chunks\n")

    print("provenance")
    for row in st.conn.execute(
            "SELECT source, provenance, COUNT(*) n, SUM(words) w FROM sessions "
            "GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {row['source']:7} {row['provenance']:13} {row['n']:>6,} sessions "
              f"{row['w'] or 0:>10,} words")

    gone = st.conn.execute(
        "SELECT COUNT(*) n FROM sessions WHERE source_present = 0").fetchone()["n"]
    if gone:
        print(f"\nirreplaceable: {gone:,} sessions whose original transcript is gone")

    print("\nindex lag")
    for source, info in ingest.index_lag(st, default_roots()).items():
        pending = info["unindexed_or_grown_files"]
        state = "up to date" if not pending else f"{pending:,} file(s) not yet indexed"
        print(f"  {source:7} {state}")

    # Queue section (spec 003): the hook can only enqueue, so a wedged drain
    # or a pile of malformed jobs is invisible unless doctor surfaces it —
    # same "staleness must be visible" principle as index lag above.
    print("\nqueue")
    pending_jobs = queue.pending_count()
    bad_jobs = queue.bad_count()
    oldest_age = queue.oldest_pending_age_s()
    print(f"  pending  {pending_jobs:,} job(s)")
    if oldest_age is not None:
        print(f"  oldest   {oldest_age:,.0f}s old")
        if oldest_age > 300:
            print("  WARNING: oldest pending job exceeds 5 minutes — the drain may be wedged")
    if bad_jobs:
        print(f"  WARNING: {bad_jobs:,} malformed job(s) in {QUEUE_DIR / 'bad'}")

    last_sweep = st.last_sweep_at()
    print(f"  last sweep  {last_sweep or 'never'}")

    failures = st.conn.execute(
        "SELECT source, category, count FROM parse_failures ORDER BY count DESC").fetchall()
    if failures:
        print("\nparse failures (a rising rate suggests an upstream format change)")
        for row in failures:
            print(f"  {row['source']:7} {row['category']:24} {row['count']:,}")

    print("\nimport ledger (last 5)")
    tail = st.ledger_tail(5)
    if not tail:
        print("  (no imports yet)")
    for row in tail:
        status = row["outcome"] if row["finished_at"] else "IN PROGRESS"
        print(f"  #{row['ledger_id']:<4} {row['started_at']}  {row['actor']:10} "
              f"{row['source_kind']:18} {status}")

    # Invariant 9: a crashed import must be visible, never silently reaped.
    incomplete = st.incomplete_imports()
    if incomplete:
        ids = ", ".join(f"#{row['ledger_id']}" for row in incomplete)
        print(f"\nWARNING: {len(incomplete)} incomplete import(s) never finished: {ids}")
        print("         (started_at set, finished_at NULL — the process likely crashed)")

    # A lock held by a dead pid is stale evidence someone should know about,
    # even though the next importer is free to take it over.
    lock = st.conn.execute("SELECT * FROM import_lock WHERE id = 1").fetchone()
    if lock is not None:
        alive = store.pid_alive(lock["pid"])
        if not alive:
            print(f"\nWARNING: import lock held by actor {lock['actor']} (pid {lock['pid']}) "
                  f"since {lock['acquired_at']} — process is not running; lock is stale")
        else:
            print(f"\nimport lock held by actor {lock['actor']} (pid {lock['pid']}) "
                  f"since {lock['acquired_at']}")

    st.close()
    return 0


def _size(path: str | Path) -> str:
    try:
        n = Path(path).stat().st_size
    except OSError:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="muninn", description="Search and archive your AI agent history.")
    parser.add_argument("--version", action="version", version=f"muninn {__version__}")
    parser.add_argument("--db", default=str(DB_PATH), help="archive path")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="ingest transcripts into the archive")
    p_index.add_argument("--source", choices=("claude", "codex"))
    p_index.add_argument("--json", action="store_true",
                         help="print only the machine-readable import receipt(s)")
    p_index.add_argument("--watch", action="store_true",
                         help="run the background indexer: sweep, then drain the "
                              "queue and react to file changes indefinitely")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser(
        "search", help="search the archive",
        description="Search the archive. By default, every session is searched "
                    "EXCEPT tool-invoked ones (reproducible/bug-residue "
                    "sessions from other tools calling `claude -p`, not "
                    "conversations) — pass --provenance tool-invoked to "
                    "include them explicitly. Subagent transcripts ARE "
                    "searched by default; they hold real work.")
    p_import = sub.add_parser("import", help="import a claude.ai or ChatGPT export")
    p_import.add_argument("path", nargs="?",
                          help="export directory, conversations.json, or .zip "
                               "(default: newest under ~/Downloads)")
    p_import.add_argument("--json", action="store_true",
                          help="print only the machine-readable import receipt")
    p_import.add_argument("--actor", default="cli")
    p_import.add_argument("--verify-safe-to-delete", action="store_true",
                          help="answer, from the ledger only, whether this source "
                               "is fully recorded and safe to delete")
    p_import.set_defaults(func=cmd_import)

    p_install = sub.add_parser(
        "install-hooks", help="wire the SessionEnd hook into ~/.claude/settings.json")
    p_install.add_argument("--check", action="store_true",
                           help="report status only; never write settings.json")
    p_install.set_defaults(func=cmd_install_hooks)

    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true",
                          help="print a JSON array of result objects instead of text")
    _add_filter_args(p_search)
    p_search.set_defaults(func=cmd_search)

    p_show = sub.add_parser("show", help="print one session (id prefixes are fine)")
    p_show.add_argument("session_id")
    p_show.set_defaults(func=cmd_show)

    p_doctor = sub.add_parser("doctor", help="report archive health and index lag")
    p_doctor.set_defaults(func=cmd_doctor)

    p_log = sub.add_parser(
        "log", help='reverse-chronological "what did I do last week" view')
    p_log.add_argument("--repo")
    p_log.add_argument("--since", help="ISO date/prefix: 2026, 2026-07, or 2026-07-31")
    p_log.add_argument("--limit", type=int, default=20)
    p_log.add_argument("--json", action="store_true")
    p_log.set_defaults(func=cmd_log)
    return parser


def _add_filter_args(p: argparse.ArgumentParser) -> None:
    """Filters shared across search-shaped subcommands. See docs/specs/004."""
    p.add_argument("--repo", help="substring match on the basename of the session's cwd")
    p.add_argument("--branch", help="exact match on git branch")
    p.add_argument("--file", help="a file touched in the session (basename or path suffix)")
    p.add_argument("--tool", help="a tool the session used (e.g. Read, Edit, exec)")
    p.add_argument("--model", help="substring match on model name")
    p.add_argument("--provenance", choices=("human", "tool-invoked", "subagent"),
                   help="default: everything except tool-invoked")
    p.add_argument("--source", help="claude, codex, claude-cloud, ...")
    p.add_argument("--since", help="ISO date/prefix: 2026, 2026-07, or 2026-07-31")
    p.add_argument("--until", help="ISO date/prefix, inclusive")
    p.add_argument("--outcome", choices=("fixed", "abandoned", "ongoing"),
                   help="wired for spec 005 enrichment; matches nothing until it lands")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
