# Spec 002 — Export importers (claude.ai and ChatGPT)

**Status:** ready to implement **after** spec 001 (import ledger) lands
**Owner of design:** planned by Opus, implemented by Sonnet
**Read first:** `.valholl/articles/deterministic-imports.md` and
`.valholl/articles/import-ledger-schema.md`. This spec is where the incident in
those articles actually happened, so their requirements are not optional here.

## Why

Vendor data exports are the only route to cloud conversations (claude.ai,
ChatGPT), which never touch the local transcript directories. They are also the
source type that caused the false *"the export contained nothing new"* claim.

Everything in this spec is downstream of one fact: **a claude.ai export is a
~30-day window, not a complete history.** Absence of a conversation from an
export is not evidence of anything.

## Scope

**In:** `muninn/exports.py` with parsers for both vendor formats, discovery,
zip handling, `muninn import` CLI, `--verify-safe-to-delete`, tests.

**Out:** the watcher/hooks (spec 003), enrichment, embeddings, console.

## Files

| File | Action |
|---|---|
| `muninn/exports.py` | **new** — detection, parsing, import |
| `muninn/cli.py` | add `import` subcommand |
| `tests/test_exports.py` | **new** |
| `tests/fixtures/` | **new** — small synthetic exports of both shapes |

## Source formats (verified against the predecessor parsers)

Both vendors ship a **top-level JSON array** in a file named
`conversations.json`. The filename alone is ambiguous; discriminate structurally.

### claude.ai — `claude-export`

Discovery: `~/Downloads/data-*-batch-0000/conversations.json` (a directory;
already unzipped).

Per conversation:

| field | use |
|---|---|
| `uuid` | **item id.** Missing/empty ⇒ `missing-item-id` skip |
| `name` | title (may be `""`) |
| `created_at` | start |
| `updated_at` | **raw digest input** and change detection (ISO string) |
| `chat_messages[]` | messages |

Per message: `sender` (`human`/`assistant` only), `created_at`, `text`, or
`content[]` blocks of `{"type":"text","text":…}`; `attachments[]` with
`extracted_content` and `file_name`.

Discriminator: presence of `chat_messages` / `uuid`.

### ChatGPT — `chatgpt-export`

Discovery: `~/Downloads` non-recursive — a directory containing
`conversations.json`, a bare `conversations.json`, or a `.zip` whose name
contains `chatgpt` or `openai`. For zips, pick the **shortest** path ending in
`conversations.json` (the shallowest one).

Per conversation:

| field | use |
|---|---|
| `id` → `conversation_id` → `uuid` | **item id.** All missing ⇒ `missing-item-id` skip. **Never coerce to `"unknown"`** — codexdex did, and every id-less conversation overwrote the last |
| `title` / `name` | title |
| `create_time` / `update_time` | epoch **floats**; `created_at`/`updated_at` are string fallbacks |
| `mapping` | **messages are a graph**, dict of node-id → node |

`mapping` nodes flatten to a linear stream sorted by
`node["message"]["create_time"]`. Do not attempt parent/child traversal;
branches and regenerations collapse by timestamp, matching the predecessor. Per
node: `message.author.role` (`user`/`assistant`), `message.content.parts[]` —
**only `str` parts** contribute prose; dict parts are multimodal and yield
`unsupported-content-type`.

Discriminator: presence of `mapping` in any of the first 20 items.

## Build order

### Step 1 — detection and discovery

```python
def detect_kind(payload: list) -> str | None:
    """'claude-export' | 'chatgpt-export' | None. Structural, never filename."""

def find_exports(downloads: Path) -> list[ExportCandidate]:
    """Newest first by mtime. Handles dir / bare json / zip."""

def load_payload(path: Path) -> tuple[list, str | None]:
    """Return (payload, file_digest). Handles zip extraction in-memory."""
```

### Step 2 — parsers

```python
def parse_claude_export(payload: list) -> tuple[list[ParsedSession], list[Skip], list[tuple[str,str]]]
def parse_chatgpt_export(payload: list) -> tuple[list[ParsedSession], list[Skip], list[tuple[str,str]]]
```

Each returns parsed sessions, enumerated skips, and the `(item_id,
raw_updated_at)` pairs for `digest_items`. Reuse `sources.ParsedSession` —
cloud conversations become sessions with `source` of `claude-cloud` /
`chatgpt-cloud` and provenance `human`.

Rules:

- **Every** input item appears exactly once across sessions + skips.
  Conservation is asserted by spec 001; make it true here.
- `raw_updated_at` is `str(value)` of the source's own field — do **not**
  normalize epoch floats before hashing.
- Attachment text is included, truncated at 30,000 chars (the predecessor's
  limit, chosen because 200K-char single-message attachments cannot be split at
  turn boundaries).
- A conversation whose messages all yield nothing is `no-content` — but if the
  cause was an unsupported role or content type, prefer *that* reason. "Empty"
  and "we could not read it" must be distinguishable; conflating them is what
  made the incident's `4 empty` unauditable.

### Step 3 — import entry point

```python
def import_export(st, path: Path, *, actor: str = "cli") -> ImportReceipt
```

Flow: load → detect kind → `digest_items(kind, pairs)` + `digest_file` →
`begin_import(windowed=True for claude-export)` → lock → upsert sessions →
`record_items` → assert conservation → `finish_import`.

`windowed=True` for `claude-export` unconditionally. For `chatgpt-export`, set
`windowed=True` as well unless proven otherwise — assuming completeness is the
dangerous direction.

**Change detection is per item:** an existing session whose stored
`ended_at` equals the source's `updated_at` is `unchanged`; otherwise `updated`.

### Step 4 — CLI

```
muninn import [PATH]                    # auto-discovers newest if omitted
muninn import PATH --json
muninn import PATH --verify-safe-to-delete
```

Human output is **two labelled sections**, per invariant 4:

```
source   claude-export · 65 items · 2026-06-30 .. 2026-07-30 · windowed (~30d)
this run added 0 · updated 0 · unchanged 61 · skipped 4
         duplicate of import #14 (actor cli, finished 2026-07-30T21:15:17Z)
skips    4 items: no-content ×2, unsupported-content-type ×2
```

When `windowed`, print a standing caveat: *absence from a windowed export does
not indicate upstream deletion.*

`--verify-safe-to-delete` answers **only** from the ledger, per spec 001. On
refusal, print the reason. Never emit a bare "yes".

## Acceptance criteria

`tests/test_exports.py`, using synthetic fixtures (no real exports on this
machine — build them):

1. **Detection** — a claude.ai-shaped payload detects as `claude-export`; a
   ChatGPT-shaped one as `chatgpt-export`; garbage as `None`. Assert a
   claude-shaped payload is **not** detected as ChatGPT (the predecessor has this
   exact test).
2. **The incident, reproduced and fixed** — import a 3-item export twice as two
   different actors. Second run: `outcome == "duplicate"`,
   `duplicate_of == first ledger_id`, `source.item_count == 3`, and the receipt's
   attribution names actor A. Assert explicitly that no field can be read as
   "the export contained nothing".
3. **Windowed safety** — after importing a windowed export that omits a
   previously-known session, that session's `source_present` is unchanged.
4. **Conservation** — an export with one good, one id-less, one empty and one
   multimodal-only item yields 1 added + 3 skipped == 4 items.
5. **No id coercion** — two id-less conversations produce two
   `missing-item-id` skips and zero sessions.
6. **Digest ignores byte layout** — re-serialize the same payload with different
   key order and indentation; `digest_items` is identical, `digest_file` differs.
7. **Digest includes kind** — identical pairs under the two kinds give different
   digests.
8. **Epoch floats are not normalized pre-hash** — a ChatGPT export whose
   `update_time` is `1785400000.5` digests identically across two runs in
   separate processes.
9. **Zip handling** — a zip with `export/conversations.json` and a deeper decoy
   picks the shallowest; a zip without one is `rejected` with a reason, not a
   crash.
10. **Graph flattening** — a `mapping` with three nodes out of timestamp order
    produces prose in timestamp order.
11. **Attachment truncation** — a 50,000-char `extracted_content` is included
    truncated, and the session is not skipped.
12. **Skip reasons are distinguishable** — a voice-only conversation yields
    `unsupported-content-type`, not `no-content`.

Also: `test_losslessness.py` and `test_ledger.py` pass unmodified; ruff clean.

## Definition of done

```sh
uv run python -m unittest discover tests -v
uv run ruff check muninn tests tools
uv run muninn import --help
```

Commit; do not push.

## Guardrails

- **Never coerce a missing item id.** Skip it.
- **Never normalize timestamps before hashing.**
- **Never mark a session missing from a windowed source.**
- **Never print a store-relative counter as the headline.** The source's facts
  and this run's delta are separate sections, always.
- **Do not** modify `tests/test_losslessness.py` or `tests/test_ledger.py`.
- **Do not** add dependencies — `zipfile` and `json` are stdlib.
- **Do not** read the user's real `~/Downloads` in tests; use fixtures and
  `tmp_path`.
- If a vendor field is absent where this spec says it exists, treat it as a
  counted skip and **report the discrepancy** — the formats are not contracts.
