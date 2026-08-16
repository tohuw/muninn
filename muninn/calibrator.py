"""Keep the derived calibration describing the archive it was derived from.

The enrichment gate is derived from the corpus (spec 011), which means it is
only correct for the corpus it was derived *from*. Archives grow. On one real
machine it tripled between surveys, and the gate that had been chosen to cover
85% of conversation text quietly fell to 74% — enrichment kept reporting "100%
of eligible" the whole time, because it was faithfully enriching everything the
stale gate allowed. Nothing was broken; the number just meant less each week.

``doctor`` has always detected this and printed "re-run `muninn survey`". That
is a warning nobody is watching for. A calibration that only stays current when
a human notices a line in a health report is a slower version of not having one,
so this worker keeps it current instead.

**Surveying calls no model.** It is SQL aggregates over the archive, so running
it unattended costs nothing to run. What it *changes* can cost: a re-derived
gate usually admits sessions the stale one excluded, and those are then enriched
by the enricher — under that worker's own metered guard, which this one neither
sees nor bypasses. So the spending decision stays exactly where it was, with
``--enrich-metered``; this only stops the gate being wrong.

**Why not fold this into the enricher**, which already re-reads the calibration
on every pass: because the enricher exits its loop entirely when spending is not
allowed. On a metered setup it stops before it would ever look, so the archive
that most needs an accurate gate — the one whose owner is being careful about
cost — would be the one whose gate rotted. Separate worker, separate lifecycle.

**A first survey is included, not just a re-survey.** An un-surveyed archive
makes ``enrich.plan`` return an empty plan by design (spec 011 refuses to
default a threshold), so a fresh install enriched nothing until someone ran
``muninn survey`` by hand. That is the same babysitting in its first and most
confusing form: everything looks installed and nothing happens.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from . import store, survey

logger = logging.getLogger("muninn.calibrator")

#: How long between drift checks. Generously long on purpose: a gate is a
#: property of the whole corpus, so it moves on the timescale of weeks of use,
#: and the check itself is a handful of aggregate queries over every session.
#: Nothing is gained by asking often, and an archive being actively ingested
#: would pay for it repeatedly.
DEFAULT_INTERVAL_S = 3600.0

#: Checked once shortly after start too, so a daemon that is restarted after a
#: long gap does not wait a full interval before noticing. Long enough that the
#: startup sweep — the heaviest thing the daemon does — is not competing with it.
STARTUP_DELAY_S = 300.0

STOPPED_RUNNING = "running"
STOPPED_REQUESTED = "requested"
STOPPED_DISABLED = "disabled"


class BackgroundCalibrator:
    """Re-derives ``calibration.json`` when it stops describing the archive.

    Composition, like the embedder and the enricher: the work is
    ``survey.drift`` → ``survey.survey`` → ``survey.write_calibration``, and this
    class contributes only *when* that happens and what to say about it.
    """

    def __init__(self, db_path: str | Path, *,
                 roots: dict[str, Path] | None = None,
                 interval_s: float = DEFAULT_INTERVAL_S,
                 startup_delay_s: float = STARTUP_DELAY_S,
                 announce: Any | None = None) -> None:
        self.db_path = db_path
        self.roots = roots
        self.interval_s = interval_s
        self.startup_delay_s = startup_delay_s
        self.announce = announce or (lambda _message: None)
        self.stopped_reason = STOPPED_DISABLED
        self.surveys = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread is not None:
            return True
        self._stop.clear()
        self.stopped_reason = STOPPED_RUNNING
        self._thread = threading.Thread(
            target=self._run, name="muninn-calibrator", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to finish and join it. Idempotent."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():   # pragma: no cover - a survey mid-flight
                logger.warning("muninn calibrator: still busy after %.0fs; "
                               "leaving it to be reaped at exit", timeout)
        if self.stopped_reason == STOPPED_RUNNING:
            self.stopped_reason = STOPPED_REQUESTED

    # ── the loop ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        if self._stop.wait(self.startup_delay_s):
            return
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:       # noqa: BLE001
                # Never fatal. A calibration that could not be refreshed leaves
                # the previous one in place, which is the state this worker
                # exists to improve on rather than a state it must guarantee.
                logger.warning("muninn calibrator: pass failed", exc_info=True)
            if self._stop.wait(self.interval_s):
                return

    def check_once(self) -> list[str]:
        """Survey if the calibration is missing or has drifted. Returns why.

        An empty list means "still current, nothing written" — the common case,
        and deliberately distinguishable from "checked and rewrote".
        """
        path = survey.calibration_path(self.db_path)
        st = store.open_store(self.db_path)
        try:
            doc = survey.read_calibration(path)
            if doc is None:
                # Missing or unreadable. Both mean nothing downstream has a
                # threshold, which enrich.plan treats as "enrich nothing".
                reasons = ["no calibration had been derived yet"]
            else:
                reasons = survey.drift(st, doc)
                if not reasons:
                    return []
            fresh = survey.survey(st, db=self.db_path, roots=self.roots)
        finally:
            st.close()

        survey.write_calibration(fresh, path)
        self.surveys += 1
        self.announce("muninn calibrator: re-derived the enrichment gate — "
                      + "; ".join(reasons))
        return reasons
