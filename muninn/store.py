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

import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

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

    def search(self, query: str, limit: int = 20, **filters: Any) -> list[dict[str, Any]]:
        """Minimal FTS search. Structured filters arrive with task #5."""
        match = fts_query(query)
        if not match:
            return []
        sql = (
            "SELECT c.session_id AS session_id, s.source AS source, "
            "       s.provenance AS provenance, s.started_at AS started_at, "
            "       snippet(chunks, 2, '[', ']', ' ... ', 16) AS excerpt, "
            "       bm25(chunks) AS score "
            "FROM chunks c JOIN sessions s ON s.session_id = c.session_id "
            "WHERE chunks MATCH ? "
            "ORDER BY score LIMIT ?"
        )
        try:
            cur = self.conn.execute(sql, (match, limit))
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in cur.fetchall()]


# -- helpers ---------------------------------------------------------------


def _is_blank(v: Any) -> bool:
    return v is None or v == "" or v == 0


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
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING", (str(SCHEMA_VERSION),))
    conn.commit()
    if fresh:
        try:
            os.chmod(path, 0o600)  # the archive contains conversation prose
        except OSError:
            pass
    return Store(conn, path)
