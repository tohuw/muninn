"""Structured filters, capped expansion, and ranked/deduped SQL for search.

Everything that decides *what SQL search() runs* lives here rather than in
store.py, so specs 002/003 (background indexer, export importers) — which are
being implemented concurrently in sibling worktrees and also touch store.py —
have as little surface to collide with as possible. store.py only gets a thin
call into this module.

Two constraints from .valholl/articles/corpus-measurements.md drive the shape
of everything below:

- **Broad OR queries degrade linearly** (1.9 ms at 8k chunks -> 45.5 ms at
  162k). ``MAX_EXPANSION_TERMS`` is not a tuning knob; it is the fix for a
  measured regression, so nothing in this module may build an OR-expression
  wider than the cap, no matter how many words the user typed.
- **FTS5 itself is fast at any plausible scale.** So filters are cheap to add;
  the discipline that matters is parameter binding, not query count.

Parameter binding only, everywhere in this module. A transcript may contain
the literal text of a SQL statement (a user pasting an error, a shell history
line) and that must never be able to influence the query it is found by — see
docs/specs/004-structured-filters.md, "Guardrails".
"""
from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from typing import Any

# The measured breakpoint: broad OR queries went 1.9ms -> 45.5ms as the corpus
# grew 8k -> 162k chunks (corpus-measurements.md). Expansion must never widen
# beyond this many OR-ed terms, regardless of how many words the user typed.
MAX_EXPANSION_TERMS = 4


@dataclass(frozen=True)
class Filters:
    """One field per `--flag`, all optional, all AND-ed together.

    ``provenance=None`` is not "no filter" — it means "apply the default",
    which excludes tool-invoked sessions. An explicit ``provenance="tool-invoked"``
    is how a caller opts back in. See ``_provenance_clause`` below and
    docs/specs/004-structured-filters.md ("Defaults worth stating").
    """

    repo: str | None = None
    branch: str | None = None
    file: str | None = None
    tool: str | None = None
    model: str | None = None
    provenance: str | None = None
    source: str | None = None
    since: str | None = None
    until: str | None = None
    outcome: str | None = None


def parse_date_prefix(value: str) -> tuple[str, str]:
    """Parse a partial ISO date into inclusive ``(start, end)`` bounds.

    ``"2026"`` -> the whole year; ``"2026-07"`` -> the whole month;
    ``"2026-07-31"`` -> that single day. Sessions store ``started_at`` as a
    full ISO8601 timestamp, so the bounds carry time components: a bare
    ``--since 2026-07`` must include a session that started at
    ``2026-07-01T00:00:00`` and exclude one from ``2026-06-30T23:59:59``.
    """
    value = value.strip()
    parts = value.split("-")
    try:
        year = int(parts[0])
        if len(parts) == 1:
            start = dt.date(year, 1, 1)
            end = dt.date(year, 12, 31)
        elif len(parts) == 2:
            month = int(parts[1])
            start = dt.date(year, month, 1)
            end = dt.date(year, month, calendar.monthrange(year, month)[1])
        elif len(parts) == 3:
            month, day = int(parts[1]), int(parts[2])
            start = dt.date(year, month, day)
            end = start
        else:
            raise ValueError(f"not a date prefix: {value!r}")
    except (ValueError, IndexError) as exc:
        raise ValueError(f"not a date prefix: {value!r}") from exc
    return f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59.999999"


def expand_terms(match_expr: str, max_terms: int = MAX_EXPANSION_TERMS) -> str:
    """Build a capped OR-expression from an already-sanitized match expression.

    ``match_expr`` is the output of ``store.fts_query()`` — space-joined,
    already-quoted terms (FTS5's default join is AND, which is the "precise"
    query). This builds the fallback: the same terms OR-ed, capped at
    ``max_terms`` regardless of how many the query actually had. Bare
    ``AND``/``OR``/``NOT`` operator tokens are dropped rather than OR-ed,
    since re-ORing an explicit operator would change the user's own intent
    rather than just broadening recall.
    """
    terms = [t for t in match_expr.split() if t not in ("AND", "OR", "NOT")]
    return " OR ".join(terms[:max_terms])


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards in a value that will be bound (never interpolated).

    Binding already makes this injection-safe; escaping is purely so a path or
    model name containing a literal ``%`` or ``_`` cannot accidentally act as
    a wildcard in the LIKE pattern built around it.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_clauses(filters: Filters) -> tuple[list[str], list[Any]]:
    """Shared WHERE fragments for both search() and log().

    Returns fragments (no leading ``AND``) and their params, in the exact
    order the fragments are joined — the caller must not reorder one list
    without the other, since every ``?`` is positional.
    """
    where: list[str] = []
    params: list[Any] = []

    if filters.repo:
        # "basename of sessions.cwd" (docs/specs/004): a custom SQL function
        # is registered in store.open_store() rather than hand-rolling
        # last-path-segment extraction in SQL, which SQLite has no builtin
        # for and which gets subtly wrong on edge cases (trailing slash, no
        # slash at all) that os.path.basename already handles correctly.
        where.append("LOWER(basename(s.cwd)) LIKE LOWER(?) ESCAPE '\\'")
        params.append(f"%{_escape_like(filters.repo)}%")
    if filters.branch:
        where.append("s.branch = ?")
        params.append(filters.branch)
    if filters.model:
        where.append("LOWER(s.model) LIKE LOWER(?) ESCAPE '\\'")
        params.append(f"%{_escape_like(filters.model)}%")
    if filters.source:
        where.append("s.source = ?")
        params.append(filters.source)
    if filters.outcome:
        # Wired now, populated by spec 005. Filtering on a column that is
        # always NULL today is correct behavior, not a bug: it must match
        # nothing rather than raise. See docs/specs/004, "Out of scope".
        where.append("s.outcome = ?")
        params.append(filters.outcome)
    if filters.since:
        start, _end = parse_date_prefix(filters.since)
        where.append("s.started_at >= ?")
        params.append(start)
    if filters.until:
        _start, end = parse_date_prefix(filters.until)
        where.append("s.started_at <= ?")
        params.append(end)

    if filters.provenance:
        where.append("s.provenance = ?")
        params.append(filters.provenance)
    else:
        # The default: search everything EXCEPT tool-invoked. Subagent
        # transcripts are real work (251 sessions, 732,949 words on the real
        # corpus per corpus-measurements.md) and must be searched by default;
        # only the reproducible/bug-residue class is opted out, and only
        # until the caller explicitly asks for it back with --provenance
        # tool-invoked.
        where.append("s.provenance != 'tool-invoked'")

    if filters.file:
        # Basename match ("auth.py" == session_files.basename) OR a suffix
        # aligned on a path segment boundary ("y/auth.py" matches
        # "/x/y/auth.py" because it is preceded by "/"; "th.py" must NOT
        # match "auth.py" even though it is a literal string suffix). The
        # trailing "/" in the LIKE pattern is what enforces the boundary.
        where.append(
            "EXISTS (SELECT 1 FROM session_files f WHERE f.session_id = s.session_id "
            "AND (f.basename = ? OR f.path = ? OR f.path LIKE ? ESCAPE '\\'))"
        )
        params.extend([filters.file, filters.file, f"%/{_escape_like(filters.file)}"])

    if filters.tool:
        where.append(
            "EXISTS (SELECT 1 FROM session_tools t WHERE t.session_id = s.session_id "
            "AND LOWER(t.tool) = LOWER(?))"
        )
        params.append(filters.tool)

    return where, params


def build_search_sql(filters: Filters | None, match_expr: str,
                     limit: int) -> tuple[str, list[Any]]:
    """Build the ranked, deduped, filtered search query.

    Dedup is done in SQL (a window function ranks each session's chunk hits by
    bm25 and keeps only the best), not in Python after the fact — the old CLI
    deduped post-hoc and therefore under-filled ``--limit`` whenever one
    session matched several chunks (docs/specs/004, "Ranking"). ``chunk_hits``
    survives the dedup as a count so a caller can tell "one strong hit" from
    "five hits, best one shown" without a second query.

    Ranking is ``bm25(chunks)`` ascending (SQLite's bm25 returns negative
    scores; lower is better) then ``started_at`` descending as the tiebreak —
    users overwhelmingly want recent work first when relevance is comparable.

    Every value the caller controls is bound as a ``?`` parameter, including
    ``match_expr`` itself; nothing here f-strings user input into SQL text.
    """
    filters = filters or Filters()
    where, where_params = _filter_clauses(filters)
    where_sql = f"AND {' AND '.join(where)}" if where else ""

    sql = (
        "WITH matched AS ("
        "  SELECT c.session_id AS session_id,"
        "         bm25(chunks) AS score,"
        "         snippet(chunks, 2, '[', ']', ' ... ', 16) AS excerpt"
        "  FROM chunks c"
        "  WHERE chunks MATCH ?"
        "),"
        "ranked AS ("
        "  SELECT m.session_id AS session_id, m.score AS score, m.excerpt AS excerpt,"
        "         s.source AS source, s.provenance AS provenance,"
        "         s.started_at AS started_at, s.cwd AS cwd, s.words AS words,"
        f"        ROW_NUMBER() OVER (PARTITION BY m.session_id ORDER BY m.score ASC) AS rn,"
        f"        COUNT(*) OVER (PARTITION BY m.session_id) AS chunk_hits"
        "  FROM matched m"
        "  JOIN sessions s ON s.session_id = m.session_id"
        f"  WHERE 1=1 {where_sql}"
        ")"
        "SELECT session_id, source, provenance, started_at, cwd, words, excerpt, score, chunk_hits"
        " FROM ranked"
        " WHERE rn = 1"
        " ORDER BY score ASC, started_at DESC"
        " LIMIT ?"
    )
    params: list[Any] = [match_expr, *where_params, limit]
    return sql, params


def build_log_sql(filters: Filters | None, limit: int) -> tuple[str, list[Any]]:
    """Build the reverse-chronological ``muninn log`` query.

    Shares ``_filter_clauses`` with search() so ``--repo``/``--since`` behave
    identically in both commands rather than drifting into two subtly
    different implementations of "matches this repo".
    """
    filters = filters or Filters()
    where, where_params = _filter_clauses(filters)
    where_sql = f"AND {' AND '.join(where)}" if where else ""

    sql = (
        "SELECT s.session_id AS session_id, s.source AS source, s.provenance AS provenance,"
        "       s.started_at AS started_at, s.cwd AS cwd, s.words AS words, s.topic AS topic"
        " FROM sessions s"
        f" WHERE 1=1 {where_sql}"
        " ORDER BY s.started_at DESC"
        " LIMIT ?"
    )
    params: list[Any] = [*where_params, limit]
    return sql, params
