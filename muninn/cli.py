"""Command line interface.

The CLI is Muninn's public boundary. Agents and humans both go through it; it is
the only supported way to ask the archive questions, so that internals stay free
to change. (Huginn's skill takes the same stance, and it is a good one.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, exports, ingest, store
from .receipt import Outcome

# Muninn's own state. Config/state split follows platform convention.
if sys.platform == "win32":
    STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Muninn"
else:
    STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "muninn"

DB_PATH = STATE_DIR / "muninn.db"


def default_roots() -> dict[str, Path]:
    """Where transcripts live, per source. ``$CODEX_HOME`` is honored."""
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return {
        "claude": Path.home() / ".claude" / "projects",
        "codex": codex_home / "sessions",
    }


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
    receipts = []
    for source, root in roots.items():
        if not root.is_dir():
            if not args.json:
                print(f"{source:7} no transcripts at {root}")
            continue
        result = ingest.ingest_path(st, root, source)
        if result.receipt is not None:
            receipts.append(result.receipt)
        if not args.json:
            _print_index_result(source, result)
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
    hits = st.search(args.query, limit=args.limit)
    if not hits:
        print("no matches")
        st.close()
        return 1
    seen: set[str] = set()
    for hit in hits:
        sid = hit["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
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
        print(f"\n{sid[:8]}  {rec.get('source','?'):6} {when}  {where}{tag}"
              f"  ({rec.get('words',0):,}w)")
        excerpt = " ".join((hit.get("excerpt") or "").split())
        if excerpt:
            print(f"    {excerpt}")
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
    p_index.set_defaults(func=cmd_index)

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

    p_search = sub.add_parser("search", help="search the archive")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.set_defaults(func=cmd_search)

    p_show = sub.add_parser("show", help="print one session (id prefixes are fine)")
    p_show.add_argument("session_id")
    p_show.set_defaults(func=cmd_show)

    p_doctor = sub.add_parser("doctor", help="report archive health and index lag")
    p_doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
