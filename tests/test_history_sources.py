"""What core decides a contributed source's silence means (tohuw/muninn#1).

``HistorySource.reconcile()`` lets a plugin say *what it still sees*. Everything
here is about the other half — core turning that into ``source_present = 0`` and
never into a delete, because the archived prose may be the only surviving copy
(.valholl/articles/archive-of-record.md).

The split under test is deliberate and is the reason option 2 was chosen over
handing plugins a ``Store``: a plugin author who is never given the means to
delete cannot get the never-delete rule wrong. So these tests drive
``ingest.reconcile_history_source`` with deliberately badly-behaved sources — one
that raises, one that vouches for nothing, one whose name is a SQL ``LIKE``
wildcard — and assert that no arrangement of them costs a single word of prose.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from muninn import ingest
from muninn.ingest import ReconcileOutcome
from muninn.plugins import SourceContext
from muninn.store import open_store

PROSE = "[USER] the only surviving copy\n\n[ASSISTANT] of this conversation"


class _Source:
    """A stand-in history source. ``vouches`` is whatever ``reconcile`` returns."""

    name = "tickets"
    windowed = False

    def __init__(self, vouches=None, *, raises: bool = False) -> None:
        self._vouches = vouches
        self._raises = raises
        self.calls = 0

    def available(self) -> str | None:
        return None

    def fetch(self, context: SourceContext):
        return []

    def reconcile(self, context: SourceContext):
        self.calls += 1
        if self._raises:
            raise ConnectionError("upstream is unreachable")
        return self._vouches


class _ContributeOnly:
    """A plugin written before ``reconcile()`` existed. Must still be safe."""

    name = "tickets"

    def available(self) -> str | None:
        return None

    def fetch(self, context: SourceContext):
        return []


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="muninn-history-"))
        self.st = open_store(self.tmp / "archive.db")
        self.addCleanup(self._cleanup)
        self.ctx = SourceContext(plugin="acme", source="tickets")

    def _cleanup(self) -> None:
        import shutil

        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, session_id: str, *, source: str = "acme.tickets") -> None:
        self.st.upsert_session({
            "session_id": session_id, "source": source, "provenance": "human",
            "text": PROSE, "words": len(PROSE.split()), "source_present": 1,
            "user_turns": 1, "assistant_turns": 1, "tool_uses": 0, "tool_results": 0,
            "origin": "raw",
        })
        self.st.commit()

    def _present(self, session_id: str) -> int:
        return self.st.get_session(session_id)["source_present"]

    # -- the diff ----------------------------------------------------------

    def test_a_vouched_session_stays_present(self) -> None:
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(self.st, _Source(["A"]), self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.RECONCILED)
        self.assertEqual(result.marked, ())
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)

    def test_an_unvouched_session_is_flagged_and_its_prose_survives(self) -> None:
        # The whole point. A remote session that vanished is not a session to
        # clean up — its transcript may exist nowhere else on earth.
        gone = self.ctx.namespaced_id("B")
        self._add(self.ctx.namespaced_id("A"))
        self._add(gone)
        result = ingest.reconcile_history_source(self.st, _Source(["A"]), self.ctx)
        self.assertEqual(result.marked, (gone,))
        self.assertEqual(self._present(gone), 0)
        self.assertEqual(self.st.session_text(gone), PROSE)
        self.assertEqual(self.st.count_sessions(), 2)

    def test_the_pass_is_idempotent(self) -> None:
        gone = self.ctx.namespaced_id("B")
        self._add(self.ctx.namespaced_id("A"))
        self._add(gone)
        source = _Source(["A"])
        ingest.reconcile_history_source(self.st, source, self.ctx)
        second = ingest.reconcile_history_source(self.st, source, self.ctx)
        # Nothing left to mark: already-flagged rows are excluded, so a daily
        # reconcile does not re-report the same loss forever.
        self.assertEqual(second.marked, ())
        self.assertEqual(self.st.session_text(gone), PROSE)

    def test_an_empty_vouch_is_honoured_literally(self) -> None:
        # "I enumerated and upstream holds nothing" is a real state, and the
        # source has ``None`` available to say the other thing. Flagging is
        # still non-destructive, and a later fetch sets source_present back.
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(self.st, _Source([]), self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.RECONCILED)
        self.assertEqual(len(result.marked), 1)
        self.assertEqual(self.st.session_text(self.ctx.namespaced_id("A")), PROSE)

    def test_a_key_the_archive_never_held_is_not_an_error(self) -> None:
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(
            self.st, _Source(["A", "never-ingested"]), self.ctx)
        self.assertEqual(result.marked, ())
        self.assertEqual(result.vouched, 2)

    # -- the three abstentions ---------------------------------------------

    def test_none_abstains(self) -> None:
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(self.st, _Source(None), self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.ABSTAINED)
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)

    def test_a_raising_source_abstains_rather_than_flagging_everything(self) -> None:
        # The failure this guard exists for: an unreachable remote must cost a
        # pass, never a mass reclassification of everything it ever contributed.
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(
            self.st, _Source(raises=True), self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.ABSTAINED)
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)

    def test_a_source_without_the_method_abstains(self) -> None:
        # A plugin written against API_VERSION 1, before reconcile() existed.
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(self.st, _ContributeOnly(), self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.ABSTAINED)
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)

    def test_a_windowed_source_is_refused_before_it_is_even_asked(self) -> None:
        # Invariant 6, the same one the export importers carry: absence from a
        # 30-day window cannot be distinguished from deletion.
        self._add(self.ctx.namespaced_id("A"))
        source = _Source([])
        source.windowed = True
        result = ingest.reconcile_history_source(self.st, source, self.ctx)
        self.assertIs(result.outcome, ReconcileOutcome.REFUSED_WINDOWED)
        self.assertEqual(source.calls, 0)
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)

    def test_the_outcome_distinguishes_what_a_count_cannot(self) -> None:
        # Three sources, three reasons, all producing zero marks. An integer
        # return would make them one fact and they need three responses.
        self._add(self.ctx.namespaced_id("A"))
        windowed = _Source([])
        windowed.windowed = True
        outcomes = {
            ingest.reconcile_history_source(self.st, src, self.ctx).outcome
            for src in (_Source(["A"]), _Source(None), windowed)
        }
        self.assertEqual(len(outcomes), 3)

    # -- scoping: a source speaks only for its own id space -----------------

    def test_another_plugins_sessions_are_untouched(self) -> None:
        other = SourceContext(plugin="other", source="tickets")
        self._add(other.namespaced_id("A"), source="other.tickets")
        result = ingest.reconcile_history_source(self.st, _Source([]), self.ctx)
        self.assertEqual(result.marked, ())
        self.assertEqual(self._present(other.namespaced_id("A")), 1)

    def test_local_sessions_are_untouched(self) -> None:
        # A contributed source has no standing to say anything about a Claude or
        # Codex transcript, and the local sweep owns those already.
        self._add("bare-local-session-id", source="claude")
        ingest.reconcile_history_source(self.st, _Source([]), self.ctx)
        self.assertEqual(self._present("bare-local-session-id"), 1)

    def test_an_underscore_in_a_source_name_is_not_a_like_wildcard(self) -> None:
        # ``_`` matches any single character in LIKE, so an unescaped prefix for
        # ``plugin:acme.a_c:`` would also select ``plugin:acme.abc:`` — one
        # plugin flagging another's sessions. The source string is
        # plugin-supplied and is not covered by the plugin-name regex.
        wildcard = SourceContext(plugin="acme", source="a_c")
        neighbour = SourceContext(plugin="acme", source="abc")
        self._add(neighbour.namespaced_id("A"), source="acme.abc")
        result = ingest.reconcile_history_source(self.st, _Source([]), wildcard)
        self.assertEqual(result.marked, ())
        self.assertEqual(self._present(neighbour.namespaced_id("A")), 1)

    def test_a_percent_in_a_source_name_is_not_a_like_wildcard(self) -> None:
        wildcard = SourceContext(plugin="acme", source="%")
        self._add(self.ctx.namespaced_id("A"))
        result = ingest.reconcile_history_source(self.st, _Source([]), wildcard)
        self.assertEqual(result.marked, ())
        self.assertEqual(self._present(self.ctx.namespaced_id("A")), 1)


if __name__ == "__main__":
    unittest.main()
