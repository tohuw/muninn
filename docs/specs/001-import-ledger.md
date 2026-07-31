# Spec 001 — Import ledger

**Status:** ready to implement
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/deterministic-imports.md`, then
`.valholl/articles/import-ledger-schema.md`. That second article is the
normative design; this spec is the build order and acceptance criteria.

## Why

Muninn's current `IngestResult` reports store-relative counters. Reproduced
against real code on 2026-07-31:

```
ACTOR A imports:      scanned=1 ingested=1 updated=0 unchanged=0
ACTOR B, 3 min later: scanned=1 ingested=0 updated=0 unchanged=1
```

An agent reading actor B's output says "nothing new" — false. And
`ingest_state`'s `ON CONFLICT DO UPDATE` destroyed actor A's evidence. Fix both.

## Scope

**In:** ledger tables, digest computation, per-item receipts, skip vocabulary,
import lock, `ImportReceipt` dataclass + JSON, rewiring `ingest_path`, `doctor`
reporting, `--json` output, tests.

**Out:** export importers (spec 002), background indexer (spec 003), enrichment,
embeddings, console. Do not build these.

## Files

| File | Action |
|---|---|
| `muninn/store.py` | add 3 tables, bump `SCHEMA_VERSION` to 2, add ledger methods |
| `muninn/receipt.py` | **new** — `ImportReceipt`, `SkipReason`, `Outcome` |
| `muninn/digest.py` | **new** — digest functions |
| `muninn/ingest.py` | rewire `ingest_path` to open/close a ledger row |
| `muninn/cli.py` | `--json` on `index`; ledger + incomplete-import in `doctor` |
| `tests/test_ledger.py` | **new** — invariant tests |
| `tests/test_losslessness.py` | must keep passing unchanged |

## Build order

Work in this order; each step ends with a green test run.

### Step 1 — `muninn/receipt.py`

```python
class Outcome(str, enum.Enum):
    IMPORTED = "imported"
    DUPLICATE = "duplicate"
    PARTIAL = "partial"
    REJECTED = "rejected"

class SkipReason(str, enum.Enum):
    NO_CONTENT = "no-content"
    UNPARSEABLE_JSON = "unparseable-json"
    UNKNOWN_SCHEMA = "unknown-schema"
    MISSING_ITEM_ID = "missing-item-id"
    MISSING_TIMESTAMP = "missing-timestamp"
    DUPLICATE_ITEM_IN_SOURCE = "duplicate-item-in-source"
    READ_ERROR = "read-error"
    UNSUPPORTED_SENDER_ROLE = "unsupported-sender-role"
    UNSUPPORTED_CONTENT_TYPE = "unsupported-content-type"
    SUPERSEDED_BY_RICHER_ORIGIN = "superseded-by-richer-origin"

SOURCE_KINDS = ("claude-transcripts", "codex-rollouts", "claude-export",
                "chatgpt-export", "prose-index")
```

Plus frozen dataclasses `SourceFacts`, `Delta`, `Skip`, `Attribution`, and
`ImportReceipt` with `.to_json()` emitting **exactly** the shape in the wiki
article's "Structured output" section, including `"schema":
"muninn.import-receipt/1"`.

Rules: `Delta` and `SourceFacts` are separate types and must not be merged into
one object — invariant 4 is enforced by the type system here, not by convention.

### Step 2 — `muninn/digest.py`

```python
def digest_items(source_kind: str, pairs: Iterable[tuple[str, str]]) -> str:
    """items-sha256:<hex> over source_kind then sorted "id\tupdated_at" lines.

    updated_at must be the SOURCE'S RAW representation, stringified verbatim —
    never a converted one. claude.ai gives an ISO string; ChatGPT gives an epoch
    float. Converting inside the digest boundary makes the digest depend on
    float formatting and therefore on the Python version.

    source_kind is in the preimage because both vendors ship a top-level array
    in a file named conversations.json; without it, two different exports can
    collide.
    """

def digest_file(path: Path) -> str:
    """file-sha256:<hex>, streamed in chunks. Recorded alongside digest_items:
    it catches upstream schema changes that leave (id, updated_at) identical."""

def digest_tree(root: Path, paths: Iterable[Path]) -> str:
    """tree-sha256:<hex> over sorted "relpath\tsize\tmtime_ns" lines.

    relpath is POSIX-style relative to root so the digest is stable across
    platforms and across moving the tree.
    """
```

All return their scheme prefix. `mtime_ns` via `stat().st_mtime_ns` (integer — do
not use float `st_mtime`, it is not reproducible across filesystems).

**Two identity schemes, deliberately.** Exports get a content digest; live
transcript trees get a `(path, offset)` cursor plus a tree digest for
attribution. Do not unify them — `(item_id → updated_at)` simply does not exist
for append-only JSONL, and pretending otherwise would fabricate identity.

### Step 3 — `muninn/store.py`

Add the three tables from the wiki article **verbatim**, plus one extra column on
`import_ledger` not shown there:

```sql
    file_digest   TEXT,   -- file-sha256 of the source file, when it is a file
```

Bump `SCHEMA_VERSION = 2` and add a migration: if an existing db reports version
1, run `CREATE TABLE IF NOT EXISTS` for the new tables and update `meta`.
Existing archives must keep working — do not require a rebuild.

Methods:

```python
def begin_import(self, *, actor: str, source_kind: str, source_ref: str | None,
                 source_digest: str, facts: SourceFacts) -> int
    """INSERT an in-flight row (finished_at NULL, outcome 'rejected'), return ledger_id.

    Pre-inserting outcome='rejected' means a crashed process leaves a row that
    reads as "did not succeed" rather than as success.
    """

def finish_import(self, ledger_id: int, *, outcome: Outcome, delta: Delta,
                  duplicate_of: int | None = None, error: str | None = None) -> None
    """Set finished_at + outcome + counts on THIS row only."""

def record_items(self, ledger_id: int, items: Iterable[tuple[str, str, str | None]]) -> None
    """(item_id, disposition, reason) rows. reason REQUIRED when skipped."""

def find_import_by_digest(self, source_digest: str) -> dict | None
    """Earliest COMPLETED row for this digest, or None."""

def acquire_import_lock(self, ledger_id: int, actor: str, pid: int) -> dict | None
    """None on success; the holder's row if already held."""

def release_import_lock(self, ledger_id: int) -> None
def incomplete_imports(self) -> list[dict]
def ledger_tail(self, limit: int = 10) -> list[dict]
```

Stale-lock handling: if the holder's `pid` is not alive (`os.kill(pid, 0)`
raises `ProcessLookupError`), the lock may be taken over, and the abandoned
ledger row is left as-is for `doctor` to report. Do **not** delete it.

### Step 4 — rewire `muninn/ingest.py`

`ingest_path(st, root, source, *, actor="cli")` becomes:

1. Discover paths, compute `digest_tree`, build `SourceFacts` (item_count =
   file count; span from session timestamps once parsed; `windowed=False` for
   live trees).
2. `begin_import(...)`, `acquire_import_lock(...)`. If held by a live pid:
   `finish_import(outcome=REJECTED)` and return a receipt whose
   `attribution` names the holder. Do not raise.
3. Parse and upsert as today, accumulating per-item dispositions.
4. If `find_import_by_digest` returned a prior completed row **and** the delta
   is all-unchanged: `outcome=DUPLICATE`, `duplicate_of=<that id>`.
   Otherwise `IMPORTED`, or `PARTIAL` when `skipped > 0` and the archive changed.
5. **Assert conservation** before finishing:
   `added + updated + unchanged + skipped == item_count`. Raise
   `ConservationError` if it disagrees — an uncounted drop is the exact failure
   this subsystem exists to prevent, and claudex had one (a falsy `uuid` was
   `continue`d before any counter incremented).
6. `record_items`, `finish_import`, `release_import_lock`, return `ImportReceipt`.

**Keep `IngestResult` as-is and return it too** (e.g. `ingest_path` returns the
receipt; expose `result` on it) so `tests/test_losslessness.py` keeps passing
without edits. If you must change that test file, stop and flag it — it is a
contract, not scaffolding.

Preserve exactly: `_reconcile_missing` never deletes; a windowed source may never
mark anything missing (invariant 6 — assert this, since export importers land in
spec 002).

### Step 5 — `muninn/cli.py`

- `muninn index --json` prints only the receipt JSON to stdout, nothing else.
- Human output prints **two labelled sections**, never blended:
  ```
  source   claude-transcripts · 384 items · 2026-07-01 .. 2026-07-30
  this run added 247 · updated 0 · unchanged 137 · skipped 0
  ```
- When `outcome == duplicate`, the human line must say so explicitly and name the
  prior import, e.g.
  `duplicate of import #14 (actor cli, finished 2026-07-30T21:15:17Z)`.
- `doctor` gains: last 5 ledger rows; a warning listing incomplete imports; and a
  warning if a lock is held by a dead pid.

## Acceptance criteria

`tests/test_ledger.py` must cover, one test per invariant:

1. **Append-only** — two imports of the same source produce two ledger rows; the
   first row's `started_at`/`actor` are byte-identical before and after the
   second run.
2. **Digest determinism** — same content in two different directories ⇒ same
   `items-sha256`; a changed `updated_at` ⇒ different digest.
3. **Duplicate by identity** — the actor-B scenario yields
   `outcome == "duplicate"` and `duplicate_of == <A's ledger_id>`, and
   **explicitly assert** the receipt cannot be read as an empty source
   (`source.item_count > 0`).
4. **Separation** — `ImportReceipt.to_json()` has disjoint `source` and `delta`
   objects; assert no delta key appears under `source`.
5. **Skips enumerated** — `delta.skipped == len(receipt.skips)`, every reason is
   a `SkipReason` member.
6. **Windowed safety** — with `windowed=True`, no session gets
   `source_present = 0`.
7. **Lock serializes** — a second import while a live lock is held returns
   `outcome == "rejected"` with the holder's actor.
8. **Errors are class names** — force a parser exception; assert
   `ledger.error` matches `^[A-Za-z_][A-Za-z0-9_]*$` and contains no spaces.
9. **Crash visible** — a row with `finished_at IS NULL` appears in
   `incomplete_imports()` and in `doctor` output.
10. **Migration** — open a v1 archive (build one by creating tables from the
    current schema minus the ledger), run `open_store`, confirm the ledger tables
    appear and existing sessions are intact.
11. **Conservation** — a source with one good item and one unparseable item
    yields `added + updated + unchanged + skipped == item_count`; and a
    deliberately mis-counted import raises `ConservationError`.
12. **No id coercion** — an item with a missing/empty id produces a
    `missing-item-id` skip, and **no** session row is created for it. Assert two
    id-less items produce two skips, not one overwritten row.
13. **Digest stability across representations** — `digest_items` over the same
    pairs is identical when computed twice in different working directories, and
    **differs** when only `source_kind` differs.

Also required:

- `tests/test_losslessness.py` passes **unmodified** (14 tests).
- `uv run ruff check muninn tests tools` clean.
- The reproduction from "Why" now prints `duplicate of import #1` for actor B.
  Include this as a test.

## Definition of done

```sh
uv run python -m unittest discover tests -v     # all pass, incl. 10 new
uv run ruff check muninn tests tools            # clean
uv run muninn index --json | python3 -m json.tool   # valid receipt
uv run muninn doctor                            # shows ledger + no warnings
```

Then commit. Do not push; leave that for review.

## Guardrails

- **Do not** modify `tests/test_losslessness.py`.
- **Do not** add dependencies. Stdlib `hashlib`, `sqlite3`, `enum`,
  `dataclasses` only.
- **Do not** put exception messages, transcript text, paths from other users, or
  credentials into ledger columns or receipts. `error` holds a class name.
- **Do not** implement export importers, the watcher, or hooks — later specs.
- If a requirement here contradicts the wiki article, **the wiki wins**; flag the
  contradiction rather than choosing silently.
- If you find yourself wanting to change an invariant to make a test pass, stop
  and report it. The invariants are the deliverable.
