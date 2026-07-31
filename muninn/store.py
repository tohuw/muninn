"""SQLite storage: the archive of record.

Design constraints that are not negotiable, and why:

- **Nothing here deletes session prose.** For much of a corpus this database is
  the only surviving copy (Claude Code sweeps transcripts after
  ``cleanupPeriodDays``, default 30). Retention tiering, when it lands, drops
  chunk text only for sessions whose raw source still exists.
- **Full prose is stored verbatim** in ``sessions.text``, separately from the
  FTS5 ``chunks`` table. Chunks are a derived search index and can always be
  rebuilt; the prose cannot.
- **FTS5 is an index, not storage.** The chunks table is declared
  ``content=''``-free (a normal FTS5 table) for snippet support, but the
  authoritative text is always ``sessions.text``.

See .valholl/articles/archive-of-record.md.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .query import Filters, build_log_sql, build_search_sql, expand_terms
from .receipt import Delta, Outcome, SourceFacts

SCHEMA_VERSION = 2

# Chunking defaults. Real values come from calibration; these are fallbacks so
# the store works before a survey has ever run.
DEFAULT_CHUNK_WORDS = 400
DEFAULT_CHUNK_STRIDE = 320

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id       TEXT PRIMARY KEY,
    source           TEXT NOT NULL,           -- claude | codex | ...
    provenance       TEXT NOT NULL,           -- human | tool-invoked | subagent
    parent_id        TEXT,                    -- for subagents
    cwd              TEXT,
    branch           TEXT,
    model            TEXT,
    title            TEXT,
    started_at       TEXT,                    -- ISO8601
    ended_at         TEXT,
    duration_s       REAL,
    user_turns       INTEGER NOT NULL DEFAULT 0,
    assistant_turns  INTEGER NOT NULL DEFAULT 0,
    tool_uses        INTEGER NOT NULL DEFAULT 0,
    tool_results     INTEGER NOT NULL DEFAULT 0,
    words            INTEGER NOT NULL DEFAULT 0,
    tokens           INTEGER,
    text             TEXT NOT NULL DEFAULT '',  -- authoritative prose
    source_path      TEXT,                      -- last known raw path
    source_present   INTEGER NOT NULL DEFAULT 1, -- 0 once the raw file is gone
    origin           TEXT NOT NULL DEFAULT 'raw', -- raw | prose-index
    ingested_at      TEXT,
    updated_at       TEXT,
    -- enrichment (populated later, nullable by design)
    topic            TEXT,
    outcome          TEXT,
    summary          TEXT,
    facets_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_source     ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_provenance ON sessions(provenance);
CREATE INDEX IF NOT EXISTS idx_sessions_started    ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_cwd        ON sessions(cwd);
CREATE INDEX IF NOT EXISTS idx_sessions_branch     ON sessions(branch);
CREATE INDEX IF NOT EXISTS idx_sessions_outcome    ON sessions(outcome);

-- Files touched and tools used, normalized for --file / --tool filters.
CREATE TABLE IF NOT EXISTS session_files (
    session_id TEXT NOT NULL,
    path       TEXT NOT NULL,
    basename   TEXT NOT NULL,
    PRIMARY KEY (session_id, path)
);
CREATE INDEX IF NOT EXISTS idx_files_basename ON session_files(basename);

CREATE TABLE IF NOT EXISTS session_tools (
    session_id TEXT NOT NULL,
    tool       TEXT NOT NULL,
    uses       INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, tool)
);
CREATE INDEX IF NOT EXISTS idx_tools_tool ON session_tools(tool);

-- Incremental ingest bookkeeping: byte offsets so append-only transcripts are
-- tailed rather than re-read.
CREATE TABLE IF NOT EXISTS ingest_state (
    source_path  TEXT PRIMARY KEY,
    session_id   TEXT,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    offset_bytes INTEGER NOT NULL DEFAULT 0,
    last_seen_at TEXT
);

-- Parse failures by category (never message text).
CREATE TABLE IF NOT EXISTS parse_failures (
    source   TEXT NOT NULL,
    category TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, category)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
    session_id UNINDEXED,
    ordinal    UNINDEXED,
    body,
    tokenize = 'porter unicode61'
);
"""

# The import ledger: append-only, keyed by content digest. See
# .valholl/articles/import-ledger-schema.md, which is the normative design —
# this schema is that article's SQL verbatim, plus one extra column
# (``file_digest``) named by docs/specs/001-import-ledger.md. Nothing may
# UPDATE or DELETE a row here except finish_import() closing out the very row
# begin_import() created; a second import of the same digest always appends.
_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_ledger (
    ledger_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,          -- ISO8601 UTC
    finished_at   TEXT,                   -- NULL while in flight or if crashed
    actor         TEXT NOT NULL,          -- "cli", "hook", "watcher", "agent:<name>"
    source_kind   TEXT NOT NULL,          -- claude-transcripts | codex-rollouts
                                          -- claude-export | chatgpt-export | prose-index
    source_ref    TEXT,                   -- path or export name, display only
    source_digest TEXT NOT NULL,          -- see digest.py
    file_digest   TEXT,                   -- file-sha256 of the source file, when it is a file
    item_count    INTEGER NOT NULL DEFAULT 0,   -- items the SOURCE contains
    span_earliest TEXT,                   -- earliest item timestamp in source
    span_latest   TEXT,                   -- latest item timestamp in source
    windowed      INTEGER NOT NULL DEFAULT 0,   -- 1 if source may be a partial window
    outcome       TEXT NOT NULL,          -- imported | duplicate | partial | rejected
    duplicate_of  INTEGER,                -- ledger_id of the original import
    added         INTEGER NOT NULL DEFAULT 0,
    updated       INTEGER NOT NULL DEFAULT 0,
    unchanged     INTEGER NOT NULL DEFAULT 0,
    skipped       INTEGER NOT NULL DEFAULT 0,
    error         TEXT,                   -- exception CLASS name only, never a message
    FOREIGN KEY (duplicate_of) REFERENCES import_ledger(ledger_id)
);
CREATE INDEX IF NOT EXISTS idx_ledger_digest  ON import_ledger(source_digest);
CREATE INDEX IF NOT EXISTS idx_ledger_started ON import_ledger(started_at);

-- Per-item receipts: what actually happened to each item, and why.
CREATE TABLE IF NOT EXISTS import_items (
    ledger_id   INTEGER NOT NULL,
    item_id     TEXT NOT NULL,            -- session id / conversation uuid
    disposition TEXT NOT NULL,            -- added | updated | unchanged | skipped
    reason      TEXT,                     -- required when disposition = 'skipped'
    PRIMARY KEY (ledger_id, item_id),
    FOREIGN KEY (ledger_id) REFERENCES import_ledger(ledger_id)
);
CREATE INDEX IF NOT EXISTS idx_import_items_item ON import_items(item_id);

-- Advisory lock. One row, or none.
CREATE TABLE IF NOT EXISTS import_lock (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_id  INTEGER NOT NULL,
    actor      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    pid        INTEGER
);
"""


class Store:
    """Thin, explicit wrapper over the SQLite connection."""

    def __init__(self, conn: sqlite3.Connection, path: Path) -> None:
        self.conn = conn
        self.path = path

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def upsert_session(self, rec: dict[str, Any]) -> None:
        """Insert or update a session row.

        Losslessness rule: an update never blanks a field that previously held
        a value. A later pass with thinner data (e.g. a prose-index backfill
        for a session already ingested from raw) leaves the richer value alone.
        """
        cols = [
            "session_id", "source", "provenance", "parent_id", "cwd", "branch",
            "model", "title", "started_at", "ended_at", "duration_s",
            "user_turns", "assistant_turns", "tool_uses", "tool_results",
            "words", "tokens", "text", "source_path", "source_present",
            "origin", "ingested_at", "updated_at",
        ]
        existing = self.get_session(rec["session_id"])
        if existing is None:
            values = [rec.get(c) for c in cols]
            placeholders = ", ".join("?" for _ in cols)
            self.conn.execute(
                f"INSERT INTO sessions ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            return

        merged: dict[str, Any] = {}
        for c in cols:
            if c == "session_id":
                continue
            new = rec.get(c)
            old = existing.get(c)
            merged[c] = old if _is_blank(new) and not _is_blank(old) else new
        sets = ", ".join(f"{c} = ?" for c in merged)
        self.conn.execute(
            f"UPDATE sessions SET {sets} WHERE session_id = ?",
            [*merged.values(), rec["session_id"]],
        )

    def replace_chunks(self, session_id: str, text: str,
                       chunk_words: int = DEFAULT_CHUNK_WORDS,
                       stride: int = DEFAULT_CHUNK_STRIDE) -> int:
        """Rebuild the FTS rows for one session. Safe: chunks are derived data."""
        self.conn.execute("DELETE FROM chunks WHERE session_id = ?", (session_id,))
        rows = [(session_id, i, body)
                for i, body in enumerate(chunk_text(text, chunk_words, stride))]
        if rows:
            self.conn.executemany(
                "INSERT INTO chunks (session_id, ordinal, body) VALUES (?, ?, ?)", rows
            )
        return len(rows)

    def set_files(self, session_id: str, paths: Iterable[str]) -> None:
        rows = {(session_id, p, os.path.basename(p)) for p in paths if p}
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR IGNORE INTO session_files (session_id, path, basename) "
            "VALUES (?, ?, ?)", sorted(rows)
        )

    def set_tools(self, session_id: str, tools: dict[str, int]) -> None:
        for tool, uses in sorted(tools.items()):
            self.conn.execute(
                "INSERT INTO session_tools (session_id, tool, uses) VALUES (?, ?, ?) "
                "ON CONFLICT(session_id, tool) DO UPDATE SET uses = excluded.uses",
                (session_id, tool, uses),
            )

    def record_parse_failure(self, source: str, category: str, count: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO parse_failures (source, category, count) VALUES (?, ?, ?) "
            "ON CONFLICT(source, category) DO UPDATE SET count = count + excluded.count",
            (source, category, count),
        )

    def save_ingest_state(self, source_path: str, session_id: str | None,
                          size_bytes: int, mtime: float, offset_bytes: int,
                          seen_at: str) -> None:
        self.conn.execute(
            "INSERT INTO ingest_state "
            "(source_path, session_id, size_bytes, mtime, offset_bytes, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_path) DO UPDATE SET "
            "  session_id = excluded.session_id, size_bytes = excluded.size_bytes, "
            "  mtime = excluded.mtime, offset_bytes = excluded.offset_bytes, "
            "  last_seen_at = excluded.last_seen_at",
            (source_path, session_id, size_bytes, mtime, offset_bytes, seen_at),
        )

    def get_ingest_state(self, source_path: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM ingest_state WHERE source_path = ?", (source_path,))
        row = cur.fetchone()
        return dict(row) if row else None

    def prune_tool_invoked(self, source: str | None = None) -> int:
        """Drop prose and chunks for tool-invoked sessions, keeping their counts.

        The archive-of-record guarantee covers ``human`` and ``subagent``
        sessions. Tool-invoked rows are reproducible byproducts of some other
        tool's call volume — and sometimes pure bug residue — so their prose is
        not worth storing. Metadata is retained so statistics and provenance
        anomaly detection still work.
        """
        params: list[object] = []
        where = "provenance = 'tool-invoked'"
        if source:
            where += " AND source = ?"
            params.append(source)
        rows = self.conn.execute(
            f"SELECT session_id FROM sessions WHERE {where} AND text != ''", params
        ).fetchall()
        for row in rows:
            self.conn.execute("DELETE FROM chunks WHERE session_id = ?",
                              (row["session_id"],))
            self.conn.execute("UPDATE sessions SET text = '' WHERE session_id = ?",
                              (row["session_id"],))
        self.conn.commit()
        return len(rows)

    def mark_source_missing(self, session_id: str) -> None:
        """The raw file is gone. Keep the archived prose; just record the fact."""
        self.conn.execute(
            "UPDATE sessions SET source_present = 0 WHERE session_id = ?", (session_id,))

    def commit(self) -> None:
        self.conn.commit()

    # -- import ledger -------------------------------------------------------
    #
    # See .valholl/articles/import-ledger-schema.md. The one rule that matters
    # more than any single method: nothing here UPDATEs or DELETEs a completed
    # import_ledger row. begin_import() appends; finish_import() closes out
    # only the specific in-flight row its own ledger_id names.

    def begin_import(self, *, actor: str, source_kind: str, source_ref: str | None,
                     source_digest: str, facts: SourceFacts) -> int:
        """INSERT an in-flight row and return its ``ledger_id``.

        The row is pre-inserted with ``finished_at`` NULL and
        ``outcome='rejected'``. That default matters: if the process crashes
        before finish_import() runs, the row already reads as "did not
        succeed" rather than as a success nobody confirmed. See invariant 9
        (a crashed import must be visible, never silently reaped).
        """
        started_at = _now()
        cur = self.conn.execute(
            "INSERT INTO import_ledger "
            "(started_at, finished_at, actor, source_kind, source_ref, source_digest, "
            " file_digest, item_count, span_earliest, span_latest, windowed, outcome) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (started_at, actor, source_kind, source_ref, source_digest,
             facts.file_digest, facts.item_count, facts.span_earliest,
             facts.span_latest, 1 if facts.windowed else 0, Outcome.REJECTED.value),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_import(self, ledger_id: int, *, outcome: Outcome, delta: Delta,
                      duplicate_of: int | None = None, error: str | None = None) -> None:
        """Set ``finished_at`` + outcome + counts on THIS row only.

        This is the sole UPDATE the ledger permits, and it targets exactly the
        row ``ledger_id`` names — never a row from a different import, and
        never by matching on digest (which would let a later run silently
        overwrite an earlier one's evidence, exactly as claudex's
        ``ON CONFLICT(source_path) DO UPDATE`` did to ``ingest_state``).
        """
        self.conn.execute(
            "UPDATE import_ledger SET finished_at = ?, outcome = ?, duplicate_of = ?, "
            "added = ?, updated = ?, unchanged = ?, skipped = ?, error = ? "
            "WHERE ledger_id = ?",
            (_now(), outcome.value, duplicate_of, delta.added, delta.updated,
             delta.unchanged, delta.skipped, error, ledger_id),
        )
        self.conn.commit()

    def record_items(self, ledger_id: int,
                     items: Iterable[tuple[str, str, str | None]]) -> None:
        """Insert ``(item_id, disposition, reason)`` per-item receipts.

        A count cannot be audited; this table is what makes "which four items,
        and why?" a query instead of forensics.
        """
        rows = [(ledger_id, item_id, disposition, reason)
                for item_id, disposition, reason in items]
        if not rows:
            return
        self.conn.executemany(
            "INSERT INTO import_items (ledger_id, item_id, disposition, reason) "
            "VALUES (?, ?, ?, ?)", rows,
        )
        self.conn.commit()

    def find_import_by_digest(self, source_digest: str) -> dict[str, Any] | None:
        """Earliest COMPLETED row for this digest, or None.

        "Completed" means ``finished_at IS NOT NULL``: an in-flight or crashed
        row must never be offered up as the prior import to attribute a
        duplicate to, or a crash could masquerade as a successful import.
        """
        cur = self.conn.execute(
            "SELECT * FROM import_ledger "
            "WHERE source_digest = ? AND finished_at IS NOT NULL "
            "ORDER BY ledger_id ASC LIMIT 1",
            (source_digest,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def acquire_import_lock(self, ledger_id: int, actor: str, pid: int) -> dict[str, Any] | None:
        """Take the advisory import lock, or report who already holds it.

        Returns None on success. If the lock is already held by a live pid,
        returns that holder's row unchanged — the caller must report
        "import in progress by actor A since T", never race for it.

        A holder whose pid is no longer alive is stale: the caller may take
        the lock over, but the *ledger row that abandoned it* is left exactly
        as-is for doctor to report as an incomplete import. Nothing here
        deletes that evidence.
        """
        cur = self.conn.execute("SELECT * FROM import_lock WHERE id = 1")
        holder = cur.fetchone()
        if holder is not None and pid_alive(holder["pid"]):
            return dict(holder)
        self.conn.execute(
            "INSERT INTO import_lock (id, ledger_id, actor, acquired_at, pid) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "  ledger_id = excluded.ledger_id, actor = excluded.actor, "
            "  acquired_at = excluded.acquired_at, pid = excluded.pid",
            (ledger_id, actor, _now(), pid),
        )
        self.conn.commit()
        return None

    def release_import_lock(self, ledger_id: int) -> None:
        """Release the lock, but only if this import still holds it.

        Guards against a stale-lock takeover releasing the *new* holder's lock
        out from under it: if actor B took the lock over from a dead actor A
        and then A's (already-crashed) process somehow reached this line, it
        must not release B's live lock.
        """
        self.conn.execute(
            "DELETE FROM import_lock WHERE id = 1 AND ledger_id = ?", (ledger_id,))
        self.conn.commit()

    def incomplete_imports(self) -> list[dict[str, Any]]:
        """Rows with ``finished_at IS NULL`` — crashed or still-running imports.

        Invariant 9: a crashed import must be visible, never silently reaped.
        """
        cur = self.conn.execute(
            "SELECT * FROM import_ledger WHERE finished_at IS NULL "
            "ORDER BY ledger_id ASC")
        return [dict(r) for r in cur.fetchall()]

    def ledger_tail(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent ``limit`` ledger rows, newest first."""
        cur = self.conn.execute(
            "SELECT * FROM import_ledger ORDER BY ledger_id DESC LIMIT ?", (limit,))
        return [dict(r) for r in cur.fetchall()]

    # -- reads -------------------------------------------------------------

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def session_text(self, session_id: str) -> str:
        cur = self.conn.execute(
            "SELECT text FROM sessions WHERE session_id = ?", (session_id,))
        row = cur.fetchone()
        return row["text"] if row else ""

    def count_sessions(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"])

    def count_chunks(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])

    def search(self, query: str, limit: int = 20,
              filters: Filters | None = None) -> list[dict[str, Any]]:
        """Ranked, deduped, filtered FTS search — one row per session.

        Two queries may run. The first is the precise AND-of-terms match
        (FTS5's default join). Only if that returns fewer than ``limit`` rows
        does a second, OR-expanded query run, capped at MAX_EXPANSION_TERMS
        terms regardless of how many words the query had — see
        .valholl/articles/corpus-measurements.md for why the cap exists (a
        broad OR query measured 1.9ms -> 45.5ms as the corpus grew 8k -> 162k
        chunks) and muninn/query.py for the SQL and the cap itself. Widening
        the cap "for better recall" is the exact regression that measurement
        describes; it is a guardrail, not a tuning knob.
        """
        match = fts_query(query)
        if not match:
            return []
        sql, params = build_search_sql(filters, match, limit)
        try:
            rows = [dict(r) for r in self.conn.execute(sql, params).fetchall()]
        except sqlite3.OperationalError:
            return []

        if len(rows) < limit:
            expanded = expand_terms(match)
            if expanded and expanded != match:
                exp_sql, exp_params = build_search_sql(filters, expanded, limit)
                try:
                    exp_rows = [dict(r) for r in self.conn.execute(exp_sql, exp_params).fetchall()]
                except sqlite3.OperationalError:
                    exp_rows = []
                seen = {r["session_id"] for r in rows}
                rows.extend(r for r in exp_rows if r["session_id"] not in seen)
                # Two stable passes, weakest key first: this reproduces
                # "ORDER BY score ASC, started_at DESC" for a merged Python
                # list the same way SQL would for a single query, since
                # Python's sort is guaranteed stable.
                rows.sort(key=lambda r: r["started_at"] or "", reverse=True)
                rows.sort(key=lambda r: r["score"])
                rows = rows[:limit]
        return rows

    def log(self, limit: int = 20, filters: Filters | None = None) -> list[dict[str, Any]]:
        """Reverse-chronological session timeline for ``muninn log``."""
        sql, params = build_log_sql(filters, limit)
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


# -- helpers ---------------------------------------------------------------


def _is_blank(v: Any) -> bool:
    return v is None or v == "" or v == 0


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def pid_alive(pid: int | None) -> bool:
    """Is a process with this pid still running?

    ``os.kill(pid, 0)`` sends no signal; it only asks the kernel whether the
    process exists and is ours to signal. A stale lock (holder pid dead) may
    be taken over; a live one must serialize the caller out.
    """
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, but owned by someone else. Still alive from our perspective.
        return True
    return True


def chunk_text(text: str, chunk_words: int = DEFAULT_CHUNK_WORDS,
               stride: int = DEFAULT_CHUNK_STRIDE) -> list[str]:
    """Split prose into overlapping word windows.

    Overlap exists so a phrase spanning a boundary is still findable. Turn-aware
    chunking lands with the source adapters; this is the mechanical fallback.
    """
    words = text.split()
    if not words:
        return []
    if len(words) <= chunk_words:
        return [" ".join(words)]
    out = []
    for i in range(0, len(words), stride):
        window = words[i:i + chunk_words]
        if not window:
            break
        out.append(" ".join(window))
        if i + chunk_words >= len(words):
            break
    return out


_FTS_SAFE = re.compile(r"[^\w\s\"'*-]+")


def fts_query(raw: str) -> str:
    """Turn user input into a safe FTS5 MATCH expression.

    FTS5's query language would otherwise raise on bare punctuation (a literal
    ``NEAR/10`` is a syntax error, for instance), so strip operators we do not
    explicitly support and quote the remaining terms.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Preserve an explicit phrase search.
    if raw.startswith('"') and raw.endswith('"') and len(raw) > 2:
        inner = _FTS_SAFE.sub(" ", raw[1:-1]).strip()
        return f'"{inner}"' if inner else ""
    upper_ops = {"AND", "OR", "NOT"}
    parts: list[str] = []
    for tok in _FTS_SAFE.sub(" ", raw).split():
        if tok in upper_ops:
            parts.append(tok)
        elif tok.endswith("*") and len(tok) > 1:
            parts.append(f'"{tok[:-1]}"*')
        else:
            parts.append(f'"{tok}"')
    return " ".join(parts)


def open_store(path: str | Path) -> Store:
    """Open (creating if needed) the archive at ``path``."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # --repo matches against the basename of sessions.cwd (docs/specs/004).
    # SQLite has no builtin for "last path segment", and hand-rolling it as a
    # SQL expression gets trailing-slash / no-slash edge cases subtly wrong;
    # registering os.path.basename directly means the filter and the CLI's
    # own display logic (cmd_search's `Path(cwd).name`) can never disagree.
    conn.create_function("basename", 1, lambda p: os.path.basename(p) if p else "")
    conn.executescript(_SCHEMA)

    # Migration from v1 (no ledger) to v2: a v1 archive has every table in
    # _SCHEMA already, just none of the ledger tables. Every statement in
    # _LEDGER_SCHEMA is CREATE-IF-NOT-EXISTS, so running it unconditionally on
    # any existing archive is safe and adds only what is missing — no rebuild,
    # no data touched. Existing sessions, chunks, and ingest_state are
    # untouched by this call.
    conn.executescript(_LEDGER_SCHEMA)

    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),))
    elif int(row["value"]) < SCHEMA_VERSION:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION),))
    conn.commit()
    if fresh:
        try:
            os.chmod(path, 0o600)  # the archive contains conversation prose
        except OSError:
            pass
    return Store(conn, path)
