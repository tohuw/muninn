# Spec 003 — Background indexer

**Status:** ready to implement **after** spec 001 (import ledger) lands
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/continuous-ingest-not-periodic.md` and
`.valholl/articles/session-lifecycle-facts.md`. The second one contains measured
facts that contradict reasonable assumptions — read it before designing anything.

## Why

Measured 2026-07-30: the predecessor tool's cron-driven index was **7 days
stale with 148 unindexed transcripts**, including the session that discovered the
problem. Combined with Claude Code's 30-day sweep, anything created and pruned
between runs is lost with no error. A stale index also silently fails the thing
users most expect: searching for what they did this week.

## The constraint that shapes everything

`SessionEnd` shares a **1.5-second budget across all SessionEnd hooks**, does not
support `async: true`, and is **not guaranteed to fire** on crash, SIGKILL, or
window close.

Therefore: **the hook may only enqueue. It must never parse, never open the
archive, never import.** A hook that indexes will time out and delay the user's
shell.

## Architecture — three layers

```
SessionEnd hook  ──append──>  queue dir  ──drain──>  indexer
  (<50 ms, no db)             (one tiny                (parses, imports
                               JSON file                 via spec 001)
                               per job)
watcher (watchfiles) ─────────────────────────────────────┘
sweep (on start + interval) ──reconcile──> catches everything missed
```

Each layer exists because the one above it can fail:

- **Hook** is fast and eager, but can be missed entirely.
- **Watcher** catches sessions whose hook never fired, but misses events while
  the daemon is down, and can hit inotify limits.
- **Sweep** is the only layer that *closes* the guarantee. Not optional.

## Scope

**In:** `muninn/queue.py`, `muninn/indexer.py`, `muninn/hooks/` (hook entry
point + installer), `muninn index --watch`, sweep-on-start, lag reporting in
`doctor`, tests.

**Out:** export importers (spec 002), enrichment, embeddings, console, menubar.

## Files

| File | Action |
|---|---|
| `muninn/queue.py` | **new** — filesystem job queue |
| `muninn/indexer.py` | **new** — drain loop, watcher, sweep |
| `muninn/hooks/cli.py` | **new** — `muninn-hook` entry point |
| `muninn/hooks/install.py` | **new** — writes the settings.json hook block |
| `muninn/cli.py` | add `index --watch`, `install-hooks`; extend `doctor` |
| `pyproject.toml` | add `muninn-hook = "muninn.hooks.cli:main"` |
| `tests/test_queue.py` | **new** |
| `tests/test_indexer.py` | **new** |

## Build order

### Step 1 — `muninn/queue.py`

A directory of one JSON file per job. No database: the hook must not touch
SQLite (lock contention would blow the 1.5 s budget).

```python
QUEUE_DIR = STATE_DIR / "queue"

def enqueue(job: dict, *, queue_dir: Path = QUEUE_DIR) -> Path:
    """Atomically add a job. MUST be fast (<50 ms) and never raise.

    Write to <uuid>.tmp then os.replace() to <uuid>.json so a reader never
    observes a partial file.
    """

def drain(*, queue_dir: Path = QUEUE_DIR, limit: int | None = None) -> list[dict]:
    """Read and remove jobs, oldest first by mtime. Malformed files are moved
    to queue_dir/bad/ rather than deleted — they are evidence."""

def pending_count(*, queue_dir: Path = QUEUE_DIR) -> int
```

Job shape:

```json
{"v": 1, "kind": "session-end", "session_id": "…",
 "transcript_path": "…", "cwd": "…", "reason": "clear",
 "enqueued_at": "2026-07-31T04:12:00Z"}
```

### Step 2 — `muninn/hooks/cli.py`

`muninn-hook session-end` reads the payload JSON from **stdin**, enqueues, exits
0. Hard requirements:

- **Always exit 0**, even on malformed input or an unwritable queue. A failing
  hook must never disrupt the user's session.
- **No imports of `store`, `sqlite3`, `ingest`, or any parser.** Import only
  `json`, `sys`, `pathlib`, and `muninn.queue`. Keep startup minimal.
- Never print to stdout on success (stdout may be interpreted by the harness).
  Diagnostics go to stderr only.
- Wrap the entire body in `try/except BaseException` → `return 0`.

Add a `--self-test` flag that feeds a synthetic payload through and asserts a job
file appears, so the installer can verify the wiring.

### Step 3 — `muninn/hooks/install.py`

Idempotently add to `~/.claude/settings.json`:

```json
{"hooks": {"SessionEnd": [{"hooks": [{"type": "command",
  "command": "<abs path to muninn-hook> session-end"}]}]}}
```

Rules: read → merge → atomic write (temp + `os.replace`), preserving all
existing keys and any other SessionEnd entries. **Never** clobber the file.
Re-running must not duplicate the entry (match on the command string). Back up to
`settings.json.muninn-bak` before the first write. Do not set `async` — it is
unsupported for SessionEnd.

### Step 4 — `muninn/indexer.py`

```python
def drain_once(st, *, actor="hook") -> list[ImportReceipt]:
    """Drain the queue; import each job's transcript via spec 001."""

def sweep(st, roots: dict[str, Path], *, actor="sweep") -> list[ImportReceipt]:
    """Full reconciling scan. This is what closes the guarantee."""

def watch(st, roots: dict[str, Path], *, interval_s: float = 2.0,
          sweep_interval_s: float = 900.0) -> None:
    """Long-running loop: drain queue, react to file events, sweep periodically.

    Must run a sweep on startup BEFORE watching — events during downtime were
    missed and only a sweep recovers them.
    """
```

Use `watchfiles.watch` with a timeout so the loop can also service the queue and
periodic sweep. **On Linux, catch inotify-limit errors and fall back to polling**
with a logged warning rather than dying — thousands of transcript files is
exactly where that limit bites.

Rewrite detection: if a file's current size is **smaller** than the stored
`offset_bytes`, treat it as rewritten/truncated (`/compact`, rotation) and
re-parse whole. Never seek beyond EOF.

### Step 5 — CLI + doctor

- `muninn index --watch` runs `watch()`. Log a line per import receipt.
- `muninn install-hooks` runs the installer and reports what changed; `--check`
  reports status without writing.
- `doctor` gains a **queue** section: pending job count, count in `bad/`, age of
  the oldest pending job, and time since the last sweep. Warn when the oldest
  pending job exceeds 5 minutes (the drain is wedged) or `bad/` is non-empty.

## Acceptance criteria

`tests/test_queue.py`:

1. `enqueue` then `drain` round-trips a job.
2. Concurrent enqueues (threads) all survive; no job is lost or corrupted.
3. A malformed job file lands in `bad/` and does not break `drain`.
4. `enqueue` never raises when the queue dir is unwritable (chmod 0500 a temp
   dir; assert it returns rather than raising).

`tests/test_indexer.py`:

5. **Hook path**: enqueue a job for a real temp transcript, `drain_once`, assert
   the session is in the archive.
6. **Sweep catches a missed hook**: create a transcript with no job, run `sweep`,
   assert it is indexed.
7. **Sweep-before-watch**: assert `watch()` performs a sweep before its first
   event wait (inject a fake watcher or assert via a receipt list).
8. **Rewrite detection**: index a file, truncate it to a shorter valid transcript,
   re-index, assert prose matches the new content and no stale text lingers.
9. **Idempotence**: `drain_once` twice for the same job yields
   `outcome == "duplicate"` on the second (ledger, spec 001).
10. **Hook is cheap**: assert `muninn.hooks.cli` does not import `sqlite3` or
    `muninn.store` — check `sys.modules` after invoking `main()` in a subprocess.
    This is the test that keeps the 1.5 s budget safe.

Also:

- `muninn-hook session-end --self-test` exits 0 and creates a job.
- Installer is idempotent: run twice, assert exactly one entry and that unrelated
  settings keys survive byte-identical.
- `tests/test_losslessness.py` and `tests/test_ledger.py` pass unmodified.
- `uv run ruff check muninn tests tools` clean.

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn install-hooks --check
uv run muninn doctor            # shows queue + sweep sections
```

Commit; do not push.

## Guardrails

- **The hook must not touch SQLite.** This is the single most important
  constraint in this spec. Test 10 enforces it.
- **Never let a hook failure surface to the user.** Exit 0 always.
- **Do not delete anything on reconcile.** A missing source sets
  `source_present = 0` only. Re-read `archive-of-record.md` if tempted.
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
- **Do not add dependencies** beyond `watchfiles`, which is already declared.
- Codex has no known hook mechanism — its sources are watcher/sweep only. Do not
  invent one.
- If the 1.5 s budget or any documented fact here appears wrong, **measure it and
  report**; do not silently design around a guess.
