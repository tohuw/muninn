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

from . import __version__, agent_install, daemon, exports, ingest, queue, raven, store
from .hooks import install as hooks_install
from .paths import DB_PATH, QUEUE_DIR, STATE_DIR, default_roots
from .plugins import discover_plugins
from .policy import resolve as resolve_policies
from .policy import shadowed_distribution_names as shadowed_policy_distributions
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
    roots = _roots_for(args)

    if args.watch:
        # The FOREGROUND ingest loop, and only that. Since spec 010 the raven
        # descriptor, /api/menu and the state file belong to `muninn serve`;
        # this path deliberately publishes none of them, so someone can watch
        # ingest happen on a console without a service also being installed,
        # and so two processes never race to own one descriptor path.
        #
        # It still takes the single-instance lock, because the failure to
        # prevent is two loops draining one queue and sweeping one archive —
        # and that failure does not care which command started them. Naming
        # the mistake: locking only in `serve` would make `index --watch` look
        # harmless while it silently doubles every import.
        #
        # The store is opened by the daemon rather than here, so the lock is
        # taken before anything touches the archive.
        return _run_ingest_loop(args, roots, menubar=False, holder=daemon.HOLDER_WATCH)

    st = store.open_store(args.db)

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


def _roots_for(args: argparse.Namespace) -> dict[str, Path]:
    """Configured transcript roots, narrowed by ``--source`` if given."""
    roots = default_roots()
    source = getattr(args, "source", None)
    if source:
        roots = {k: v for k, v in roots.items() if k == source}
    return roots


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the daemon: continuous ingest, the raven surface, and a state file.

    See docs/specs/010-daemon.md. Named ``serve`` rather than ``daemon`` to match
    Huginn's verb for the same thing — a user who runs both ravens learns one
    word — and because it is what the process *does* rather than what it is.

    This function is deliberately thin. Everything about the lifecycle lives in
    muninn/daemon.py, so that "what the daemon owns" is answerable by reading one
    module rather than by reading a CLI handler and inferring.
    """
    return _run_ingest_loop(args, _roots_for(args),
                            menubar=not args.no_menubar, holder=daemon.HOLDER_SERVE)


def _announce(message: str) -> None:
    """Print one daemon line and flush it.

    The flush is the whole reason this is not just ``print``. Python
    block-buffers stdout when it is not a tty, which is exactly what a service
    manager gives a daemon — so every line here (the port it bound, each import
    receipt) sat in a 8 KiB buffer and reached the log only when the process
    exited. Verified before the fix: a live `muninn serve` redirected to a file
    left the file empty for its entire run, and the startup lines all appeared at
    once on shutdown. A daemon whose log is empty while it works is
    indistinguishable from a daemon that is not working, which is the specific
    invisibility this project keeps re-learning about staleness.
    """
    print(message, flush=True)


def _run_ingest_loop(args: argparse.Namespace, roots: dict[str, Path], *,
                     menubar: bool, holder: str) -> int:
    """Shared body of `serve` and `index --watch`. One loop, two front doors.

    The difference between them is entirely the ``menubar``/``holder`` pair, and
    keeping it that narrow is the point: a future change to shutdown ordering or
    to the lock cannot apply to one command and not the other, which is exactly
    how `index --watch` came to be the thing publishing a descriptor in the first
    place (spec 009's "The lifecycle question").
    """
    service = daemon.Daemon(
        args.db, roots,
        menubar=menubar,
        # Only `serve` publishes a state file. A foreground watcher expects no
        # supervisor and advertises no port, so a state file would be a claim
        # that something can manage it — and `doctor` would report a daemon that
        # no service manager knows about.
        publish_state=(holder == daemon.HOLDER_SERVE),
        holder=holder,
        announce=_announce,
    )
    try:
        return service.run()
    except daemon.AlreadyRunning as exc:
        print(f"muninn: {exc}", file=sys.stderr)
        print("        stop it first, or run `muninn doctor` to see what is holding the lock.",
              file=sys.stderr)
        return daemon.EXIT_ALREADY_RUNNING


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


def cmd_install_agent(args: argparse.Namespace) -> int:
    """Install a login agent that runs `muninn serve` at every login.

    Thin on purpose, exactly like ``cmd_serve``: the mechanism is
    ``corvidae.login_agent`` and the Muninn-specific parts are
    ``muninn/agent_install.py``, so "what gets installed where" is answerable by
    reading one module rather than by reading a CLI handler and inferring.

    The exit code is the contract and the printed wording is not — corvidae says
    so explicitly about its own backends' output, which may change within a CalVer
    year. 0 installed, 1 refused or the OS mechanism failed, 2 no mechanism here.
    """
    return agent_install.install()


def cmd_uninstall_agent(args: argparse.Namespace) -> int:
    """Remove the login agent. Same exit-code discipline as install-agent."""
    return agent_install.uninstall()


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

    _print_plugins_section()
    _print_policy_section()
    _print_daemon_section()
    _print_menubar_section()

    st.close()
    return 0


def _print_daemon_section() -> None:
    """Spec 010: is the daemon running, on what port, and what holds the lock.

    Three separate facts, deliberately not collapsed into one verdict:

    - **The lock** answers "is an ingest loop running at all", and it answers it
      for `index --watch` too, which writes no state file. It is the only one of
      the three the kernel maintains, so it survives a SIGKILL that leaves the
      other two stale.
    - **The state file** answers "can a supervisor find it", and names the port.
    - The descriptor (next section) answers "is Muninn in the menubar".

    Naming the mistake a reader might make: a stale state file plus a free lock
    is not "running". It is a crashed daemon, and reporting it as running is the
    invisible-staleness failure this project has already been bitten by — so the
    pid is cross-checked against the OS rather than trusted because a file says
    so.
    """
    print("\ndaemon (`muninn serve`)")
    _print_login_agent_line()

    held, lock_pid, holder = daemon.SingleInstance.probe()
    lock_file = daemon.lock_path()
    if held is None:
        # Unknown, not free. An unenforced guard reported as "nothing running"
        # is worse than no line at all.
        print(f"  lock        {lock_file}")
        print("              UNKNOWN — no file-locking primitive here, or the lock "
              "file cannot be opened")
    elif held:
        where = f"pid {lock_pid}" if lock_pid is not None else "an unrecorded pid"
        print(f"  lock        held by {where} ({holder})")
    else:
        print("  lock        free — no ingest loop is running")

    state = daemon.read_state()
    state_file = daemon.state_path()
    if state is None:
        if state_file.exists():
            print(f"  state       {state_file}")
            print("              present but unreadable — treat the daemon as not running")
        else:
            print("  state       absent — start it with `muninn serve`")
        return

    # Every value below is rendered through a narrowing step rather than
    # interpolated as it was read. The file is Muninn's own 0600 one, so this is
    # not a trust boundary so much as a refusal to build one: `doctor`'s output is
    # what an agent relays to a human (CLAUDE.md, "The agent-facing contract"), and
    # a field printed verbatim makes whatever wrote that file an author of it. A
    # `db` path carrying an ANSI escape could rewrite the line above it.
    pid = state.get("pid") if isinstance(state.get("pid"), int) else None
    port = state.get("port") if isinstance(state.get("port"), int) else None
    if pid is None or not store.pid_alive(pid):
        print(f"  state       {state_file}")
        print(f"  WARNING: state file names pid {pid if pid is not None else '(unrecorded)'}, "
              f"which is not running — the daemon crashed; the file is stale")
        return
    print(f"  running     pid {pid} · since {_epoch_to_iso(state.get('started'))}")
    # "no menu port" is a real state, not an error: ravenserve.attach() returns
    # None rather than costing the daemon its ingest (spec 009 #9), so a daemon
    # with no port is still doing the job that matters.
    print(f"  menu port   {port if port is not None else 'none (the raven did not bind; see below)'}")
    print(f"  archive     {raven.safe_label(state.get('db'), 200) or '(unrecorded)'}")


def _print_login_agent_line() -> None:
    """Whether a login agent will start `muninn serve` at the next login.

    Part of the daemon section rather than a section of its own, because it
    answers a fourth question about the same subject — "will this come back by
    itself" — and a reader who has to correlate two sections to learn that the
    daemon is running *but* nothing will restart it has been given a puzzle
    instead of a report.

    Printed **first, and before any of the early returns below.** That position is
    load-bearing: a crashed daemon leaves a stale state file, which is exactly the
    case that returns early — and it is also exactly the case where "is anything
    going to restart it" is the most useful line on screen. Ordering it after the
    lock would have made the answer disappear at the moment it matters most.

    "not installed" is a completely normal state, not a warning: an external
    supervisor the user configured by hand, or a foreground `muninn serve` in a
    terminal, are both legitimate and neither leaves a plist behind. So this
    reports and does not advise, except to name the verb.
    """
    agent = agent_install.get_login_agent()
    if agent is None:
        # Not a failure. corvidae returns None for a platform it has no
        # mechanism for, and saying so is better than omitting the line and
        # letting the reader assume "not installed".
        print(f"  at login    no start-at-login mechanism on {sys.platform}")
        return
    if agent.installed():
        # The path, not just the verdict — a `doctor` that says "installed" while
        # the file lives somewhere the reader is not looking (a redirected
        # $XDG_CONFIG_HOME, say) is the same invisible-mismatch failure the
        # descriptor line exists to prevent.
        print(f"  at login    installed · {agent.label} · {agent_install.config_location(agent)}")
    else:
        print(f"  at login    not installed — `muninn install-agent` adds a {agent.label}")


def _epoch_to_iso(value: object) -> str:
    """Render the state file's epoch ``started`` for a human, or say it is unusable.

    The field is epoch seconds to match Huginn's ``daemon.json`` and Muninn's own
    raven descriptor (see daemon.write_state); rendering it is this layer's job.
    A value that will not convert is reported as such rather than printed raw —
    "started 1.7e+09" in a health report is worse than an admission.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "an unrecorded time"
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return "an unusable timestamp"


def _print_menubar_section() -> None:
    """Whether Muninn is currently discoverable in the shared menubar (spec 009).

    Worth a line even though the answer may be "no": a user who cannot find
    Muninn in the menubar has no other way to tell "the daemon is not running"
    from "the descriptor went somewhere the host does not look", and those need
    completely different fixes. The path is printed for exactly that reason —
    it is the shared ravens directory, not Muninn's own state dir, and confusing
    the two is the mistake this line makes visible.

    Since spec 010 the publisher is `muninn serve`, not `index --watch`. That
    changed the heading and nothing else: the descriptor's contents, location and
    liveness rules are unchanged, which is why the host needed no coordination.
    """
    path = raven.descriptor_path()
    print("\nshared menubar (published while `muninn serve` runs)")
    print(f"  descriptor  {path}")
    if not path.exists():
        print("              absent — Muninn is not in the menubar right now")
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid, port = payload.get("pid"), payload.get("port")
    except (OSError, ValueError):
        print("              present but unreadable — the host will report it as malformed")
        return
    alive = store.pid_alive(pid if isinstance(pid, int) else None)
    state = "serving" if alive else "STALE (its process is gone; the host will say so)"
    print(f"              {state} · pid {pid} · port {port}")


def _print_plugins_section() -> None:
    """Spec 008: a mismatched/broken plugin must be a loud line in doctor, never a silent skip.

    discover_plugins() is @lru_cache'd (see muninn/plugins.py) so a change to
    installed plugins is only picked up on the next process start — stated
    here rather than left to be discovered by surprise when a freshly
    installed plugin doesn't appear until the CLI is re-invoked (it always
    is, since each `muninn` invocation is a fresh process — but a long-lived
    `muninn index --watch` would need a restart).
    """
    result = discover_plugins()
    print("\nplugins (discovered once per process; restart to pick up changes)")
    if not result.specs and not result.errors:
        print("  (none installed)")
    for spec in result.specs:
        caps = []
        if spec.embedders:
            caps.append(f"{len(spec.embedders)} embedder(s)")
        if spec.text_providers:
            caps.append(f"{len(spec.text_providers)} text provider(s)")
        if spec.history_sources:
            caps.append(f"{len(spec.history_sources)} history source(s)")
        cap_str = ", ".join(caps) if caps else "no capabilities"
        print(f"  {spec.name:16} v{spec.version:10} api [{spec.min_api},{spec.max_api}]  {cap_str}")
    for err in result.errors:
        # Only error.error_class is a class name — never render err.detail
        # verbatim here even though this module's own validation errors are
        # safe; a third-party entry point's exception message is not, and
        # there must be exactly one code path for "what gets shown."
        print(f"  WARNING: {err.entry_point!r} failed to load — {err.error_class}")


def _print_policy_section() -> None:
    """Spec 008: which model policies are active, so a restricted build is visibly restricted."""
    policies = resolve_policies()
    print("\nmodel policy (every LLM/embedding call routes through these; they intersect)")
    for policy in policies:
        provider = policy.require_provider or "any provider"
        allow = ", ".join(policy.allow) if policy.allow else "(none — refuses everything)"
        print(f"  {policy.name:16} provider={provider:14} allow=[{allow}]")
        if policy.reason:
            print(f"                   reason: {policy.reason}")

    # Two distributions claiming one normalised name is the signal that
    # something is shadowing the policy distribution — the fail-open documented
    # in muninn/policy.py::_policy_entry_points and in
    # .valholl/articles/model-policy-chokepoint.md, "Discovery is the attack
    # surface." The policies still bind (that is the fix), but a restricted
    # build should have exactly one distribution per name, so the duplicate is
    # reported rather than tolerated quietly. Only names are printed, never a
    # filesystem path: a path can leak a home directory or an internal
    # distribution's layout, per the rule plugins load errors already follow.
    for name in shadowed_policy_distributions():
        print(f"  WARNING: {name!r} contributes a model policy from more than one installed "
              f"distribution — a duplicate or shadowed install; verify which one is authoritative")


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

    p_serve = sub.add_parser(
        "serve", help="run the daemon: continuous ingest plus the menubar raven",
        description="Run Muninn as a service. Sweeps on startup, then drains the "
                    "SessionEnd queue and reacts to transcript changes forever; "
                    "publishes the raven descriptor and serves /api/menu on "
                    "loopback; writes a state file a supervisor can read; and "
                    "tears all three down on SIGTERM, SIGHUP or Ctrl-C. "
                    "See docs/specs/010-daemon.md.")
    p_serve.add_argument("--source", choices=("claude", "codex"),
                         help="ingest only this source (default: every configured root)")
    p_serve.add_argument("--no-menubar", action="store_true",
                         help="do not publish a raven descriptor or serve /api/menu; "
                              "ingest only (docs/specs/009)")
    p_serve.set_defaults(func=cmd_serve)

    p_index = sub.add_parser("index", help="ingest transcripts into the archive")
    p_index.add_argument("--source", choices=("claude", "codex"))
    p_index.add_argument("--json", action="store_true",
                         help="print only the machine-readable import receipt(s)")
    p_index.add_argument("--watch", action="store_true",
                         help="run the ingest loop in the foreground: sweep, then "
                              "drain the queue and react to file changes "
                              "indefinitely. Publishes nothing — use `muninn "
                              "serve` for the menubar raven and a state file")
    # Accepted and inert, deliberately rather than removed. Since spec 010
    # `index --watch` publishes no descriptor at all, so this flag's request is
    # already satisfied — and an existing launchd plist or shell alias that
    # passes it would otherwise start failing on an unrecognised argument, which
    # is a worse outcome than a flag that says it has nothing left to do.
    p_index.add_argument("--no-menubar", action="store_true",
                         help=argparse.SUPPRESS)
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

    # Named to match Huginn's verbs exactly, for the same reason `serve` is:
    # someone running both ravens should learn one word. See
    # docs/specs/010-daemon.md, "Follow-up seam".
    p_install_agent = sub.add_parser(
        "install-agent", help="start `muninn serve` at every login (launchd/systemd/Run key)",
        description="Install a login agent that runs `muninn serve` at login, so "
                    "the archive keeps ingesting and Muninn stays in the shared "
                    "menubar without anyone remembering to start it. macOS gets a "
                    "LaunchAgent (which also restarts it if it dies), Linux a "
                    "systemd user unit (restart on failure only, so `systemctl "
                    "--user stop muninn` stays effective), Windows an HKCU Run "
                    "entry (start only; it is not a supervisor). Refuses while "
                    "another ingest loop holds the single-instance lock, because "
                    "supervising a process that exits immediately is a restart "
                    "loop rather than a service.")
    p_install_agent.set_defaults(func=cmd_install_agent)

    p_uninstall_agent = sub.add_parser(
        "uninstall-agent", help="remove the login agent installed by install-agent")
    p_uninstall_agent.set_defaults(func=cmd_uninstall_agent)

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
