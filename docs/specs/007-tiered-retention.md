# Spec 007 — Tiered retention

**Status:** ready to implement after 001 and 005
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/archive-of-record.md`. This spec deletes
things, so that article is not optional — it is the constraint that makes this
safe.

## Why

The archive grows without bound. Measured: ~645 MB of FTS5 index per 23k
sessions, and a Codex-heavy or high-volume developer accrues far faster than the
development machine did. The machine this was built on sat at **99% full with 5.9
GB free**, so this is a real ceiling, not a theoretical one.

But this is also the most dangerous spec in the set, because it is the only one
that removes data on purpose.

## The constraint that outranks the byte budget

**Tiering must never drop the last surviving copy.**

Claude Code deletes transcripts after `cleanupPeriodDays`. Once
`source_present = 0`, the archive's prose is the only copy in existence. Such a
session's text is **permanently ineligible** for demotion, no matter how old, no
matter what the budget says. If the budget cannot be met without touching
irreplaceable prose, the correct behavior is to **report that it cannot be met**
— not to free space.

Stated as code: any candidate query for demotion must filter
`WHERE source_present = 1`, and a test must prove that a `source_present = 0`
session survives an aggressive budget.

## Tiers

| Tier | Holds | Demotion drops |
|---|---|---|
| **hot** | full prose + chunks + vectors | — |
| **warm** | full prose + facets; chunks dropped | FTS5 chunks (rebuildable from prose) |
| **cold** | facets + summary only; prose dropped | prose — **only if `source_present = 1`** |

Demotion order is deliberate: chunks first, because they are *derived* and can be
rebuilt from prose at any time, so warming is fully reversible. Prose is dropped
only in cold, only when the raw file still exists, and that is the one
irreversible step in the system.

`tool-invoked` prose is prunable at any tier — it is a reproducible byproduct of
another tool's call volume, and on the development corpus 3,534 such entries were
outright bug residue. See `.valholl/articles/provenance-classification.md`.

## Scope

**In:** `muninn/retention.py`, tier column + migration, `muninn retention` CLI
with a mandatory dry-run-first flow, `doctor` reporting, tests.

**Out:** automatic background demotion. **Retention never runs implicitly.** A
human or an explicit scheduled invocation triggers it; the indexer must not
demote as a side effect of ingesting.

## Files

| File | Action |
|---|---|
| `muninn/retention.py` | **new** — planning and execution, strictly separated |
| `muninn/store.py` | `sessions.tier` column + migration; demotion helpers |
| `muninn/cli.py` | `retention` subcommand |
| `tests/test_retention.py` | **new** |

## Design: plan, then apply

```python
@dataclass(frozen=True)
class RetentionPlan:
    target_bytes: int
    current_bytes: int
    demotions: tuple[Demotion, ...]     # (session_id, from_tier, to_tier, bytes_freed)
    protected: tuple[Protected, ...]    # (session_id, reason) — why it was NOT touched
    achievable: bool                    # False if the budget cannot be met safely
    shortfall_bytes: int

def plan(st, *, target_bytes: int, hot_days: int = 180,
         warm_days: int = 540) -> RetentionPlan

def apply(st, plan: RetentionPlan, *, actor: str = "cli") -> RetentionReceipt
```

`plan()` is **pure** — it reads and computes, never writes. `apply()` executes a
plan it is handed. That separation is what makes `--dry-run` trustworthy: the
thing you preview is literally the thing that runs.

`protected` is a first-class output, not a filter side effect. A user asking "why
is my archive still 3 GB" deserves the answer, and the answer is usually "1,200
sessions are irreplaceable." Same discipline as enumerated skips: a count cannot
be audited.

Every `apply()` writes a ledger row (`source_kind = "retention"`), so demotions
appear in the same append-only history as imports.

## CLI

```
muninn retention                          # show current tier distribution, plan nothing
muninn retention --plan --budget 1GB      # print the plan, change nothing
muninn retention --apply --budget 1GB     # requires an immediately prior identical --plan
muninn retention --rewarm SESSION_ID      # rebuild chunks from prose
```

`--apply` **must refuse** unless it can restate the plan and the plan is
`achievable`. When `achievable` is false, print the shortfall and the protected
count, and exit non-zero. Never partially apply toward an impossible budget.

`--rewarm` exists because warm→hot is reversible and users will want it after
searching cold history. Cold→warm is *not* offered: the prose is gone, and
pretending otherwise would be dishonest.

## Build order

1. `sessions.tier` column (default `'hot'`) + migration, and a `tier` index.
2. Byte accounting: per-session bytes for prose, chunks, and vectors. Use
   `length()` on the stored values rather than guessing from page counts.
3. `plan()` — pure, with the `source_present = 1` filter and the protected list.
4. `apply()` — transactional per session; a failure mid-run leaves earlier
   demotions committed and the ledger row honest about what happened.
5. `--rewarm`.
6. `doctor`: tier distribution table, total bytes, protected count, and the last
   retention run from the ledger.
7. Tests.

## Acceptance criteria

`tests/test_retention.py`:

1. **Irreplaceable prose is never dropped** — a `source_present = 0` session,
   older than `warm_days`, with an absurdly small budget, keeps its prose and
   appears in `protected` with reason `irreplaceable`.
2. **Impossible budget reports rather than over-deletes** — when only protected
   sessions remain, `achievable is False`, `shortfall_bytes > 0`, and `apply()`
   refuses.
3. **Chunks before prose** — a session eligible for both loses chunks first;
   assert prose is intact after a warm demotion.
4. **Warming is reversible** — demote to warm, `--rewarm`, and the rebuilt chunk
   count and searchability match the original exactly.
5. **Cold keeps facets and summary** — after cold demotion, `topic`, `outcome`,
   `summary` and `facets_json` are all still present.
6. **tool-invoked is prunable regardless** — a recent tool-invoked session can be
   pruned even inside `hot_days`.
7. **Plan is pure** — call `plan()` and assert the archive is byte-identical
   afterward (compare a full-table digest before and after).
8. **Plan/apply agreement** — `apply()` frees within a small tolerance of the
   plan's prediction, and demotes exactly the planned session ids.
9. **`--apply` without a matching plan refuses** — assert non-zero exit and no
   mutation.
10. **Ledger row written** — a retention run appends one `import_ledger` row with
    `source_kind = "retention"` and correct counts.
11. **Search still works across tiers** — a warm session is findable via its
    prose (rebuild-on-demand or excluded gracefully, but never a crash), and a
    cold session is findable by topic.
12. **Idempotence** — running the same plan twice frees nothing the second time
    and reports zero demotions.

Also: all prior contract tests pass unmodified; ruff clean.

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn retention                     # distribution only
uv run muninn retention --plan --budget 1GB # plan only, zero mutation
uv run muninn doctor                        # tier + protected reporting
```

Commit; do not push. **Do not run `--apply` against the real archive.**

## Guardrails

- **Never drop prose for a session with `source_present = 0`.** This is the one
  rule in the entire project that has no exception.
- **`plan()` never writes.** Test 7 proves it.
- **Never partially apply an unachievable plan.**
- **Never demote implicitly.** Ingest and the watcher must not call `apply()`.
- **Do not offer cold→warm rewarming.** The prose is gone; say so.
- **Do not add dependencies.**
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
- If the byte accounting is inaccurate enough that plans mispredict badly,
  **report it** rather than adding a fudge factor. A retention system that lies
  about what it will free is worse than none.
