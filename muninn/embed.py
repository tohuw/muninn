"""Embeddings: storage, cosine search, and the measurement that removes the work.

**Brute-force numpy cosine over 60,000 chunks x 1024 dims: 1.9 ms.**

That number is the whole design. It means **no vector database, no HNSW, no
faiss, ever** — a matrix multiply and an ``argpartition`` are sufficient well
past any corpus a person will accumulate, and every ANN index is a cache
invalidation problem, a build step and a dependency bought to save a
millisecond. See .valholl/articles/corpus-measurements.md; anyone proposing an
index for this workload has not measured it.

The entire cost of "semantic" is *generating* the embeddings once, not searching
them. That is why ``muninn embed`` is a separate, resumable, dry-runnable
command and search is not.

Since docs/specs/014-automatic-embedding.md that cost is paid **automatically**,
by a background worker the daemon owns (``muninn/embedder.py``), whenever a
provider is installed. This module is unchanged by that: the worker embeds the
same :func:`pending_chunks` rows through the same :func:`store_vectors`, so
"automatic" is a scheduling decision made elsewhere and not a second code path
through storage.

## numpy is imported lazily, and that is load-bearing

``import muninn.embed`` must not import numpy. numpy lives in the optional
``[semantic]`` extra (CLAUDE.md: anything ML-shaped must not be a default
install), and ``muninn.cli`` imports this module unconditionally to register the
subcommands — so a module-level ``import numpy`` would make the *default*
install fail at startup rather than fail helpfully at ``--semantic``. Every
numpy use is therefore inside a function, and a test asserts the property in a
subprocess rather than trusting inspection.

## Vectors from different models are never mixed

``model`` is part of the primary key, every query filters on it, and there is no
"all models" read path. Mixing two embedding spaces does not raise and does not
look wrong — it returns confident nonsense — which is the kind of failure that
survives review, so it is prevented structurally rather than remembered.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from . import policy
from .store import Store

#: float32 little-endian, matching the BLOB layout documented in the schema.
_ITEM = "<f"
_ITEM_BYTES = 4


class EmbeddingUnavailable(RuntimeError):
    """No usable embedding provider, or numpy is absent. Always actionable.

    Raised instead of degrading to lexical results, because a search that
    silently drops the semantic half while the user asked for ``--semantic``
    reports the wrong thing confidently — the same class of failure as a
    calibration that defaults its gate.
    """


class DimensionMismatch(ValueError):
    """A vector's length disagrees with the provider's declared ``dim``."""


class EmbeddingProvider(Protocol):
    """Structurally identical to ``plugins.EmbeddingProvider``.

    ``available()`` must do no I/O — no network call, no weight load. It runs
    during discovery and at startup, and a slow probe there is a hang rather
    than a diagnostic. ``embed()`` returns an ``(n, dim)`` float32 array,
    **L2-normalised**, so search is a plain dot product rather than a division
    per row per query.
    """

    name: str
    model: str
    dim: int

    def available(self) -> str | None: ...

    def embed(self, texts: Sequence[str]) -> Any: ...


def require_numpy():
    """Import numpy, or raise something a person can act on.

    Deferred to call time on purpose — see the module docstring. The error names
    the extra rather than the package, because ``pip install numpy`` alone would
    satisfy the import and still leave no provider installed.
    """
    try:
        import numpy
    except ImportError as exc:
        raise EmbeddingUnavailable(
            "numpy is not installed — semantic search lives in the optional "
            "extra: `uv sync --extra semantic`"
        ) from exc
    return numpy


# ── Storage ───────────────────────────────────────────────────────────────────

def pack_vector(values: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob: bytes) -> tuple[float, ...]:
    return struct.unpack(f"<{len(blob) // _ITEM_BYTES}f", blob)


def store_vectors(st: Store, session_id: str, model: str, dim: int,
                  vectors: Sequence[Sequence[float]], *, start_ordinal: int = 0) -> int:
    """Persist one session's chunk vectors. Returns the row count written.

    A wrong-length vector raises rather than being padded or truncated. The
    alternative is a silently corrupt row that cosine search will happily rank —
    and because every value is finite and the arithmetic succeeds, nothing
    downstream would ever notice.
    """
    rows = []
    for offset, vector in enumerate(vectors):
        values = list(vector)
        if len(values) != dim:
            raise DimensionMismatch(
                f"{session_id} chunk {start_ordinal + offset}: provider declares dim={dim}, "
                f"got {len(values)}")
        rows.append((session_id, start_ordinal + offset, model, dim, pack_vector(values)))
    if rows:
        st.conn.executemany(
            "INSERT INTO chunk_vectors (session_id, ordinal, model, dim, vec) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, ordinal, model) DO UPDATE SET "
            "  dim = excluded.dim, vec = excluded.vec",
            rows)
    return len(rows)


def pending_chunks(st: Store, model: str, *, source: str | None = None,
                   limit: int | None = None, newest_first: bool = False) -> list[Any]:
    """Chunks with no vector in ``model``'s space: the work still to be paid for.

    One definition of "pending", used by both callers — ``muninn embed`` and the
    background worker (``embedder.BackgroundEmbedder``). Two copies of this
    ``LEFT JOIN`` would be two chances to disagree about what is already
    embedded, and the direction that disagreement fails in is a worker that
    re-embeds rows the CLI considers done, which costs real money per pass
    against a metered provider.

    ``newest_first`` orders by the session's start time rather than by
    ``session_id``, and it is the worker's default for a reason worth stating:
    on a fresh archive the backlog is thousands of chunks, and the session a
    person most wants to find semantically is the one they just finished. Id
    order would embed it last. The CLI keeps id order, which is stable and makes
    a resumed run visibly continue where it stopped.

    Sessions with no ``started_at`` sort last under ``newest_first`` rather than
    first, which is what ``NULLS LAST`` would say if SQLite's older syntax
    supported it here; an undated session is not evidence of recency.
    """
    sql = ("SELECT c.session_id, c.ordinal, c.body FROM chunks c "
           "LEFT JOIN chunk_vectors v "
           "  ON v.session_id = c.session_id AND v.ordinal = c.ordinal AND v.model = ? "
           "WHERE v.session_id IS NULL ")
    params: list[Any] = [model]
    if source:
        sql += "AND c.session_id IN (SELECT session_id FROM sessions WHERE source = ?) "
        params.append(source)
    if newest_first:
        sql += ("ORDER BY (SELECT s.started_at IS NULL FROM sessions s "
                "          WHERE s.session_id = c.session_id), "
                "         (SELECT s.started_at FROM sessions s "
                "          WHERE s.session_id = c.session_id) DESC, "
                "         c.session_id, c.ordinal")
    else:
        sql += "ORDER BY c.session_id, c.ordinal"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return st.conn.execute(sql, tuple(params)).fetchall()


def pending_count(st: Store, model: str, *, source: str | None = None) -> int:
    """How many chunks still need a vector in ``model``'s space.

    Reported by `doctor`, because an automatic background pass that has silently
    stopped looks exactly like one that has finished — and this project's whole
    recurring lesson is that invisible staleness is the expensive kind
    (.valholl/articles/continuous-ingest-not-periodic.md).
    """
    sql = ("SELECT COUNT(*) n FROM chunks c "
           "LEFT JOIN chunk_vectors v "
           "  ON v.session_id = c.session_id AND v.ordinal = c.ordinal AND v.model = ? "
           "WHERE v.session_id IS NULL ")
    params: list[Any] = [model]
    if source:
        sql += "AND c.session_id IN (SELECT session_id FROM sessions WHERE source = ?) "
        params.append(source)
    return int(st.conn.execute(sql, tuple(params)).fetchone()["n"])


def vector_count(st: Store, model: str | None = None) -> int:
    if model is None:
        return int(st.conn.execute(
            "SELECT COUNT(*) n FROM chunk_vectors").fetchone()["n"])
    return int(st.conn.execute(
        "SELECT COUNT(*) n FROM chunk_vectors WHERE model = ?", (model,)).fetchone()["n"])


def models_present(st: Store) -> list[tuple[str, int, int]]:
    """``(model, dim, rows)`` for every embedding space in the archive.

    Reported by `doctor` so a half-finished model migration — two spaces, one
    of them stale — is visible rather than discovered when search gets worse.
    """
    return [(r["model"], r["dim"], r["n"]) for r in st.conn.execute(
        "SELECT model, dim, COUNT(*) n FROM chunk_vectors GROUP BY model, dim "
        "ORDER BY model")]


# ── The matrix ────────────────────────────────────────────────────────────────

@dataclass
class Matrix:
    """A contiguous float32 array plus the keys its rows correspond to."""

    keys: tuple[tuple[str, int], ...]     # (session_id, ordinal), row-aligned
    array: Any                            # numpy (n, dim) float32

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def nbytes(self) -> int:
        return int(getattr(self.array, "nbytes", 0))


#: ``(db_path, model) -> (stamp, Matrix)``. See :func:`_stamp` for what the
#: stamp covers and why the obvious version of it was wrong.
_MATRIX_CACHE: dict[tuple[str, str], tuple[tuple, Matrix]] = {}


def _stamp(st: Store) -> tuple:
    """A cheap value that changes whenever the archive might have.

    The obvious implementation — stat the database file — is **wrong here, and
    silently**, which is why this has its own function and this comment. The
    archive runs in WAL mode (``store.open_store`` sets ``journal_mode=WAL``),
    so a committed write lands in ``muninn.db-wal`` and leaves ``muninn.db``
    untouched until a checkpoint. A matrix cached before `muninn embed` ran
    would therefore still be served afterwards, in any process that outlives the
    write — the daemon, or a shell that searched twice. Caught by a test that
    embedded, re-read, and got the old row count back.

    So the stamp is three things:

    - the database file's ``(mtime, size)`` — catches checkpoints and any
      non-WAL fallback;
    - the ``-wal`` file's ``(mtime, size)`` — catches the ordinary committed
      write, which is the case that failed;
    - ``PRAGMA data_version`` — catches a *different* connection's commit that
      has already been checkpointed away, which neither stat can see.

    Size is included alongside mtime because mtime has one-second granularity on
    some filesystems, and an embed pass finishing inside the same second as the
    previous read would otherwise look unchanged.
    """
    parts: list[object] = []
    for path in (st.path, st.path.with_name(st.path.name + "-wal")):
        try:
            stat = path.stat()
            parts.append((stat.st_mtime, stat.st_size))
        except OSError:
            parts.append(None)   # absent is a state, and a stable one
    try:
        parts.append(st.conn.execute("PRAGMA data_version").fetchone()[0])
    except Exception:   # noqa: BLE001 - a stamp that cannot be read must not
        parts.append(None)      # break a read path; it just caches less well
    return tuple(parts)


def load_matrix(st: Store, model: str, *, use_cache: bool = True) -> Matrix:
    """Every vector for ``model``, as one array. Cached on the archive's mtime.

    60k x 1024 float32 is 246 MB, which is acceptable to hold and worth
    reporting — `doctor` prints it, so growth is visible before it is a problem
    rather than after.
    """
    np = require_numpy()
    key = (str(st.path), model)
    stamp = _stamp(st)
    if use_cache:
        cached = _MATRIX_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]

    rows = st.conn.execute(
        "SELECT session_id, ordinal, dim, vec FROM chunk_vectors WHERE model = ? "
        "ORDER BY session_id, ordinal", (model,)).fetchall()
    if not rows:
        matrix = Matrix(keys=(), array=np.zeros((0, 0), dtype=np.float32))
    else:
        dim = rows[0]["dim"]
        # A row whose blob length disagrees with its dim is dropped rather than
        # reshaped: it cannot be interpreted, and guessing would put arbitrary
        # numbers into a ranking.
        usable = [r for r in rows if len(r["vec"]) == dim * _ITEM_BYTES]
        array = np.frombuffer(b"".join(r["vec"] for r in usable),
                              dtype="<f4").reshape(len(usable), dim)
        matrix = Matrix(keys=tuple((r["session_id"], r["ordinal"]) for r in usable),
                        array=np.ascontiguousarray(array))
    # Re-stamped after the read, not before: a write that lands *during* the
    # read would otherwise be cached under the pre-write stamp and never
    # invalidate. Storing the later stamp means the next call re-reads, which is
    # the safe direction to be wrong in.
    _MATRIX_CACHE[key] = (_stamp(st), matrix)
    return matrix


def clear_cache() -> None:
    _MATRIX_CACHE.clear()


# ── Search ────────────────────────────────────────────────────────────────────

def normalize(vector: Any) -> Any:
    """L2-normalise, leaving a zero vector alone rather than dividing by zero."""
    np = require_numpy()
    arr = np.asarray(vector, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr if norm == 0.0 else (arr / norm).astype(np.float32)


def cosine_topk(matrix: Matrix, query: Any, k: int = 20) -> list[tuple[tuple[str, int], float]]:
    """Top ``k`` rows by cosine similarity, best first.

    A plain dot product: stored vectors are unit-length by the provider's
    contract and the query is normalised here, so cosine *is* the dot product
    and no per-row division is needed.

    ``argpartition`` rather than a full ``argsort`` — it is O(n) against O(n log
    n), and at 60k rows the difference is most of the 1.9 ms.
    """
    np = require_numpy()
    if len(matrix) == 0 or matrix.array.size == 0:
        return []
    q = normalize(query)
    if q.shape[0] != matrix.array.shape[1]:
        raise DimensionMismatch(
            f"query has dim={q.shape[0]}, archive vectors have dim={matrix.array.shape[1]}")
    scores = matrix.array @ q
    k = min(k, scores.shape[0])
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(matrix.keys[i], float(scores[i])) for i in top]


def search_sessions(st: Store, provider: EmbeddingProvider, query: str,
                    limit: int = 20) -> list[tuple[str, float]]:
    """Semantic search, collapsed to one row per session.

    A session's score is its **best** chunk, not its mean. A long session with
    one exactly-relevant passage is a better answer than a short one that is
    vaguely on-topic throughout, and averaging inverts that — the long session's
    single strong chunk gets diluted by fifty unrelated ones.
    """
    policy.check(provider.model, provider.name)
    matrix = load_matrix(st, provider.model)
    if len(matrix) == 0:
        return []
    vector = provider.embed([query])[0]
    # Over-fetch chunks so that collapsing to sessions still fills the limit:
    # the top 20 chunks are frequently 3 sessions.
    hits = cosine_topk(matrix, vector, k=min(limit * 10, len(matrix)))
    best: dict[str, float] = {}
    for (session_id, _ordinal), score in hits:
        if score > best.get(session_id, float("-inf")):
            best[session_id] = score
    ranked = sorted(best.items(), key=lambda kv: (-kv[1], kv[0]))
    return ranked[:limit]


def correlate(st: Store, model: str, session_id: str,
              limit: int = 10) -> list[tuple[str, float]]:
    """Sessions most like this one, by mean vector, excluding itself.

    The "conversations like this one" feature, and the reason embeddings are
    worth having beyond query-time recall.

    A session's **mean** vector is right here where a per-chunk max was right
    for query search, and the difference is what is being asked. A query asks
    "does this session contain this"; correlation asks "is this session *about*
    the same thing", which is a property of the whole conversation. Comparing
    best-chunk to best-chunk would make every pair of sessions that once
    mentioned a stack trace look like neighbours.
    """
    np = require_numpy()
    matrix = load_matrix(st, model)
    if len(matrix) == 0:
        return []

    by_session: dict[str, list[int]] = {}
    for row, (sid, _ordinal) in enumerate(matrix.keys):
        by_session.setdefault(sid, []).append(row)
    if session_id not in by_session:
        return []

    means = {sid: normalize(matrix.array[rows].mean(axis=0))
             for sid, rows in by_session.items()}
    target = means[session_id]
    scored = [(sid, float(np.dot(target, vec)))
              for sid, vec in means.items() if sid != session_id]
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return scored[:limit]


# ── Providers ─────────────────────────────────────────────────────────────────

def resolve_provider(name: str | None = None) -> EmbeddingProvider:
    """The embedding provider to use, or a refusal that names the fix.

    **The default build ships none.** A plugin contributes one, or the optional
    ``[semantic]`` extra provides the local MLX provider. This function never
    invents a fallback: a search that quietly returned lexical results while the
    user asked for semantic ones is the specific failure spec 006 forbids.
    """
    from .plugins import discover_plugins

    candidates = [p for spec in discover_plugins().specs for p in spec.embedders]
    if name is not None:
        for candidate in candidates:
            if getattr(candidate, "name", None) == name:
                return candidate
        raise EmbeddingUnavailable(f"no embedding provider named {name!r} is installed")

    for candidate in candidates:
        if candidate.available() is None:
            return candidate

    # Imported here, not at module scope: the local provider pulls MLX, which is
    # not a default dependency (docs/specs/006, "imported lazily").
    try:
        from .embed_mlx import MLXEmbeddingProvider
    except ImportError:
        MLXEmbeddingProvider = None    # noqa: N806 - a sentinel, not a class alias
    if MLXEmbeddingProvider is not None:
        local = MLXEmbeddingProvider()
        if local.available() is None:
            return local
        reason = local.available()
    else:
        reason = "the [semantic] extra is not installed"

    raise EmbeddingUnavailable(
        f"no embedding provider is available — {reason}. Install the local one "
        f"with `uv sync --extra semantic`, or a plugin that contributes an "
        f"EmbeddingProvider."
    )
