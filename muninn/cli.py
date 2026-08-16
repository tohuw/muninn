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

from . import (
    __version__,
    agent_install,
    cost,
    daemon,
    embed,
    enrich,
    exports,
    fuse,
    ingest,
    prose_index,
    providers,
    queue,
    raven,
    recall,
    rerank,
    resume,
    store,
    survey,
)
from .hooks import install as hooks_install
from .paths import DB_PATH, QUEUE_DIR, STATE_DIR, default_roots
from .plugins import discover_plugins
from .policy import PolicyRefused
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


def _announce_err(message: str) -> None:
    """Progress for a run whose stdout is a machine-readable receipt.

    Same flush discipline as :func:`_announce` and for the same reason — a
    redirected long run that block-buffers looks hung.
    """
    print(message, file=sys.stderr, flush=True)


def _run_ingest_loop(args: argparse.Namespace, roots: dict[str, Path], *,
                     menubar: bool, holder: str) -> int:
    """Shared body of `serve` and `index --watch`. One loop, two front doors.

    The difference between them is entirely the ``menubar``/``holder`` pair plus
    the two things derived from ``holder`` below, and keeping it that narrow is
    the point: a future change to shutdown ordering or to the lock cannot apply
    to one command and not the other, which is exactly how `index --watch` came
    to be the thing publishing a descriptor in the first place (spec 009's "The
    lifecycle question").
    """
    while True:
        try:
            code = _run_ingest_loop_once(args, roots, menubar=menubar, holder=holder)
        except KeyboardInterrupt:
            # Ctrl-C outside the window ``Daemon.run`` covers. SIGINT is
            # deliberately not claimed by install_termination_handlers, so it
            # arrives as a KeyboardInterrupt wherever the main thread happens to
            # be -- and ``run``'s own try/except only spans the ingest loop. It
            # can therefore land here: between a restart's teardown and the next
            # iteration, or after run() has already returned.
            #
            # Escaping to the top level is not a harmless difference in tidiness.
            # CPython restores SIG_DFL and re-raises SIGINT so the process
            # *reports itself killed by the signal* -- exit -2, which a service
            # manager reads as a crash rather than a stop. The teardown has
            # already run either way, because it lives in a ``finally``; only
            # the exit status was wrong. Ctrl-C on a daemon is a stop, and a stop
            # is 0, exactly as SIGTERM gives.
            return 0
        if code is not _RESTART:
            return code
        # The teardown in Daemon.run has already withdrawn the descriptor, stopped
        # the embedder, removed the state file and released the lock — in that
        # order — so the next iteration acquires the lock and republishes cleanly.
        # A fresh Daemon rather than a reused one: a restart has to look like a
        # restart to everything watching, which means a new port, a new state file
        # and a worker that starts from the current backlog rather than a stalled
        # counter (embedder.py's STALL_LIMIT is per-instance by design).
        _announce("muninn: restarting")


#: Sentinel for "the menu asked for a restart". Not an exit code, because it must
#: never be able to reach a shell as one — a supervisor reading it would treat a
#: restart as a failure and start racing us.
_RESTART = object()


def _run_ingest_loop_once(args: argparse.Namespace, roots: dict[str, Path], *,
                          menubar: bool, holder: str) -> int | object:
    """One run of the ingest loop. Returns an exit code, or ``_RESTART``."""
    service = daemon.Daemon(
        args.db, roots,
        menubar=menubar,
        # Automatic embedding is the service's job, not the debug watcher's
        # (spec 014), and `--no-embed` is how someone who has a provider
        # installed declines to spend it. ``getattr`` because `index --watch`
        # does not define the flag at all: unlike `--no-menubar`, which is
        # accepted and inert there for the sake of plists written before spec
        # 010, `--no-embed` is new, so no existing invocation can be passing it
        # and there is nothing to stay compatible with.
        embed=(holder == daemon.HOLDER_SERVE and not getattr(args, "no_embed", False)),
        # Same ``getattr`` reasoning as ``embed`` above, and the same
        # service-only rule: `index --watch` is a debug ingest loop and must not
        # start spending model calls because someone wanted to watch a sweep.
        enrich=(holder == daemon.HOLDER_SERVE and not getattr(args, "no_enrich", False)),
        # Unlike the two above this is not a spending switch, so it is not
        # limited to `serve`: `index --watch` benefits from an accurate gate
        # too, and keeping it current costs a few aggregate queries an hour.
        recalibrate=not getattr(args, "no_recalibrate", False),
        enrich_metered=getattr(args, "enrich_metered", False),
        # Only `serve` publishes a state file. A foreground watcher expects no
        # supervisor and advertises no port, so a state file would be a claim
        # that something can manage it — and `doctor` would report a daemon that
        # no service manager knows about.
        publish_state=(holder == daemon.HOLDER_SERVE),
        holder=holder,
        announce=_announce,
    )
    try:
        code = service.run()
        return _RESTART if service.restart_requested else code
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


def cmd_enrich(args: argparse.Namespace) -> int:
    """Extract queryable facets from substantive sessions (spec 005).

    ``--dry-run`` is not a courtesy: this is the one expensive operation in the
    tool — a full corpus pass is thousands of model calls — so "what would this
    cost" has to be answerable without spending it.

    An un-surveyed archive is refused rather than defaulted. The gate exists
    precisely because it was derived from this corpus, and quietly substituting
    a constant would reintroduce the hard-coded threshold spec 011 removed.
    """
    shard = None
    if args.shard:
        try:
            k, n = (int(part) for part in args.shard.split("/", 1))
        except ValueError:
            print("muninn: --shard takes K/N, e.g. --shard 0/4", file=sys.stderr)
            return 2
        if not (n > 0 and 0 <= k < n):
            print(f"muninn: --shard {args.shard} is out of range — need 0 <= K < N",
                  file=sys.stderr)
            return 2
        shard = (k, n)

    st = store.open_store(args.db)
    calibration = enrich.load_calibration(args.db)
    plan = enrich.plan(st, calibration, session_id=args.session_id,
                       source=args.source, limit=args.limit, force=args.force,
                       shard=shard)

    if not plan.calibrated:
        # No longer fatal. Selection is a structural floor now, not a gate
        # derived from this corpus, so an un-surveyed archive can be enriched —
        # it just has no measured shape to report alongside the result.
        print("note: no calibration.json beside this archive, so no corpus "
              "statistics accompany this plan.", file=sys.stderr)
        print(f"  `muninn survey` writes one "
              f"({survey.calibration_path(args.db)})", file=sys.stderr)

    # `--dry-run` is what plans; `--json` only chooses the *shape* of the output.
    #
    # These were one condition until spec 015, and `--json` therefore planned
    # instead of enriching. On every other command `--json` means "the machine
    # readable form of what this command does", so enrich was the anomaly — and
    # the cost of the anomaly fell on exactly the caller who cannot see it: an
    # agent asking for receipts got a plan, believed the work was done, and
    # reported facets that were never written. `--dry-run --json` is still the
    # planning form, and is what a caller that wants an estimate should use.
    if args.dry_run:
        st.close()
        payload = {
            "planned": len(plan.candidates),
            "estimated_calls": plan.estimated_calls,
            "thresholds": plan.thresholds,
            "skipped": plan.skipped,
            "sessions": [c.session_id for c in plan.candidates],
        }
        if args.json:
            print(json.dumps(payload))
        else:
            _print_enrich_plan(plan)
        return 0

    if not plan.candidates:
        st.close()
        if args.json:
            # A receipt for "nothing to do" rather than a human plan, so a caller
            # parsing receipts never has to also parse the plan format.
            print(json.dumps({"enriched": 0, "failed": 0, "sessions": [],
                              "redactions": {}, "failures": {},
                              "skipped": plan.skipped, "model": None,
                              "provider": None}))
        else:
            _print_enrich_plan(plan)
        return 0

    try:
        provider = providers.resolve_provider(args.model, args.provider)
    except providers.ProviderError as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2
    reason = provider.available()
    if reason is not None:
        st.close()
        print(f"muninn: provider unavailable — {reason}", file=sys.stderr)
        return 2

    # flush=True throughout, and it is not cosmetic. Python block-buffers stdout
    # when it is not a tty, which is exactly what a redirected long run is — so
    # the first real enrichment pass wrote an empty log for its entire life and
    # looked hung. muninn/daemon.py records the same lesson for `serve`; this is
    # the second place it applies, and both are long-running.
    # Progress goes to stderr under --json so stdout stays a single parseable
    # object. Silencing it entirely would be worse: a corpus pass runs for hours,
    # and the daemon's log lesson (muninn/daemon.py) is that a long run with no
    # output is indistinguishable from a hung one.
    progress = _announce_err if args.json else _announce
    progress(f"enriching {len(plan.candidates):,} session(s) with "
             f"{getattr(provider, 'model', '?')} "
             f"(~{plan.estimated_calls:,} model calls)")
    try:
        result = enrich.enrich_sessions(st, plan.candidates, provider,
                                        progress=progress)
    except PolicyRefused as exc:
        # A refused model is a statement about the run's configuration, not
        # about one session — retrying it per session would produce thousands of
        # identical refusals.
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2
    st.close()

    if args.json:
        # The receipt. Same facts the human output carries, plus the model and
        # provider that actually ran — which a chain provider decides at call
        # time, so a caller cannot infer it from the flags it passed.
        print(json.dumps({
            "enriched": result.enriched,
            "failed": result.failed,
            "sessions": [c.session_id for c in plan.candidates],
            "redactions": result.redactions,
            "failures": result.failures,
            "skipped": plan.skipped,
            "model": getattr(provider, "model", None),
            "provider": getattr(provider, "name", None),
        }))
        return 0

    _announce(f"enriched {result.enriched:,} · failed {result.failed:,}")
    if result.redactions:
        # Reported by kind and count, never by value: the point of naming them
        # is that the user learns their transcripts contain credentials.
        named = ", ".join(f"{k} ×{v}" for k, v in sorted(result.redactions.items()))
        print(f"redacted before sending: {named}")
    for category, count in sorted(result.failures.items()):
        print(f"  {category:20} {count:,}")
    return 0


def _print_enrich_plan(plan) -> None:
    print(f"planned  {len(plan.candidates):,} session(s) · "
          f"~{plan.estimated_calls:,} model calls")
    if plan.thresholds:
        gates = ", ".join(f"{s} >= {t:,}w" for s, t in sorted(plan.thresholds.items()))
        print(f"gate     {gates}  (derived; `muninn survey` re-derives)")
    if plan.skipped:
        print("skipped")
        for reason, count in sorted(plan.skipped.items()):
            print(f"  {reason:22} {count:,}")


def cmd_backfill(args: argparse.Namespace) -> int:
    """Ingest a claudex/codexdex prose index (tohuw/muninn#6).

    A separate verb from `import` on purpose. `import` takes a vendor export —
    something you downloaded, which the vendor still has. This takes a
    predecessor's *archive*, which for much of its span is the only surviving
    copy of the transcripts it covers, and it is a one-time migration rather
    than a recurring operation. Naming it separately keeps "I am moving the old
    tool's data in" from reading like "I am re-importing a download".

    With no path, the known predecessor locations are used — but only those that
    actually hold prose files, so an empty `~/.codexdex` (a real state: codexdex
    was never run on the development machine before Muninn existed) is reported
    as "not found" rather than as a source with nothing in it.
    """
    if args.path:
        candidates = [prose_index.ProseIndexCandidate(
            Path(p).expanduser(), args.source or "claude") for p in args.path]
    else:
        candidates = prose_index.find_prose_indexes()
        if not candidates:
            print("no prose index found — looked for "
                  + ", ".join(f"~/{name}" for name, _ in prose_index.KNOWN_ROOTS))
            return 1

    st = store.open_store(args.db)
    receipts = []
    for candidate in candidates:
        files = prose_index.discover(candidate.path)
        if not files:
            if not args.json:
                print(f"{candidate.path}: no prose files under index/ or cloud/index/")
            continue
        result = prose_index.import_prose_index(
            st, candidate.path, default_source=candidate.default_source, actor=args.actor)
        receipt = result.receipt
        receipts.append(receipt)
        if not args.json:
            print(f"\n{candidate.path}")
            _print_export_result(receipt)
            harvested = result.facets_harvested
            if harvested:
                print(f"facets   {harvested:,} summaries harvested from the predecessor "
                      f"(that many enrichment calls not needed)")
    st.close()

    if args.json:
        print(json.dumps([r.to_dict() for r in receipts]))
    elif receipts:
        # The reason this command exists, said plainly. Someone runs it in order
        # to decide whether the predecessors can be retired, and that decision
        # needs the archive-of-record framing rather than a count.
        print("\nThese sessions are recorded as having no surviving raw transcript. "
              "Keep the predecessor indexes until you have verified the archive holds "
              "what they did.")
    return 0


def cmd_survey(args: argparse.Namespace) -> int:
    """Measure the corpus and derive thresholds from it.

    See muninn/survey.py and .valholl/articles/derived-calibration.md. The one
    thing worth knowing at this layer: an empty archive is a success, not an
    error. Surveying before ingesting is a normal order of operations, and the
    answer — "nothing to derive from yet" — is a fact, not a failure.

    ``--dry-run`` prints without writing, because a calibration is a file
    everything downstream reads: someone should be able to see what a re-survey
    would change before it changes it.
    """
    st = store.open_store(args.db)
    doc = survey.survey(st, db=args.db, roots=_roots_for(args))
    st.close()

    path = Path(args.out) if args.out else survey.calibration_path(args.db)
    if not args.dry_run:
        survey.write_calibration(doc, path)

    if args.json:
        print(json.dumps(doc))
        return 0

    _print_survey(doc, path, wrote=not args.dry_run)
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """What the archive knows about where you are working (recall.py).

    The one retrieval path that takes a *place* rather than a question. Every
    other one waits to be asked, which is the wrong shape for the material a
    person has forgotten they have: you do not search for it, because you do
    not know it is there.
    """
    st = store.open_store(args.db)
    model = None
    try:
        model = embed.resolve_provider(args.provider).model
    except embed.EmbeddingUnavailable:
        # Not fatal, and not worth a warning here: two of the three sections
        # need no embeddings at all, and the third reports its own absence.
        pass
    try:
        found = recall.recall(st, repo=args.repo, limit=args.limit, model=model)
    finally:
        st.close()

    if args.json:
        print(json.dumps(found.to_dict()))
        return 0

    where = found.repo or "this machine"
    if not found:
        print(f"nothing recalled for {where}")
        for reason in found.unavailable.values():
            print(f"  {reason}")
        return 0

    print(f"what the archive knows about {where}\n")
    for heading, entries in (
        ("unfinished — started and not finished", found.unfinished),
        ("prior work here", found.prior),
        ("related, from other repositories", found.related),
    ):
        if not entries:
            continue
        print(heading)
        for item in entries:
            score = f"  {item.score:.2f}" if item.score is not None else ""
            label = item.topic or _first_line(item.summary) or item.cwd or item.source
            print(f"  {item.session_id[:8]}  {str(item.started_at)[:10]}{score}  "
                  f"{str(label)[:66]}")
            if item.outcome:
                print(f"            outcome: {item.outcome}")
        print()
    for reason in found.unavailable.values():
        print(f"note: {reason}")
    return 0


def _first_line(text: str | None) -> str | None:
    if not text:
        return None
    lines = str(text).splitlines()
    return lines[0].strip() if lines else None


def _print_survey(doc: dict, path: Path, *, wrote: bool) -> None:
    """The human-readable half. Coverage first, threshold second.

    Deliberate ordering: the gate is *derived to hit a coverage target*, and a
    reader shown "4,046 words" first will read it as the number that matters and
    compare it against someone else's. The coverage is the intent; the word count
    is what this corpus happened to need to reach it.
    """
    archive = doc["archive"]
    print(f"archive  {archive['path']}")
    print(f"         {archive['sessions']:,} sessions · {archive['chunks']:,} chunks")

    if not doc["sources"]:
        print("\nno sessions yet — nothing to derive. Run `muninn index` first.")
    for source, report in sorted(doc["sources"].items()):
        gate = report["enrichment_gate"]
        print(f"\n[{source}] {report['conversations']:,} conversations · "
              f"{report['conversation_words']:,} words")
        for provenance in ("human", "subagent", "tool-invoked"):
            cls = report["provenance"][provenance]
            note = "  (excluded from every statistic)" if provenance == "tool-invoked" else ""
            print(f"  {provenance:13} {cls['sessions']:>6,} sessions "
                  f"{cls['words']:>10,} words{note}")
        if gate["sessions"]:
            print(f"  enrich gate  >= {gate['threshold_words']:,} words -> "
                  f"{gate['sessions']:,} sessions "
                  f"({gate['share_of_conversations_pct']:.1f}% of conversations, "
                  f"{gate['coverage_pct']:.1f}% of text)")
        else:
            print("  enrich gate  none derived (no conversation text yet)")

    _print_cost(doc.get("cost"))

    if doc["anomalies"]:
        print("\nanomalies")
        for note in doc["anomalies"]:
            print(f"  [!] {note}")

    print(f"\n{'wrote' if wrote else 'would write'}  {path}")


def _print_cost(report: dict | None) -> None:
    """What a full pass over this corpus would cost, per stage.

    Stages that call no model are printed rather than filtered out. A cost table
    listing only the priced operations reads as "these are the operations", and the
    most useful fact here is how few of them reach a model at all.

    Nothing is labelled "free". A seat-licensed model carries no incremental charge
    but draws on a shared token pool, and flattening that to "free" is how a shared
    budget gets treated as unlimited.

    Rates are printed with their confidence, and a ``~`` marks any figure that
    depends on an unverified one. A projection whose inputs a reader cannot rank
    by trustworthiness will be quoted as though all of it were measured.

    An **unpriced** stage prints its token volume and the word "unpriced" — never
    a number. This project ships no rates, so that is the default state, and it
    is the honest one: the volumes are measured here, the prices are not ours to
    assert. See ``muninn/cost.py`` and ``rates.json``.
    """
    if not report:
        return
    print("\ncost estimate (model-side only)")
    print(f"  {report.get('caveat', '')}")
    # Split on *whether a model is involved*, not on whether a figure is
    # present. Keying off ``usd`` put the no-model stages in both lists once an
    # unpriced stage could carry ``None``.
    priced = [s for s in report["stages"] if s["model"]]
    unmetered = [s for s in report["stages"] if not s["model"]]

    def money(value: float | None, mark: str) -> str:
        return "  unpriced" if value is None else f"{mark}${value:>9,.2f}"

    for stage in priced:
        mark = "~" if stage["confidence"] == "low" else " "
        seat = " (you declared this seat-licensed: no incremental charge; draws "
        seat += "on shared capacity)"
        suffix = seat if (stage["usd"] == 0 and stage["model"]
                          and "seat-licensed" in stage["note"]) else ""
        print(f"  {stage['stage']:20} {money(stage['usd'], mark)}  "
              f"{stage['model'] or ''}{suffix}")
        if stage["usd"] is None:
            volumes = ", ".join(f"{k}={v:,}" for k, v in stage["inputs"].items()
                                if isinstance(v, int))
            print(f"  {'':20}   {stage['unpriced_reason']} — measured: {volumes}")
        else:
            print(f"  {'':20}  {mark}${stage['per_unit_usd']:>9,.2f} "
                  f"per {stage['unit']}")
    for stage in unmetered:
        print(f"  {stage['stage']:20}  {'no model':>10}  {stage['note']}")
    low = "~" if report["low_confidence_models"] else " "
    print(f"  {'one-time total':20} {money(report['one_time_usd'], low)}"
          f"  embed + enrich, once per session")
    print(f"  {'recurring':20} {money(report['recurring_monthly_usd'], low)}"
          f"/month  at {report['assumptions']['searches_per_month']:,} searches, "
          f"{report['assumptions']['deep_share']:.0%} deep (a guess — yours will differ)")
    if report["low_confidence_models"]:
        print(f"  [~] depends on an unverified rate: "
              f"{', '.join(report['low_confidence_models'])}")
    if report.get("unpriced_models"):
        print(f"  no rate on file for: {', '.join(report['unpriced_models'])}")
        print("      ask your agent to look up current list pricing and write "
              "rates.json beside the archive")
    if report.get("stale_rates"):
        print(f"  rates older than {cost.STALE_AFTER_DAYS} days, worth "
              f"re-checking: {', '.join(report['stale_rates'])}")


def cmd_embed(args: argparse.Namespace) -> int:
    """Generate chunk embeddings (spec 006).

    The entire cost of "semantic" is here rather than at query time — 1.9 ms to
    search 60k vectors, but real money to make them — which is why this is a
    separate resumable command with a `--dry-run`, and search is not.
    """
    st = store.open_store(args.db)
    try:
        provider = embed.resolve_provider(args.provider)
    except embed.EmbeddingUnavailable as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2

    # The same definition of "pending" the background worker uses (spec 014), so
    # the two can never disagree about what is already embedded. Id order here
    # rather than the worker's newest-first: a resumed manual run should visibly
    # continue where the last one stopped.
    rows = embed.pending_chunks(st, provider.model,
                               source=args.source, limit=args.limit)

    if args.dry_run:
        # Every read finishes before the close. The first version closed the
        # store and then called vector_count() on it, so `--dry-run` — the one
        # path whose entire job is to be safe to run — was the only path that
        # raised.
        present = embed.vector_count(st, provider.model)
        st.close()
        print(f"model    {provider.model} (dim {provider.dim})")
        print(f"planned  {len(rows):,} chunk(s) to embed")
        print(f"present  {present:,} already embedded")
        return 0
    if not rows:
        print(f"nothing to embed — every chunk already has a {provider.model} vector")
        st.close()
        return 0

    print(f"embedding {len(rows):,} chunk(s) with {provider.model}")
    written = 0
    batch = args.batch
    try:
        for start in range(0, len(rows), batch):
            window = rows[start:start + batch]
            vectors = provider.embed([r["body"] for r in window])
            for row, vector in zip(window, vectors):
                written += embed.store_vectors(
                    st, row["session_id"], provider.model, provider.dim,
                    [list(vector)], start_ordinal=row["ordinal"])
            st.commit()   # resumable: a killed run keeps everything it paid for
    except embed.EmbeddingUnavailable as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2
    except PolicyRefused as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2
    embed.clear_cache()
    st.close()
    print(f"wrote {written:,} vector(s)")
    return 0


def cmd_correlate(args: argparse.Namespace) -> int:
    """Sessions like this one, by mean vector (spec 006).

    Uses the *mean* of a session's chunks rather than its best chunk, because
    the question is "is this about the same thing", which is a property of the
    whole conversation. Best-chunk-to-best-chunk would make every pair of
    sessions that once printed a stack trace look like neighbours.
    """
    st = store.open_store(args.db)
    try:
        provider = embed.resolve_provider(args.provider)
    except embed.EmbeddingUnavailable as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2

    rows = st.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        (args.session_id + "%",)).fetchall()
    if len(rows) != 1:
        print(f"{'no' if not rows else len(rows)} sessions match {args.session_id!r}",
              file=sys.stderr)
        st.close()
        return 1

    neighbours = embed.correlate(st, provider.model, rows[0]["session_id"],
                                 limit=args.limit)
    if args.json:
        print(json.dumps([{"session_id": s, "similarity": round(v, 4)}
                          for s, v in neighbours]))
        st.close()
        return 0
    if not neighbours:
        print("no neighbours — has this archive been embedded? (`muninn embed`)")
        st.close()
        return 1
    for session_id, score in neighbours:
        rec = st.get_session(session_id) or {}
        when = (rec.get("started_at") or "")[:10]
        where = Path(rec["cwd"]).name if rec.get("cwd") else "-"
        topic = f"  {rec['topic']}" if rec.get("topic") else ""
        print(f"{score:5.3f}  {session_id[:8]}  {rec.get('source','?'):6} {when}  "
              f"{where}{topic}")
    st.close()
    return 0


def _semantic_ids(st: store.Store, args: argparse.Namespace) -> list[str] | None:
    """Session ids from semantic search, or ``None`` if it was not requested.

    Raises ``EmbeddingUnavailable`` rather than returning empty when a provider
    is missing: "no provider" and "no matches" are different answers, and
    quietly returning lexical results labelled as semantic is what spec 006
    forbids in as many words.
    """
    if not (args.semantic or args.deep):
        return None
    provider = embed.resolve_provider(args.provider)
    return [sid for sid, _score in embed.search_sessions(
        st, provider, args.query, limit=max(args.limit * 3, 30))]


def cmd_search(args: argparse.Namespace) -> int:
    st = store.open_store(args.db)
    try:
        semantic_ids = _semantic_ids(st, args)
    except embed.EmbeddingUnavailable as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2
    except PolicyRefused as exc:
        st.close()
        print(f"muninn: {exc}", file=sys.stderr)
        return 2

    try:
        hits = st.search(args.query, limit=args.limit, filters=_filters_from_args(args))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        st.close()
        return 2

    if semantic_ids is not None:
        hits = _fuse_hits(st, hits, semantic_ids, args)
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


def _fuse_hits(st: store.Store, hits: list, semantic_ids: list[str],
               args: argparse.Namespace) -> list:
    """Reciprocal-rank fusion over the lexical and semantic orderings.

    RRF rather than score normalisation: bm25 and cosine are not commensurable,
    and scaling one into the other invents a weighting nobody can defend. See
    muninn/fuse.py.

    A semantic-only hit has no lexical row to render, so it is materialised from
    the archive with an excerpt taken from its first chunk. Dropping such hits
    would make fusion pointless — finding what lexical search *missed* is the
    entire reason to run both.
    """
    lexical_ids = [h["session_id"] for h in hits]
    ordered = fuse.fuse_ids([lexical_ids, semantic_ids], limit=args.limit)

    by_id = {h["session_id"]: h for h in hits}
    fused = []
    for session_id in ordered:
        hit = by_id.get(session_id)
        if hit is None:
            row = st.conn.execute(
                "SELECT body FROM chunks WHERE session_id = ? ORDER BY ordinal LIMIT 1",
                (session_id,)).fetchone()
            hit = {"session_id": session_id, "score": 0.0, "chunk_hits": 0,
                   "excerpt": (row["body"][:300] if row else "")}
        fused.append(hit)

    if args.deep and len(fused) > 1:
        provider = providers.resolve_provider(args.rerank_model)
        pairs = [(h["session_id"], h.get("excerpt") or "") for h in fused]
        order = rerank.rerank(args.query, pairs, provider)
        rank = {sid: i for i, sid in enumerate(order)}
        fused.sort(key=lambda h: rank.get(h["session_id"], len(rank)))
    return fused


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


def cmd_resume(args: argparse.Namespace) -> int:
    """Reopen a past session in the tool that created it, or say why not.

    Prints by default and executes only under ``--exec``. Printing is safer and
    composable — the line can be read, edited, or piped — and executing hands
    the terminal to another interactive program, which is a bigger thing to do
    on a prefix match than on an explicit request.

    Exit codes carry the answer, because an agent runs this: 0 resumable, 1 no
    session matched, 3 matched but not resumable. The third is separate from the
    second on purpose — "I could not find it" and "I found it and its transcript
    is gone" lead to completely different next moves, and the second is the
    common case as the archive outlives its sources.
    """
    st = store.open_store(args.db)
    rows = st.conn.execute(
        "SELECT session_id FROM sessions WHERE session_id LIKE ? ORDER BY session_id",
        (args.session_id + "%",)).fetchall()
    if not rows:
        print(f"no session matching {args.session_id!r}", file=sys.stderr)
        st.close()
        return 1
    if len(rows) > 1:
        print(f"{len(rows)} sessions match {args.session_id!r}:", file=sys.stderr)
        for row in rows[:20]:
            print(f"  {row['session_id']}", file=sys.stderr)
        st.close()
        return 1

    rec = st.get_session(rows[0]["session_id"]) or {}
    plan = resume.plan(rec)
    st.close()

    if args.json:
        print(json.dumps({
            "session_id": plan.session_id, "source": plan.source, "cwd": plan.cwd,
            "resumable": plan.command is not None, "command": plan.command,
            "shell": plan.shell(), "refusal": plan.refusal,
        }))
        return 0 if plan.command is not None else 3

    when = (rec.get("started_at") or "")[:10] or "?"
    print(f"{plan.session_id}  {plan.source}  {when}  {plan.cwd or '-'}")
    if plan.refusal is not None:
        # Never emit a command that will fail with the vendor's own error, which
        # says nothing about why. See muninn/resume.py.
        print(f"\nnot resumable: {plan.refusal}.", file=sys.stderr)
        print(f"The archived transcript is still here: "
              f"`muninn show {plan.session_id[:8]}`", file=sys.stderr)
        return 3

    print(f"\n{plan.shell()}")
    if not args.exec_:
        return 0

    if plan.cwd and not Path(plan.cwd).is_dir():
        # The session's directory can be gone while its transcript survives.
        # Refusing beats letting the tool start in whatever directory this
        # happens to be, which would resume the right session in the wrong repo.
        print(f"\nrefusing to run: {plan.cwd} no longer exists", file=sys.stderr)
        return 3
    import subprocess

    try:
        return subprocess.call(plan.command, cwd=plan.cwd)
    except OSError as exc:
        print(f"\ncould not run {plan.command[0]!r}: {type(exc).__name__}", file=sys.stderr)
        return 3


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

    ``args.db`` is passed down so a ``muninn --db X install-agent`` is reported as
    the mismatch it is. The installed unit runs a bare ``muninn serve``, so that
    flag reaches nothing — accepting it silently would install an agent that
    ingests a different archive than the one the operator just named, which is
    the same failure as a redirected variable arriving by a different route.
    """
    return agent_install.install(force=args.force, db=args.db)


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

    _print_embeddings_section(st)
    _print_enrichment_section(st, args.db)
    _print_calibration_section(st, args.db)
    _print_plugins_section()
    _print_policy_section()
    _print_daemon_section()
    _print_menubar_section()

    st.close()
    return 0


def _print_embeddings_section(st: store.Store) -> None:
    """Vector count and memory per model (spec 006: report it so growth is visible).

    Two models present is the line worth reading. It means a half-finished
    re-embed — search uses one space at a time, so the older set is dead weight
    that still costs the memory printed next to it, and nothing else in the tool
    will ever mention it.

    The **pending** count is the spec 014 line. Now that a background worker
    embeds automatically, a stopped worker and a finished one look identical from
    outside — and this project's recurring lesson is that the expensive kind of
    staleness is the invisible kind. A backlog that is not shrinking between two
    `doctor` runs is the signal; the daemon's log says why.
    """
    print("\nembeddings")
    models = embed.models_present(st)
    if not models:
        print("  none — semantic search is unavailable until embeddings exist "
              "(`muninn serve` embeds automatically; `muninn embed` does it in the foreground)")
        return
    chunks = st.count_chunks()
    for model, dim, rows in models:
        # float32: dim * 4 bytes per vector, which is what load_matrix holds.
        mb = rows * dim * 4 / (1024 * 1024)
        # Truncated, never rounded: 9,047 of 9,049 chunks is 99.98%, and printing
        # "100% of chunks" next to a non-zero pending count is the report claiming
        # completion it does not have. 100% is reserved for rows == chunks.
        coverage = f"{int(rows / chunks * 100)}% of chunks" if chunks else "no chunks"
        print(f"  {model:44} dim {dim:<5} {rows:>8,} vectors  {mb:6.1f} MB  {coverage}")
        pending = embed.pending_count(st, model)
        if pending:
            print(f"  {'':44} {pending:>14,} chunk(s) pending")
    if len(models) > 1:
        print("  WARNING: more than one embedding model is present. Search uses one at "
              "a time, so the others are dead weight — finish the re-embed or delete them")


def _print_enrichment_section(st: store.Store, db: str) -> None:
    """Facet coverage, and whether the background worker would be allowed to run.

    The spec 018 line, and it exists for a failure the embedding section does not
    have. A stopped embedder leaves a visible backlog; a *refused* enricher leaves
    a corpus where every facet filter returns nothing — and "no session was fixed"
    is a perfectly ordinary-looking answer. Before this, 681 sessions and 2
    enriched read identically to a healthy archive from every command's output.

    So two facts, and the second is the one worth printing even when it is boring:
    how much of the eligible corpus has facets, and whether an unattended pass
    *would* cost money — because if it would, the daemon is refusing to run it and
    the coverage above will not move on its own.
    """
    print("\nenrichment")
    calibration = enrich.load_calibration(db)
    if calibration is None:
        print("  no calibration — the enrichment gate is not derived yet; "
              "run `muninn survey`")
        return

    plan = enrich.plan(st, calibration)
    enriched = st.conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE topic IS NOT NULL AND topic != ''"
    ).fetchone()[0]
    planned = len(plan.candidates)
    eligible = enriched + planned
    coverage = f"{int(enriched / eligible * 100)}% of eligible" if eligible else "nothing eligible"
    print(f"  facets      {enriched:,} of {eligible:,} session(s) enriched  ({coverage})")
    if planned:
        print(f"  pending     {planned:,} session(s) · ~{plan.estimated_calls:,} model call(s)")
    for reason, count in sorted(plan.skipped.items()):
        print(f"  {'':11} skipped {count:,} ({reason})")

    # Which model, and whether an unattended pass may use it. Resolved rather than
    # assumed: the estimate and the refusal must name the same hop.
    try:
        provider = providers.resolve_provider()
        model = provider.model
    except Exception:                       # noqa: BLE001 - doctor never fails here
        print("  auto        no text provider resolved — background enrichment is off")
        return
    declared = getattr(provider, "metered", None)
    metered = bool(declared) if declared is not None else cost.bills_per_token(model)
    if metered:
        print(f"  auto        REFUSED — {model} bills per token, so the daemon will not "
              f"enrich unattended")
        print("              start it with `--enrich-metered` to allow that, or run "
              "`muninn enrich` yourself")
    elif declared is not None:
        # The provider said so; this line reports *that* and does not upgrade it
        # into a claim about the reader's billing. The same model id on a
        # subscription and on metered API access is one string at opposite ends
        # of "does this cost anything", and only the provider knows which it is
        # holding. Saying "carries no incremental charge" here asserted something
        # about somebody's account that nothing in this process can see.
        print(f"  auto        allowed — the {model} provider reports it does not "
              f"bill per token")
        print("              your own billing arrangement is not visible from "
              "here; `muninn survey` projects list prices only")
    else:
        print(f"  auto        allowed — no per-token rate is on file for {model}")


def _print_calibration_section(st: store.Store, db: str) -> None:
    """Whether the derived thresholds still describe this corpus (spec 011).

    Three states, kept distinct because they need three different actions and a
    report that collapses them has told the reader nothing:

    - **Never surveyed** — nothing downstream has thresholds at all.
    - **Surveyed and current** — the positive answer, stated rather than implied
      by the absence of a warning. A section that only ever speaks up when
      something is wrong leaves a reader unable to tell "fine" from "not
      checked".
    - **Surveyed and drifted** — with the reasons, because "re-run survey" on
      its own is an instruction rather than a finding.
    """
    path = survey.calibration_path(db)
    print("\ncalibration (derived thresholds; `muninn survey` writes them)")
    print(f"  file        {path}")
    doc = survey.read_calibration(path)
    if doc is None:
        if path.exists():
            print("              present but unreadable, or written by another schema "
                  "— treat it as never surveyed")
        else:
            print("              never surveyed — nothing downstream has derived thresholds")
        print("              run `muninn survey`")
        return

    print(f"  surveyed    {doc.get('surveyed_at', '(unrecorded)')}")
    for source, report in sorted(doc.get("sources", {}).items()):
        gate = report.get("enrichment_gate", {})
        if gate.get("sessions"):
            print(f"  {source:11} enrich gate >= {gate['threshold_words']:,} words "
                  f"({gate['coverage_pct']:.0f}% of conversation text)")

    reasons = survey.drift(st, doc)
    if not reasons:
        print("  drift       none — the thresholds still describe this corpus")
        return
    # Reported as a finding, not as an instruction. A running daemon re-derives
    # this on its own (spec 011), so telling every reader to run `muninn survey`
    # would be advice that is usually already being followed -- and the reason
    # drift is visible here at all is that it *was* only ever advice, which
    # nobody was watching for.
    print(f"  WARNING: the calibration no longer describes this archive "
          f"({len(reasons)} reason(s))")
    for reason in reasons:
        print(f"           - {reason}")
    print("           `muninn serve` re-derives this within the hour; run "
          "`muninn survey` to do it now,")
    print("           or if the daemon was started with --no-recalibrate.")


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
        _print_environment_divergence()
    else:
        print(f"  at login    not installed — `muninn install-agent` adds a {agent.label}")


def _print_environment_divergence() -> None:
    """Warn when the installed agent reads and writes somewhere this shell does not.

    Reported only for an *installed* agent, because with none installed there is
    no second environment for this one to disagree with — a redirected shell is
    then simply a redirected shell, which is a normal thing to run tests in.

    This is the half of tohuw/muninn#7 that the install-time refusal cannot
    cover. A refusal answers "is this install about to be wrong"; it says nothing
    about an environment that changed afterwards, or an agent installed by an
    earlier version that had no check at all. Both surface here, at the moment
    someone is already asking what state Muninn is in.
    """
    mismatch = agent_install.environment_mismatch()
    if not mismatch:
        return
    print("  WARNING: the installed agent does not inherit this shell's environment, so "
          "it uses different paths")
    for line in agent_install.format_mismatch(mismatch, indent="           "):
        print(line)


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
        # The load-bearing line for spec 015: a plugin may declare the default
        # text provider, and the whole justification for allowing that is that
        # it is *visible* here rather than inferred from precedence rules. A
        # declared default that doctor did not print would be exactly the
        # silent-preference failure `providers.resolve_provider` refused for
        # two specs.
        if getattr(spec, "default_text_provider", None):
            print(f"  {'':16} {'':11} default text provider: "
                  f"{spec.default_text_provider} (override per command with --provider)")
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
                    "loopback; embeds new chunks in the background when an "
                    "embedding provider is installed; writes a state file a "
                    "supervisor can read; and tears all of it down on SIGTERM, "
                    "SIGHUP or Ctrl-C. See docs/specs/010-daemon.md and "
                    "docs/specs/014-automatic-embedding.md.")
    p_serve.add_argument("--source", choices=("claude", "codex"),
                         help="ingest only this source (default: every configured root)")
    p_serve.add_argument("--no-menubar", action="store_true",
                         help="do not publish a raven descriptor or serve /api/menu; "
                              "ingest only (docs/specs/009)")
    p_serve.add_argument("--no-embed", action="store_true",
                         help="do not embed in the background; semantic search then "
                              "covers only what `muninn embed` has already written "
                              "(docs/specs/014)")
    p_serve.add_argument("--no-enrich", action="store_true",
                         help="do not enrich in the background; --outcome and the "
                              "other facet filters then stay empty until you run "
                              "`muninn enrich` yourself (docs/specs/018)")
    p_serve.add_argument("--no-recalibrate", action="store_true",
                         help="do not re-derive the enrichment gate when the "
                              "archive outgrows it. On by default because a "
                              "survey calls no model: it is SQL aggregates, and "
                              "the alternative is a gate that silently covers "
                              "less of the corpus every week while enrichment "
                              "keeps reporting 100% of eligible (docs/specs/011)")
    p_serve.add_argument("--enrich-metered", action="store_true",
                         help="allow background enrichment to use a model that bills "
                              "per token. Off by default: without it the worker "
                              "refuses to spend unattended and says which model it "
                              "declined, which is what stops a provider falling back "
                              "from a seat-licensed model to a metered one from "
                              "quietly starting a bill (docs/specs/018)")
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
                    "loop rather than a service. Refuses too when this shell's "
                    "paths are not the ones a login session resolves, since the "
                    "service does not inherit your environment.")
    p_install_agent.add_argument(
        "--force", action="store_true",
        help="install even though this shell's paths differ from the ones the "
             "service will use — for when you have already set them where login "
             "sessions see them. The divergence is still printed.")
    p_install_agent.set_defaults(func=cmd_install_agent)

    p_uninstall_agent = sub.add_parser(
        "uninstall-agent", help="remove the login agent installed by install-agent")
    p_uninstall_agent.set_defaults(func=cmd_uninstall_agent)

    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true",
                          help="print a JSON array of result objects instead of text")
    p_search.add_argument("--semantic", action="store_true",
                          help="fuse semantic results with lexical ones (needs "
                               "an embedding provider; errors rather than "
                               "silently returning lexical-only results)")
    p_search.add_argument("--deep", action="store_true",
                          help="--semantic, plus an LLM rerank over the top "
                               "candidates. Slower and costs a model call")
    p_search.add_argument("--provider", help="embedding provider name")
    # Not `--model`: that is already a *filter* (substring match on the model a
    # session was recorded with), and one flag cannot mean both "search for
    # sessions that used X" and "rerank using X".
    p_search.add_argument("--rerank-model", dest="rerank_model",
                          help="text model for --deep reranking")
    _add_filter_args(p_search)
    p_search.set_defaults(func=cmd_search)

    p_embed = sub.add_parser(
        "embed", help="generate chunk embeddings for semantic search",
        description="Embed chunks that do not yet have a vector for the "
                    "provider's model. Resumable: progress is committed per "
                    "batch, so a killed run keeps everything it paid for. "
                    "Vectors from different models are never mixed — the model "
                    "id is part of the key, because two embedding spaces "
                    "compared together return confident nonsense rather than "
                    "an error. See docs/specs/006.")
    p_embed.add_argument("--provider", help="embedding provider name")
    p_embed.add_argument("--source", help="only this source")
    p_embed.add_argument("--limit", type=int, help="stop after this many chunks")
    p_embed.add_argument("--batch", type=int, default=64)
    p_embed.add_argument("--dry-run", action="store_true",
                         help="report what would be embedded; generate nothing")
    p_embed.set_defaults(func=cmd_embed)

    p_correlate = sub.add_parser(
        "correlate", help='"conversations like this one", by mean vector',
        description="Nearest neighbours of a session's mean embedding vector, "
                    "excluding itself. Needs `muninn embed` to have run.")
    p_correlate.add_argument("session_id", help="full id or a prefix")
    p_correlate.add_argument("--limit", type=int, default=10)
    p_correlate.add_argument("--provider", help="embedding provider name")
    p_correlate.add_argument("--json", action="store_true")
    p_correlate.set_defaults(func=cmd_correlate)

    p_recall = sub.add_parser(
        "recall", help="what the archive knows about where you are working",
        description="Takes a place rather than a question. Lists unfinished "
                    "threads, prior work in the same repository, and related "
                    "work from other repositories. Defaults to wherever the "
                    "most recent session was working. Calls no model.")
    p_recall.add_argument(
        "--repo", help="repository to recall (default: the most recently active)")
    p_recall.add_argument("--limit", type=int, default=recall.DEFAULT_LIMIT,
                          help="entries per section")
    p_recall.add_argument("--provider", help="embedding provider name")
    p_recall.add_argument("--json", action="store_true")
    p_recall.set_defaults(func=cmd_recall)

    p_show = sub.add_parser("show", help="print one session (id prefixes are fine)")
    p_show.add_argument("session_id")
    p_show.set_defaults(func=cmd_show)

    p_resume = sub.add_parser(
        "resume", help="reopen a past session in the tool that created it",
        description="Print the invocation that reopens a session, from the "
                    "directory it ran in. Refuses rather than emitting a "
                    "command that will fail: most of what this archive holds "
                    "cannot be resumed, because the vendor swept the transcript "
                    "after 30 days and the archive is now the only copy. Exit 0 "
                    "resumable, 1 no match, 3 matched but not resumable.")
    p_resume.add_argument("session_id", help="full id or a prefix")
    p_resume.add_argument("--exec", dest="exec_", action="store_true",
                          help="run the command instead of printing it")
    p_resume.add_argument("--json", action="store_true")
    p_resume.set_defaults(func=cmd_resume)

    p_doctor = sub.add_parser("doctor", help="report archive health and index lag")
    p_doctor.set_defaults(func=cmd_doctor)

    p_survey = sub.add_parser(
        "survey", help="measure the corpus and derive thresholds into calibration.json",
        description="Measure the present corpus and write an inspectable "
                    "calibration.json beside the archive. Muninn hard-codes no "
                    "corpus thresholds: a fixed one encodes one person's habits "
                    "as everyone's defaults — a proposed 300-word enrichment "
                    "gate selected 37%% of Claude sessions but 91%% of Codex "
                    "ones on the same machine. Every statistic is scoped to a "
                    "provenance class, and tool-invoked sessions contribute to "
                    "none of them. See "
                    ".valholl/articles/derived-calibration.md.")
    p_survey.add_argument("--source", choices=("claude", "codex"),
                          help="measure index lag for this source only")
    p_survey.add_argument("--out", help="write calibration here instead of beside the archive")
    p_survey.add_argument("--dry-run", action="store_true",
                          help="print what would be derived; write nothing")
    p_survey.add_argument("--json", action="store_true",
                          help="print the calibration document instead of a report")
    p_survey.set_defaults(func=cmd_survey)

    p_backfill = sub.add_parser(
        "backfill", help="ingest a claudex/codexdex prose index (one-time migration)",
        description="Backfill the predecessors' prose indexes. Muninn supersedes "
                    "claudex and codexdex, but their indexes are archives too: "
                    "they cover sessions whose raw transcripts the vendor swept "
                    "months ago, and that data exists nowhere else. Backfilled "
                    "sessions are recorded with origin='prose-index' and never "
                    "overwrite a richer raw-derived session — that decision is "
                    "recorded per item as `superseded-by-richer-origin`, not "
                    "silently taken. Defaults to ~/.claudex and ~/.codexdex.")
    p_backfill.add_argument("path", nargs="*",
                            help="index root(s) (default: the known predecessors)")
    p_backfill.add_argument("--source", help="source to attribute files that do not "
                                             "name one (default: claude)")
    p_backfill.add_argument("--actor", default="cli")
    p_backfill.add_argument("--json", action="store_true",
                            help="print only the machine-readable import receipt(s)")
    p_backfill.set_defaults(func=cmd_backfill)

    p_enrich = sub.add_parser(
        "enrich", help="extract topic, outcome and decisions from substantive sessions",
        description="Run one model pass per substantive session to extract "
                    "queryable facets. This is the one expensive operation in "
                    "Muninn — a full pass is thousands of model calls — so "
                    "--dry-run reports the plan and the estimated call count "
                    "without spending any. Tool-invoked sessions are never "
                    "enriched, the length gate is read from calibration.json "
                    "rather than assumed, and transcript text is redacted "
                    "before it reaches a provider. See docs/specs/005.")
    p_enrich.add_argument("session_id", nargs="?",
                          help="enrich one session (id or prefix), ignoring the length gate")
    p_enrich.add_argument("--source", help="only this source")
    p_enrich.add_argument("--limit", type=int, help="stop after this many sessions")
    p_enrich.add_argument("--force", action="store_true",
                          help="re-enrich sessions that already have facets")
    p_enrich.add_argument("--dry-run", action="store_true",
                          help="report the plan and estimated call count; make no calls")
    p_enrich.add_argument("--model", help=f"model id (default: {providers.DEFAULT_MODEL})")
    p_enrich.add_argument("--provider",
                          help="name of a plugin-contributed text provider "
                               "(default: the built-in `claude -p`)")
    p_enrich.add_argument("--json", action="store_true",
                          help="print the plan as JSON and make no calls")
    p_enrich.add_argument("--shard", metavar="K/N",
                          help="enrich only shard K of N, so several workers can "
                               "run at once on disjoint slices. The bottleneck is "
                               "per-call latency, not this machine: a corpus pass "
                               "measured 34.8s per call and ~11h single-threaded. "
                               "Partitioning is by SHA-256 of the session id, so "
                               "every worker computes the same one")
    p_enrich.set_defaults(func=cmd_enrich)

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
    # Choices come from enrich.OUTCOMES rather than a literal, because the
    # literal drifted: it was written before spec 005 landed and listed three of
    # the four values, so `--outcome exploratory` was rejected by argparse while
    # 261 sessions in the archive carried exactly that outcome. A filter that
    # cannot express a value the data holds is worse than no filter — it reports
    # a usage error for a correct query.
    p.add_argument("--outcome", choices=enrich.OUTCOMES,
                   help="filter by enriched outcome (see `muninn enrich`)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
