# Spec 014 — Automatic embedding

**Status: implemented.** Depends on 006 (the `EmbeddingProvider` protocol,
`chunk_vectors`, and `muninn embed`) and 010 (the daemon that owns it).

**Read first:**
[`embedding-is-not-a-chore`](../../.valholl/articles/embedding-is-not-a-chore.md).
It is normative on the *why*, and it records the four decisions inside this spec
that a reasonable implementer would otherwise make differently.

## Why

Spec 006 shipped `muninn embed` as a separate command because the entire cost of
semantic search is generating the vectors once (1.9 ms to search 60k of them,
real money to make them). That was right about the cost and wrong about the
schedule: it left the *only* part of retrieval that a human had to remember to
run.

The consequence was not an error. It was a worse ranking, returned confidently,
for the newest sessions in the archive — because the un-embedded part is always
whatever was added since someone last remembered. See the article; this is
[`continuous-ingest-not-periodic`](../../.valholl/articles/continuous-ingest-not-periodic.md)'s
argument one layer up.

## Scope

**In:** a background embedding worker owned by `muninn serve`; one shared
definition of "pending"; a `doctor` backlog line; `--no-embed`.

**Out:** any change to how vectors are stored, searched or fused (spec 006 is
untouched); a rate limiter or token budget; embedding inside the import
transaction; the console.

## Files

| File | Action |
|---|---|
| `muninn/embedder.py` | **new** — `BackgroundEmbedder`: the thread, the failure policy, the stall guard |
| `muninn/embed.py` | `pending_chunks()`, `pending_count()` — the shared definition of the backlog |
| `muninn/daemon.py` | `Daemon(embed=True)`; start before the state file, stop before the store |
| `muninn/cli.py` | `serve --no-embed`; `cmd_embed` uses `pending_chunks`; `doctor` prints the backlog |
| `tests/test_embedder.py` | **new** |

## The design

```
muninn serve
├── indexer.watch()          main thread, own connection   (unchanged)
└── BackgroundEmbedder       worker thread, own connection (new)
        └── loop: pending_chunks → provider.embed → store_vectors → commit
```

One worker, one thread, one connection of its own. `sqlite3` connections default
to `check_same_thread=True`, but the real reason for a second connection is that
sharing one would serialise the ingest loop behind embedding writes — and ingest
waits on nothing. WAL plus the 30 s `busy_timeout` that `store.open_store`
already sets makes two writers ordinary, and `store_vectors` is an UPSERT, so a
concurrent `muninn embed` costs duplicated work and never a duplicated row.

### On by default, gated on a provider

`BackgroundEmbedder.start()` returns `False` — not raises — when no provider is
installed, which is the default build. The gate is therefore "did you already opt
into embeddings", answered by the provider's existence rather than by a second
flag that can disagree with it. This mirrors `ravenserve.attach` returning `None`
rather than costing the indexer its ingest (spec 009).

`muninn index --watch` never embeds: it is the foreground/debug path, and a debug
watcher that quietly starts spending against a hosted provider is a surprise in
the one mode someone runs to observe behaviour.

### Newest first

The worker orders its backlog by `sessions.started_at DESC`, the CLI keeps id
order, and a NULL `started_at` sorts **last** (missing data is not evidence of
recency). At ~7 chunks per session a fresh 1,156-session archive is ~8,000
pending chunks; id order would embed today's session after everything from
months ago, which is the opposite of the promise being made.

### Failure policy

| Failure | Handling |
|---|---|
| No provider | `start()` → `False`, logged at info. Not a warning: it is the default state. |
| `PolicyRefused` | Stop permanently, record `policy-refused`. A refusal is a decision, and retrying a metered API forever is expensive. |
| Anything else | Exponential backoff (5 s → 300 s), keep retrying. |
| Zero progress, non-empty backlog | Stop after 3 consecutive stalled passes, record `stalled`. |

Nothing propagates to the daemon. A daemon that dies because a provider had a bad
afternoon has traded a convenience for the data loss this project exists to
prevent.

### Ordering inside `Daemon.run`

Start **after** the descriptor and **before** the state file, for the reason
every other step in that sequence is where it is: what the daemon advertises
should already be true when it becomes discoverable. Also necessarily before
`indexer.watch()`, which never returns.

Stop **after** the descriptor and **before** the state file and the lock: the
worker holds its own connection, and it must stop writing before the lock is
released to a successor daemon that will immediately start a worker of its own.

## Acceptance criteria

One test per invariant.

1. A pass embeds the pending chunks; a second pass finds nothing.
2. `pending_count` agrees with what a pass finds.
3. Re-chunking a session makes it pending again (an appended session re-embeds).
4. A batch that succeeded is committed before a later one fails — asserted from a
   **second connection**, so it proves commit rather than connection state.
5. `_stop` mid-pass abandons the remaining batches without another provider call.
6. The newest session is embedded first; the CLI still uses id order; an undated
   session sorts last.
7. `--source` filtering still works through the shared query.
8. No provider → `start()` is `False`, reason `no-provider`, nothing raised.
9. `PolicyRefused` → stops after exactly one provider call, reason
   `policy-refused`.
10. A stalled provider stops after the stall limit with the backlog intact.
11. A transient failure is retried and then succeeds.
12. An unopenable archive does not raise out of the thread.
13. The thread drains a real backlog and `stop()` leaves it not running.
14. `stop()` is idempotent and safe before `start()`.
15. **Wiring:** `serve` calls `start()`; teardown calls `stop()`; a worker that
    did not start is not torn down; `--no-embed` constructs no worker;
    `index --watch` never embeds.
16. **End to end:** a real `Daemon` with a real thread embeds a pending session
    with nothing patched but the provider, and the worker is stopped by teardown.

### Mutation-verified, because a wiring test can pass vacuously

Every unit test in `tests/test_embedder.py` would still pass with
`Daemon.run`'s call to `start()` deleted — that call *is* the feature. Verified
by making the mutation and confirming three tests fail, including the end-to-end
one: this is the same trap `tests/test_daemon.py` documents for Huginn's first
signal tests, which all stayed green with the handler's call site removed.

## Definition of done

- `uv run pytest` green; `uv run ruff check muninn tests` clean.
- Green with **and without** the `[semantic]` extra installed: the default
  install has no numpy and no provider, and must still pass the whole suite.
- Against the real archive: `muninn doctor` shows a pending count, and a real
  `muninn serve` with a provider installed shrinks it without anyone typing
  `muninn embed`.

## Guardrails

- **Do not** embed inside the import transaction, or anywhere else on the ingest
  path. Ingest waits on nothing.
- **Do not** let any exception from a provider reach the daemon.
- **Do not** add a second definition of "pending". Both callers use
  `embed.pending_chunks`.
- **Do not** retry a `PolicyRefused`.
- **Do not** add a rate limit or token budget as a guessed constant — see
  [`derived-calibration`](../../.valholl/articles/derived-calibration.md). If the
  backlog's cost turns out to need shaping, **measure it and report the numbers**.
- **Do not modify** `tests/test_losslessness.py` or `tests/test_ledger.py`.
- Spec 006's guardrails still hold in full: no ANN index, no new default
  dependency, never mix models, never silently fall back to lexical.
