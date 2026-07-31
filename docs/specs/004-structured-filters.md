# Spec 004 — Structured filters and search quality

**Status:** ready to implement (independent of 001–003; touches `search` only)
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/corpus-measurements.md` — it contains the
measured latency numbers that dictate the design, including the one query shape
that degrades.

## Why

Search today is a bare FTS5 `MATCH` with no filters. The measurements say the
lexical layer is essentially free (0.1–1.9 ms on a real corpus), so the win is
not speed — it is *expressiveness*. `--file auth.py --since 2026-06` turns a
fuzzy recall problem into an exact query.

The one hard constraint from the measurements: **broad `OR` queries degrade
linearly** — 1.9 ms → 45.5 ms as chunks went 8k → 162k. So query expansion must
be **capped**, not unbounded. The naive "expand to 8 synonyms" approach is
precisely what degrades.

## Scope

**In:** composable filters, capped expansion, ranking, snippet quality,
`--json`, dedup by session, `muninn log`.

**Out:** embeddings and `--deep` LLM rerank (spec 006), enrichment fields
(`--outcome` is wired but will match nothing until spec 005 lands).

## Files

| File | Action |
|---|---|
| `muninn/query.py` | **new** — filter parsing, SQL building, expansion cap |
| `muninn/store.py` | extend `search()` to accept filters |
| `muninn/cli.py` | add flags to `search`; add `log` subcommand |
| `tests/test_query.py` | **new** |

## Filters

All composable, all AND-ed with each other and with the text query.

| Flag | Matches | Notes |
|---|---|---|
| `--repo NAME` | basename of `sessions.cwd` | substring, case-insensitive |
| `--branch NAME` | `sessions.branch` | exact |
| `--file PATH` | `session_files.basename` or suffix of `path` | the highest-value filter |
| `--tool NAME` | `session_tools.tool` | exact, case-insensitive |
| `--model NAME` | `sessions.model` | substring |
| `--provenance CLASS` | `sessions.provenance` | enum: human/tool-invoked/subagent |
| `--source NAME` | `sessions.source` | claude/codex/claude-cloud/… |
| `--since ISO` / `--until ISO` | `sessions.started_at` | accepts `2026`, `2026-07`, `2026-07-31` |
| `--outcome VALUE` | `sessions.outcome` | fixed/abandoned/ongoing (spec 005) |

Defaults worth stating: **`--provenance human` is NOT a default.** Subagent
transcripts hold real work (725,706 words on the dev corpus). Search everything
except `tool-invoked`, which is excluded by default and re-includable with
`--provenance tool-invoked`. Document this in `--help`.

## Query expansion — capped

```python
MAX_EXPANSION_TERMS = 4
```

Given a multi-word query, build an FTS5 expression that ANDs the terms (precise)
and, only if the result set is under a threshold, retries with OR across at most
`MAX_EXPANSION_TERMS` terms. Never expand beyond the cap. Never synonym-expand in
this spec — that belongs with the LLM path in spec 006.

Reuse the existing `store.fts_query()` sanitizer; it already strips FTS5
operators that would raise (a bare `NEAR/10` is a syntax error).

## Ranking

Order by `bm25(chunks)` ascending (SQLite returns negative scores; lower is
better), then `sessions.started_at DESC` as the tiebreak. Users overwhelmingly
want recent work first when relevance is comparable.

Dedup: a session may match on several chunks. Return **one row per session** with
its best-scoring snippet, plus `chunk_hits` count. Today the CLI dedups after the
fact and therefore under-fills `--limit`; fix that in SQL with a window function
or a grouped subquery.

## `muninn log`

```
muninn log [--repo NAME] [--since ISO] [--limit N]
```

A reverse-chronological timeline: date, session id prefix, source, repo, word
count, and topic once spec 005 lands. This is the "what did I do last week" view,
which is a different question from search and deserves its own command.

## Build order

1. `muninn/query.py`: a `Filters` frozen dataclass, `parse_date_prefix()`
   (`2026` → `2026-01-01`/`2026-12-31` bounds), and `build_search_sql(filters,
   match_expr)` returning `(sql, params)`.
2. Extend `store.search(query, *, filters=None, limit=20)`.
3. Wire CLI flags; add `--json` emitting a list of result objects.
4. `muninn log`.
5. Tests.

Build the SQL with **parameter binding only** — never string interpolation of
user input, even though this is a local database. A transcript containing SQL is
entirely normal and must not be able to affect a query.

## Acceptance criteria

`tests/test_query.py`:

1. **Each filter alone** narrows correctly — one test per filter, using a
   fixture archive with sessions that differ in exactly that dimension.
2. **Composition** — `--repo` + `--since` + text AND together; a session matching
   only two of three does not appear.
3. **Date prefixes** — `2026`, `2026-07`, `2026-07-31` all parse; `--since
   2026-07` includes 2026-07-01 and excludes 2026-06-30.
4. **`--file` matches basename and suffix** — `auth.py` matches
   `/x/y/auth.py`; `y/auth.py` matches too; `th.py` does **not** (suffix must
   align on a path segment boundary).
5. **tool-invoked excluded by default**, included with explicit
   `--provenance tool-invoked`.
6. **Subagents ARE searched by default** — assert a subagent-only match is
   returned with no flags.
7. **Expansion cap** — a 10-word query produces an expression with at most
   `MAX_EXPANSION_TERMS` OR-ed terms; assert by inspecting the built expression.
8. **Dedup fills the limit** — an archive where one session matches 5 chunks and
   4 others match 1 each returns 5 sessions for `--limit 5`, not 1.
9. **Ranking tiebreak** — two sessions with equal bm25 return newest first.
10. **SQL injection is inert** — a session whose prose contains
    `'; DROP TABLE sessions; --` is searchable and the table survives.
11. **Empty and operator-only queries** — `""`, `"AND"`, `"*"`, `"NEAR/10"`
    return empty results rather than raising.
12. **`--json` shape** — stable keys, one object per session.
13. **`muninn log`** — reverse chronological, respects `--repo` and `--since`.

Also: all prior contract tests pass unmodified; ruff clean.

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn search "extension point" --repo muninn --limit 5
uv run muninn log --limit 10
```

Commit; do not push.

## Guardrails

- **Do not exceed `MAX_EXPANSION_TERMS`.** The 45 ms measurement is why.
- **Do not default to `--provenance human`.** Subagent work is real work.
- **Parameter binding only.** No f-strings carrying user input into SQL.
- **Do not add dependencies.**
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
- If a filter needs a new column or index, add it via the same migration
  mechanism spec 001 establishes — do not require an archive rebuild.
