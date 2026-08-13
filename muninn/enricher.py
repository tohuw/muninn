"""The background enrichment worker: facets without someone remembering to ask.

Normative source: docs/specs/018-automatic-enrichment.md. Read
``muninn/embedder.py`` first — this is its sibling and follows the same shape for
the same reasons (own thread, own connection, every failure non-fatal to the
daemon, a stall guard that stops rather than spends).

## Why this exists, and why it came second

Spec 014 made embedding automatic. Enrichment stayed manual, and the failure that
produced is quieter than a missing vector: ``--outcome fixed`` returns nothing,
and *nothing* is a perfectly ordinary answer. A user cannot tell "no session was
fixed" from "no session has facets yet", so the filters that make the archive
queryable by *meaning* silently do not work. Measured on the author's own archive
before this shipped: 681 sessions, 2 enriched. Embedding was at 100%.

The reason it waited is the reason the guard below exists. Embedding is cheap
enough that automatic is uncontroversial — a whole corpus is cents. Enrichment is
the one expensive thing Muninn does, so "automatic" had to mean *automatic and
answerable*: spec 016 had to be able to say what a pass costs before a daemon
could start one unasked.

## The metered guard, which is the whole point of this module

**A daemon must not silently convert a stage that carries no charge into one that
bills.**

The Cisco distribution's text provider is a chain: Codex Luna first (seat access,
no incremental charge), Bedrock Haiku as a fallback. It reports the hop that *would*
run, resolved per call. So a laptop that loses its Codex CLI — an uninstall, an
expired login, a PATH change — starts answering "Bedrock Haiku" from the same
property, and an unattended loop would keep going and start billing. Nobody asked
for that, and nobody would see it until an invoice.

So the model is re-checked **before every pass**, not once at startup, and a
metered model stops the worker unless the operator opted in
(``--enrich-metered``). A startup check would be exactly the wrong shape here: the
whole risk is the model changing while the process runs.

**The provider answers the question, not the model id**, and getting that wrong is
what nearly shipped this feature broken. A model id cannot say whether a call
bills: ``claude -p`` on a Claude Code subscription and the identical model on
Bedrock are the same id at opposite ends of "does this cost anything". Deciding
from the rate table alone made the worker refuse on every default public install —
correct-looking, and the feature not shipping. So a provider may declare
``metered``, and only when it has no opinion does ``cost.bills_per_token`` decide.

That fallback fails closed: an unknown model counts as metered. Which is the
opposite of how the rest of ``cost`` treats an unknown rate, on purpose — an
estimate a human reads should say "unverified" and carry on; a loop that would
spend should stop.

## Cheapest-first, and why this does not order by recency

``embedder`` asks for newest-first, because the value of a vector is highest for
the session you just finished. This worker takes ``enrich.plan``'s own ordering,
which is **shortest-first**, and does not override it. That ordering already
encodes a decision this module has no business relitigating (see ``plan``'s
docstring): cost tracks length, the run is resumable, and longest-first spent
fifteen minutes on one 622,232-word session before committing a single row, which
is indistinguishable from a hang.

## Calibration is polled, not required at startup

``plan`` returns an empty plan for an un-surveyed archive rather than defaulting a
threshold, and that is spec 011's rule. This worker therefore treats "no
calibration" like an empty backlog — it waits and re-reads — instead of refusing
to start. A user who runs ``muninn survey`` an hour after installing the daemon
should not also have to restart it, and the alternative (start, then die
permanently) would need a restart to recover from a state that fixes itself.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from . import cost, enrich, providers, store
from .policy import PolicyRefused

logger = logging.getLogger("muninn.enricher")

#: How long to wait before looking for work again once the backlog is empty, or
#: once calibration is found to be missing. Longer than the embedder's 30 s: a
#: session becomes enrichable when it *ends*, not as it is written, so there is
#: nothing this poll could usefully catch sooner.
DEFAULT_IDLE_INTERVAL_S = 120.0

#: Sessions per pass. Small on purpose. Each one is at least one model call and
#: possibly fifty, ``enrich_sessions`` commits per session, and re-planning
#: between passes is what lets the metered guard re-run — a large batch would
#: postpone that check behind hours of work.
DEFAULT_BATCH = 8

#: Consecutive passes with candidates but zero enrichments before the worker
#: gives up. A session whose response cannot be parsed is recorded as a failure
#: and stays un-enriched, so it is planned again on the next pass: without this,
#: one malformed-response session is an unbounded loop against a paid API.
STALL_LIMIT = 3

#: Backoff bounds for a transient provider failure.
BACKOFF_START_S = 10.0
BACKOFF_MAX_S = 900.0

# Why the worker stopped, as a closed vocabulary — same discipline as
# ``embedder``'s and ``daemon.HOLDERS``, because `doctor` reads these back.
STOPPED_NOT_STARTED = "not-started"
STOPPED_NO_PROVIDER = "no-provider"
STOPPED_METERED = "metered-refused"
STOPPED_REQUESTED = "requested"
STOPPED_POLICY = "policy-refused"
STOPPED_STALLED = "stalled"
STOPPED_RUNNING = "running"


class BackgroundEnricher:
    """Drains the enrichment backlog in a thread, for as long as the daemon lives.

    Composition, like the embedder: the work is ``enrich.plan`` →
    ``enrich.enrich_sessions``, and this class contributes only *when* that
    happens, whether it is allowed to cost anything, and what to do when it fails.

    ``allow_metered`` is the operator's explicit opt-in to spending. It defaults
    to ``False`` and there is no environment variable for it — a switch that turns
    on unattended billing should be visible in the command that starts the
    daemon, not in a shell profile somebody else wrote.
    """

    def __init__(self, db_path: str | Path, *,
                 provider: Any | None = None,
                 batch: int = DEFAULT_BATCH,
                 idle_interval_s: float = DEFAULT_IDLE_INTERVAL_S,
                 allow_metered: bool = False,
                 backoff_start_s: float = BACKOFF_START_S,
                 backoff_max_s: float = BACKOFF_MAX_S,
                 stall_limit: int = STALL_LIMIT,
                 announce: Callable[[str], None] | None = None) -> None:
        self.db_path = db_path
        self.batch = max(1, batch)
        self.idle_interval_s = idle_interval_s
        self.allow_metered = allow_metered
        self.backoff_start_s = backoff_start_s
        self.backoff_max_s = backoff_max_s
        self.stall_limit = stall_limit
        self.announce = announce or (lambda _msg: None)

        # Injected in tests; resolved at start() otherwise, because
        # resolve_provider() consults installed plugins and must not run at
        # import time.
        self.provider = provider

        self.enriched = 0
        self.failed = 0
        self.passes = 0
        self.last_error: str | None = None
        self.stopped_reason: str = STOPPED_NOT_STARTED
        self.billed_model: str | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._warned_uncalibrated = False

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        """Resolve a provider and start the thread. ``False`` if it must not run.

        Three ways to return ``False``, and they are different facts rather than
        one "unavailable": no text provider installed, a provider that reports it
        cannot run, and a provider that would bill without an opt-in. Each sets
        its own ``stopped_reason`` so `doctor` can say which.
        """
        if self._thread is not None:
            return True
        if self.provider is None:
            try:
                self.provider = providers.resolve_provider()
            except Exception as exc:        # noqa: BLE001 - see the class docstring
                logger.info("muninn enricher: not started — no text provider (%s)", exc)
                self.stopped_reason = STOPPED_NO_PROVIDER
                return False

        # ``available()`` returns **a reason string when unusable and None when
        # usable** (``providers.TextProvider``), which is the inverse of the shape a
        # reader expects from the name. Checking it for ``False`` — the obvious
        # misreading, and one this module shipped for exactly one commit — makes the
        # guard never fire, so an unusable provider starts a worker that fails every
        # pass and backs off forever. It is contractually I/O-free (spec 008), so
        # calling it here costs nothing.
        try:
            reason = self.provider.available()
        except Exception:                   # noqa: BLE001 - a provider bug is not fatal
            logger.info("muninn enricher: not started — provider availability check failed")
            self.stopped_reason = STOPPED_NO_PROVIDER
            return False
        if reason:
            logger.info("muninn enricher: not started — provider %s is unusable: %s",
                        getattr(self.provider, "name", "?"), reason)
            self.stopped_reason = STOPPED_NO_PROVIDER
            return False

        if not self._spending_allowed():
            return False

        self.stopped_reason = STOPPED_RUNNING
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="muninn-enricher", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to finish its session and join it. Idempotent.

        The timeout is short and the thread is a daemon thread, for the same
        reason the embedder's is: one enrichment call can take minutes, facets are
        committed per session, and blocking the daemon's teardown on a provider
        round trip would orphan the descriptor this project keeps fixing.
        """
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("muninn enricher: still busy after %.0fs; "
                               "leaving it to be reaped at exit", timeout)
        if self.stopped_reason == STOPPED_RUNNING:
            self.stopped_reason = STOPPED_REQUESTED

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def status(self) -> dict[str, Any]:
        """A flat dict for `doctor` and for tests to assert against."""
        return {
            "provider": getattr(self.provider, "name", None),
            "model": self._model(),
            "running": self.running,
            "reason": self.stopped_reason,
            "enriched": self.enriched,
            "failed": self.failed,
            "passes": self.passes,
            "allow_metered": self.allow_metered,
            "billed_model": self.billed_model,
            "last_error": self.last_error,
        }

    # ── the money guard ───────────────────────────────────────────────────────

    def _model(self) -> str | None:
        """The model that *would* run right now, or None if it cannot be read.

        Read through a property on the chain provider, so this is a question with
        a different answer at different times — which is the entire reason
        :meth:`_spending_allowed` is called per pass rather than once.
        """
        try:
            return self.provider.model if self.provider is not None else None
        except Exception:                   # noqa: BLE001 - a provider bug
            return None

    def _model_bills(self, model: str) -> bool:
        """Whether the next call bills. Asks the provider before the rate table.

        The provider is authoritative because the model id genuinely cannot answer
        this: ``claude -p`` on a Claude Code subscription and the same model on
        Bedrock are the same id at opposite ends of "does this cost anything".
        Deciding from the id alone made this worker refuse to run for every public
        install, which is the feature not shipping.

        ``None`` from the provider means "no opinion", and only then does the rate
        table decide — still failing closed on an unknown model.
        """
        declared = getattr(self.provider, "metered", None)
        if declared is not None:
            return bool(declared)
        return cost.bills_per_token(model)

    def _spending_allowed(self) -> bool:
        """False when the next call would bill and nobody opted in.

        Records the offending model on the instance so `doctor` and the daemon log
        can name it. Reporting "enrichment stopped" without saying *which* model
        it refused to pay for would send the reader to the wrong question.
        """
        if self.allow_metered:
            return True
        model = self._model()
        if model is None:
            # Unreadable is not free. Same fail-closed rule as an unknown rate.
            self.billed_model = None
            logger.warning("muninn enricher: not enriching — cannot determine which "
                           "model would run; pass --enrich-metered to allow spending")
            self.stopped_reason = STOPPED_METERED
            return False
        if self._model_bills(model):
            self.billed_model = model
            self.announce(
                f"muninn enricher: not enriching — {model} bills per token and "
                f"automatic enrichment is free-providers-only; run `muninn enrich` "
                f"yourself, or start the daemon with --enrich-metered")
            self.stopped_reason = STOPPED_METERED
            return False
        self.billed_model = None
        return True

    # ── the loop ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        """Own connection, own lifetime, no exception left unlogged."""
        try:
            st = store.open_store(self.db_path)
        except Exception as exc:            # noqa: BLE001
            logger.warning("muninn enricher: cannot open the archive: %s", exc)
            self.last_error = str(exc)
            self.stopped_reason = STOPPED_STALLED
            return
        try:
            self._loop(st)
        except Exception as exc:            # noqa: BLE001 - a thread that dies
            # silently is the failure this module is written against.
            logger.exception("muninn enricher: stopping after an unhandled error")
            self.last_error = str(exc)
            self.stopped_reason = STOPPED_STALLED
        finally:
            st.close()

    def _loop(self, st: store.Store) -> None:
        provider = self.provider
        assert provider is not None       # start() guarantees it before threading

        stalls = 0
        backoff = self.backoff_start_s
        announced_backlog = False

        while not self._stop.is_set():
            # Re-checked every pass. The chain can change hops underneath us, and
            # that is the failure this guard exists for — not a startup condition.
            if not self._spending_allowed():
                return

            calibration = enrich.load_calibration(str(self.db_path))
            if calibration is None:
                if not self._warned_uncalibrated:
                    self.announce("muninn enricher: waiting — no calibration yet; "
                                  "run `muninn survey` to derive the enrichment gate")
                    self._warned_uncalibrated = True
                if self._stop.wait(self.idle_interval_s):
                    return
                continue
            self._warned_uncalibrated = False

            try:
                plan = enrich.plan(st, calibration, limit=self.batch)
            except Exception as exc:       # noqa: BLE001 - transient until proven
                logger.warning("muninn enricher: planning failed (%s); retrying in %.0fs",
                               exc, backoff)
                self.last_error = str(exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.backoff_max_s)
                continue

            if not plan.candidates:
                stalls = 0
                if self._stop.wait(self.idle_interval_s):
                    return
                continue

            if not announced_backlog:
                # Said once, and it must not be silent: on a fresh archive this is
                # hundreds of sessions, and on a metered fallback it would be a
                # bill. The count is this pass's window, so the phrasing does not
                # claim to be the whole backlog.
                announced_backlog = True
                self.announce(
                    f"muninn enricher: enriching in the background with "
                    f"{self._model()} ({len(plan.candidates)} session(s) this pass)")

            try:
                result = enrich.enrich_sessions(st, plan.candidates, provider)
            except PolicyRefused as exc:
                # A statement about the whole run's configuration, not one
                # session's. Retrying would produce identical refusals forever.
                logger.warning("muninn enricher: stopping — model policy refused %s: %s",
                               self._model(), exc)
                self.last_error = str(exc)
                self.stopped_reason = STOPPED_POLICY
                return
            except Exception as exc:       # noqa: BLE001 - transient until proven
                logger.warning("muninn enricher: pass failed (%s); retrying in %.0fs",
                               exc, backoff)
                self.last_error = str(exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, self.backoff_max_s)
                continue

            self.passes += 1
            self.enriched += result.enriched
            self.failed += result.failed
            backoff = self.backoff_start_s

            if result.enriched == 0:
                stalls += 1
                if stalls >= self.stall_limit:
                    logger.warning(
                        "muninn enricher: stopping — %d passes enriched nothing with "
                        "%d candidate(s) planned; run `muninn enrich` to see the error",
                        stalls, len(plan.candidates))
                    self.stopped_reason = STOPPED_STALLED
                    return
                if self._stop.wait(backoff):
                    return
                continue

            stalls = 0
            # Straight into the next pass. The backlog should drain as fast as the
            # provider allows, and the idle wait above keeps the thread cheap once
            # it is empty.
