"""What you already know about what you are doing now.

Every other retrieval path in Muninn waits to be asked. `search`, `log`,
`correlate`, `show` — each answers a question, and each requires you to think of
the question first. That is the wrong shape for the most valuable thing an
archive holds, because the material you most need is the material you have
*forgotten you have*. You do not search for it, because you do not know it is
there.

Recall inverts that. It takes a place rather than a question — a repository —
and says what the archive knows about work there. Three kinds of knowing, in
descending order of how easily they are lost:

1. **Unfinished threads.** Sessions the enrichment pass judged ``ongoing`` or
   ``abandoned``. Nothing in Muninn has ever surfaced these, and they are the
   single most actionable thing in the corpus: work you started, did not
   finish, and have no reminder of.
2. **Prior work here.** What else has happened in this repository, most recent
   first. Cheap, and the answer to "have I touched this before".
3. **Related elsewhere.** The nearest sessions from *other* repositories, by
   embedding. This is the non-obvious one — the time you solved this same
   problem in a different project and would never have thought to look.

**Where "now" comes from.** Muninn ingests continuously, so it already knows
which session was written to most recently and where that session was working.
It does not need to ask Huginn, and deliberately does not: the raven protocol
forbids one raven presenting another's credential, and reading Huginn's API
would mean doing exactly that. The most recent session in Muninn's own archive
is a good enough answer and costs nothing.

**No model is called anywhere in this module.** Unfinished and prior-work are
SQL; related-elsewhere is a dot product against vectors that already exist.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from .store import Store

#: Enriched outcomes that mean "this is not finished". ``exploratory`` is
#: excluded on purpose: an exploration that ended is not a loose end, and
#: treating it as one would bury the real ones under everything you ever poked
#: at. ``fixed`` is likewise done.
UNFINISHED_OUTCOMES = ("ongoing", "abandoned")

#: Provenance classes worth recalling. Same two the enrichment gate and the
#: corpus survey use: a tool-invoked `claude -p` byproduct is not work you
#: remember doing, and a recall list full of them would be noise.
RECALLABLE = ("human", "subagent")

DEFAULT_LIMIT = 5

#: Below this cosine, "related" is not a claim worth making. Measured on a real
#: archive, the median between unrelated sessions is ~0.74 and the 90th
#: percentile ~0.87, so this sits above the bulk of ordinary resemblance while
#: staying well under the threshold a *duplicate* would need. Recall is allowed
#: to be suggestive where duplicate detection has to be certain: the cost of a
#: weak suggestion here is one uninteresting row, not a session hidden from
#: search.
RELATED_FLOOR = 0.88


@dataclass(frozen=True)
class Recalled:
    """One thing the archive knows, and why it is being mentioned."""

    session_id: str
    source: str
    cwd: str
    started_at: str
    words: int
    why: str                      #: unfinished | prior | related
    topic: str | None = None
    outcome: str | None = None
    summary: str | None = None
    score: float | None = None    #: only for ``related``

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass(frozen=True)
class Recall:
    """Everything recalled for one place, plus what could not be answered."""

    repo: str | None
    unfinished: list[Recalled] = field(default_factory=list)
    prior: list[Recalled] = field(default_factory=list)
    related: list[Recalled] = field(default_factory=list)
    #: Why a section is empty, when the reason is a missing capability rather
    #: than a missing answer. "No unfinished threads" and "enrichment has not
    #: run" look identical in an empty list and mean completely different
    #: things -- the same distinction `doctor` draws for calibration.
    unavailable: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.unfinished or self.prior or self.related)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "unfinished": [r.to_dict() for r in self.unfinished],
            "prior": [r.to_dict() for r in self.prior],
            "related": [r.to_dict() for r in self.related],
            "unavailable": self.unavailable,
        }


def _row(row: Any, why: str, score: float | None = None) -> Recalled:
    return Recalled(
        session_id=row["session_id"], source=row["source"] or "",
        cwd=row["cwd"] or "", started_at=row["started_at"] or "",
        words=int(row["words"] or 0), why=why,
        topic=row["topic"], outcome=row["outcome"], summary=row["summary"],
        score=score)


_SELECT = ("SELECT session_id, source, cwd, started_at, words, topic, outcome, summary "
           "FROM sessions")


def current_repo(st: Store, *, cwd: str | None = None) -> str | None:
    """Where "here" is: the caller's own directory, else the newest session's.

    **The caller's directory wins whenever the archive knows anything about
    it.** Whoever ran this is standing somewhere, and that states what they mean
    by "here" far better than a guess from ingest can. Ranking ingest first
    produced exactly the confusion you would predict: ``muninn recall`` run
    inside one repository answered about a different one, because an unrelated
    session elsewhere had been written to a moment earlier.

    The most-recent-session heuristic remains the fallback, for a caller with no
    useful directory — a menu fetch, a home directory, a checkout the archive
    has never seen. It is also why this asks Muninn's own ingest rather than
    Huginn: the raven protocol forbids one raven presenting another's
    credential, and the newest transcript answers it for free.

    Note the asymmetry with ``why``, which refuses ``cwd`` outright. There it
    would be *attribution* — claiming a session did work in a repository — and
    an agent launched in one repo routinely edits another. Here it is only
    intent, and a person's own location is the best evidence of that.
    """
    here = os.path.basename(os.path.abspath(cwd or os.getcwd()).rstrip("/\\"))
    if here:
        clause, params = _repo_clause(here)
        known = st.conn.execute(
            f"SELECT 1 FROM sessions WHERE provenance IN (?, ?){clause} LIMIT 1",
            (*RECALLABLE, *params)).fetchone()
        if known:
            return here
    row = st.conn.execute(
        f"{_SELECT} WHERE provenance IN (?, ?) AND cwd IS NOT NULL AND cwd != '' "
        "ORDER BY started_at DESC LIMIT 1", RECALLABLE).fetchone()
    return os.path.basename((row["cwd"] or "").rstrip("/\\")) if row else None


def _repo_clause(repo: str | None) -> tuple[str, list[Any]]:
    """Match a repository the way ``--repo`` does: the basename of ``cwd``.

    Reused rather than reimplemented so a repo means the same thing here as it
    does in `search` and `log`; ``basename`` is registered as a SQL function in
    ``store.open_store`` precisely so this cannot drift.
    """
    if not repo:
        return "", []
    return " AND LOWER(basename(cwd)) = LOWER(?)", [repo]


def unfinished(st: Store, repo: str | None, limit: int = DEFAULT_LIMIT) -> list[Recalled]:
    clause, params = _repo_clause(repo)
    rows = st.conn.execute(
        f"{_SELECT} WHERE provenance IN (?, ?) "
        f"AND outcome IN ({','.join('?' * len(UNFINISHED_OUTCOMES))}){clause} "
        "ORDER BY started_at DESC LIMIT ?",
        [*RECALLABLE, *UNFINISHED_OUTCOMES, *params, limit]).fetchall()
    return [_row(r, "unfinished") for r in rows]


def prior(st: Store, repo: str | None, limit: int = DEFAULT_LIMIT,
          exclude: set[str] | None = None) -> list[Recalled]:
    clause, params = _repo_clause(repo)
    rows = st.conn.execute(
        f"{_SELECT} WHERE provenance IN (?, ?){clause} "
        "ORDER BY started_at DESC LIMIT ?",
        [*RECALLABLE, *params, limit * 3]).fetchall()
    skip = exclude or set()
    return [_row(r, "prior") for r in rows if r["session_id"] not in skip][:limit]


def related_elsewhere(st: Store, repo: str | None, model: str,
                      limit: int = DEFAULT_LIMIT,
                      exclude: set[str] | None = None) -> list[Recalled]:
    """Nearest sessions from *other* repositories, by embedding.

    Restricted to elsewhere on purpose. Prior work in this repository is
    already its own section and is cheaper to find; what this adds is the case
    nobody thinks to look for -- the same problem solved in a different project.
    Leaving this repo in would fill the list with neighbours the reader can
    already see above.
    """
    from . import embed

    try:
        np = embed.require_numpy()
        means = embed.session_means(st, model)
    except Exception:
        return []
    if not means:
        return []

    clause, params = _repo_clause(repo)
    here = [r["session_id"] for r in st.conn.execute(
        f"SELECT session_id FROM sessions WHERE provenance IN (?, ?){clause}",
        [*RECALLABLE, *params]).fetchall()]
    anchors = [s for s in here if s in means]
    if not anchors:
        return []

    rows = {r["session_id"]: r for r in st.conn.execute(
        f"{_SELECT} WHERE provenance IN (?, ?)", RECALLABLE).fetchall()}
    # The repository's own centre of gravity, not one session's: recall is about
    # the place, and any single session would bias the answer to whatever was
    # done last.
    centre = embed.normalize(np.vstack([means[s] for s in anchors]).mean(axis=0))

    skip = set(here) | (exclude or set())
    scored = []
    for session_id, vector in means.items():
        if session_id in skip or session_id not in rows:
            continue
        score = float(np.dot(centre, vector))
        if score >= RELATED_FLOOR:
            scored.append((score, session_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [_row(rows[sid], "related", round(score, 3))
            for score, sid in scored[:limit]]


def recall(st: Store, *, repo: str | None = None, limit: int = DEFAULT_LIMIT,
           model: str | None = None) -> Recall:
    """Everything the archive knows about work in ``repo``.

    ``repo`` defaults to wherever the most recent session was working, which is
    what makes this answerable without being asked a question.
    """
    where = repo or current_repo(st)
    unavailable: dict[str, str] = {}

    found_unfinished = unfinished(st, where, limit)
    if not found_unfinished and not _has_enrichment(st):
        unavailable["unfinished"] = (
            "enrichment has not run, so no session has an outcome yet "
            "(`muninn doctor` shows the gate; the daemon enriches in the background)")

    seen = {r.session_id for r in found_unfinished}
    found_prior = prior(st, where, limit, exclude=seen)
    seen |= {r.session_id for r in found_prior}

    found_related: list[Recalled] = []
    if model:
        found_related = related_elsewhere(st, where, model, limit, exclude=seen)
        if not found_related and not _has_vectors(st, model):
            unavailable["related"] = (
                "the archive is not embedded yet, so nothing can be compared "
                "(`muninn serve` embeds in the background)")
    else:
        unavailable["related"] = "no embedding provider is installed"

    return Recall(repo=where, unfinished=found_unfinished, prior=found_prior,
                  related=found_related, unavailable=unavailable)


def _has_enrichment(st: Store) -> bool:
    row = st.conn.execute(
        "SELECT 1 FROM sessions WHERE outcome IS NOT NULL LIMIT 1").fetchone()
    return row is not None


def _has_vectors(st: Store, model: str) -> bool:
    row = st.conn.execute(
        "SELECT 1 FROM chunk_vectors WHERE model = ? LIMIT 1", (model,)).fetchone()
    return row is not None
