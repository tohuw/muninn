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

    Two members are optional, and both exist because the first provider that
    windowed its input needed to say something the original three attributes
    could not. Neither may be *required*, because `plugins.EmbeddingProvider`
    is this same protocol and a plugin written against it must not break for
    lacking a member added later:

    - ``space`` — the identity of the vectors, if the weights alone do not
      determine it. See :func:`space_of`.
    - ``embed_windows`` — per-window vectors instead of one per text. See
      :func:`windows_of`, which is how callers must reach it.
    """

    name: str
    model: str
    dim: int

    def available(self) -> str | None: ...

    def embed(self, texts: Sequence[str]) -> Any: ...


def space_of(provider: EmbeddingProvider) -> str:
    """The key ``chunk_vectors`` stores a provider's vectors under.

    Defaults to the model id, which is what it was for every provider until
    one of them needed to say more. ``chunk_vectors`` exists to keep
    incompatible vectors apart, and the model id was standing in for that job
    on the assumption that the weights fully determine the vector. They do
    not: `embed_onnx` used to embed a chunk's first 512 tokens and discard the
    rest, and now embeds every window of it, and those two sets of numbers
    must never be compared even though every byte of the weights is identical.

    So a provider may declare a ``space``, and one that does not keeps the old
    behaviour unchanged — which matters because a plugin-supplied provider
    (`plugins.EmbeddingProvider`) was written against the three-attribute
    protocol and must not break for gaining a fourth.

    Kept separate from ``provider.model`` on purpose. The model id is what
    `policy.check` governs and what `doctor` prints to a person, and neither
    of those wants a recipe suffix.
    """
    declared = getattr(provider, "space", None)
    return str(declared) if declared else provider.model


def windows_of(provider: EmbeddingProvider, texts: Sequence[str]) -> tuple[Any, list[int]]:
    """``(array, owners)``: every window's vector, and which text produced it.

    ``array`` is ``(total_windows, dim)`` and ``owners[i]`` is the index into
    ``texts`` that row ``i`` came from, so a text that needed three windows
    appears three times. Rows stay in window order within a text, which is
    what makes ``owners`` sufficient to number them.

    **A provider that does not window is not a special case.** Without
    ``embed_windows`` this is ``provider.embed(texts)`` and ``owners`` is just
    ``range(len(texts))`` — one window per text, which is literally true of a
    provider that reads each text once. So `embed_mlx` and any plugin provider
    keep working untouched and store exactly the rows they always did, while
    the windowing provider stores one row per window, through this one call
    site rather than a branch at each.

    Returning a flat array plus an owner map, rather than a list of arrays per
    text, is deliberate: it is the shape a provider already has internally
    (its batches span several texts' windows), and it keeps the caller from
    re-concatenating what was just split.

    **``owners`` is truncated to the number of rows actually returned, and
    that is a behaviour this module is required to have rather than a
    defensive habit.** A provider that returns fewer vectors than it was asked
    for is broken, and `embedder.BackgroundEmbedder` detects exactly that
    case — by seeing a pass make *zero progress* against a non-empty backlog,
    then stopping after ``stall_limit`` passes. It is what stands between a
    broken metered provider and a hot loop billing for it. The callers used to
    get this free from ``zip``, which stops at the shorter side; building an
    owner list from ``len(texts)`` instead turned that silent short write into
    an ``IndexError``, which the worker read as a transient failure and
    retried forever.
    """
    embed_windows = getattr(provider, "embed_windows", None)
    if embed_windows is None:
        array = provider.embed(texts)
        owners = list(range(len(texts)))
    else:
        array, owners = embed_windows(texts)
        owners = list(owners)
    return array, owners[:len(array)]


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


def store_window_vectors(st: Store, session_id: str, model: str, dim: int,
                         items: Sequence[tuple[int, int, Sequence[float]]]) -> int:
    """Persist ``(ordinal, window, vector)`` rows. Returns the count written.

    The one write path into ``chunk_vectors``; :func:`store_vectors` is this
    function with every window fixed at 0. Kept as one function on purpose —
    two INSERTs would be two chances to disagree about the conflict clause,
    and the direction that disagreement fails in is a duplicate row that
    cosine search ranks twice.

    A wrong-length vector raises rather than being padded or truncated. The
    alternative is a silently corrupt row that cosine search will happily rank —
    and because every value is finite and the arithmetic succeeds, nothing
    downstream would ever notice.
    """
    rows = []
    for ordinal, window, vector in items:
        values = list(vector)
        if len(values) != dim:
            raise DimensionMismatch(
                f"{session_id} chunk {ordinal} window {window}: "
                f"provider declares dim={dim}, got {len(values)}")
        rows.append((session_id, ordinal, model, window, dim, pack_vector(values)))
    if rows:
        st.conn.executemany(
            "INSERT INTO chunk_vectors (session_id, ordinal, model, window, dim, vec) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id, ordinal, model, window) DO UPDATE SET "
            "  dim = excluded.dim, vec = excluded.vec",
            rows)
    return len(rows)


def store_vectors(st: Store, session_id: str, model: str, dim: int,
                  vectors: Sequence[Sequence[float]], *, start_ordinal: int = 0) -> int:
    """One vector per consecutive chunk ordinal, all at window 0.

    What a provider that does no windowing produces, and the signature every
    caller used before windowing existed. Delegates to
    :func:`store_window_vectors` so there is still exactly one INSERT.
    """
    return store_window_vectors(
        st, session_id, model, dim,
        [(start_ordinal + offset, 0, vector) for offset, vector in enumerate(vectors)])


def embed_and_store(st: Store, provider: EmbeddingProvider,
                    rows: Sequence[Any]) -> int:
    """Embed one batch of pending chunks and write every window. Row count out.

    Used by both `muninn embed` and the background worker
    (`embedder.BackgroundEmbedder`), which is the point: those two had
    identical copies of this loop, and :func:`pending_chunks` already exists
    because two copies of the *read* side would be two chances to disagree
    about what is embedded. The write side has the same problem and now has
    the same answer.

    Windows are numbered per chunk in the order the provider returned them,
    which :func:`windows_of` guarantees. The numbering is positional rather
    than derived from token offsets because nothing reads it: it exists to
    keep a chunk's windows from colliding in the primary key, and a window's
    *meaning* is in its vector.

    Does not commit. Both callers batch and commit around this so a killed run
    keeps every vector it has already paid for.
    """
    if not rows:
        return 0
    array, owners = windows_of(provider, [r["body"] for r in rows])
    model, dim = space_of(provider), provider.dim

    # (session_id, ordinal) -> next unused window number, so a batch spanning
    # several sessions numbers each chunk's windows from 0 independently.
    counters: dict[tuple[str, int], int] = {}
    grouped: dict[str, list[tuple[int, int, Any]]] = {}
    for position, owner in enumerate(owners):
        row = rows[owner]
        key = (row["session_id"], row["ordinal"])
        window = counters.get(key, 0)
        counters[key] = window + 1
        grouped.setdefault(row["session_id"], []).append(
            (row["ordinal"], window, array[position]))

    written = 0
    for session_id, items in grouped.items():
        written += store_window_vectors(st, session_id, model, dim, items)
    return written


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


def embedded_chunks(st: Store, model: str) -> int:
    """How many *chunks* have at least one vector in ``model``'s space.

    Not the same as :func:`vector_count`, which counts rows and therefore
    windows. The distinction exists because `doctor` reports coverage as a
    percentage of chunks, and dividing a window count by a chunk count
    produced "137% of chunks" the moment a chunk could hold more than one
    vector — a number that reads as a bug in the archive rather than in the
    arithmetic.
    """
    return int(st.conn.execute(
        "SELECT COUNT(DISTINCT session_id || ':' || ordinal) n "
        "FROM chunk_vectors WHERE model = ?", (model,)).fetchone()["n"])


def models_present(st: Store) -> list[tuple[str, int, int]]:
    """``(model, dim, rows)`` for every embedding space in the archive.

    Reported by `doctor` so a half-finished model migration — two spaces, one
    of them stale — is visible rather than discovered when search gets worse.
    """
    return [(r["model"], r["dim"], r["n"]) for r in st.conn.execute(
        "SELECT model, dim, COUNT(*) n FROM chunk_vectors GROUP BY model, dim "
        "ORDER BY model")]


def drop_space(st: Store, model: str) -> int:
    """Delete every vector stored under one space key. Returns rows removed.

    **The only delete path in this module, and the only one that should ever
    exist here.** AGENTS.md's first hard rule is that session prose is never
    deleted, because the archive is the last copy of it. Vectors are the
    opposite kind of data: a derived artefact, reproducible from prose that is
    still present, at a cost measured in CPU hours rather than in lost history.
    That asymmetry is the whole justification — it is not that deleting is
    cheap, it is that nothing is *lost*.

    Deleting by space and not by session is also deliberate. A space goes stale
    as a unit: `space_of` exists so that two incompatible sets of numbers cannot
    share a key, and the corollary is that half a space is not a thing anyone
    wants. A caller that wants some sessions re-embedded should delete none of
    this and let :func:`pending_chunks` find them.

    Does not commit. The caller owns the transaction, as every other write here
    does, so a wrapper that also needs to record something can do both or
    neither.
    """
    cursor = st.conn.execute("DELETE FROM chunk_vectors WHERE model = ?", (model,))
    return int(cursor.rowcount or 0)


# ── The matrix ────────────────────────────────────────────────────────────────

@dataclass
class Matrix:
    """A contiguous float32 array plus the keys its rows correspond to.

    ``window`` is in the key only to keep it unique — a chunk embedded as
    three windows is three rows. No consumer reads it, but a key that
    collided would be a quietly wrong answer the first time anything indexed
    by it, which is the kind of thing this project prefers to make impossible
    rather than remember.
    """

    keys: tuple[tuple[str, int, int], ...]   # (session_id, ordinal, window), row-aligned
    array: Any                               # numpy (n, dim) float32

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
        "SELECT session_id, ordinal, window, dim, vec FROM chunk_vectors WHERE model = ? "
        "ORDER BY session_id, ordinal, window", (model,)).fetchall()
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
        matrix = Matrix(
            keys=tuple((r["session_id"], r["ordinal"], r["window"]) for r in usable),
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

    A session's score is its **best** vector, not its mean. A long session with
    one exactly-relevant passage is a better answer than a short one that is
    vaguely on-topic throughout, and averaging inverts that — the long session's
    single strong chunk gets diluted by fifty unrelated ones.

    "Best vector" rather than "best chunk" since one chunk may be several
    windows, and that is the whole reason per-window rows are stored: the max
    here already did the right thing, so giving it the windows to choose
    between is enough, and no code in this function changed to gain it. A
    query that matches the tail of a long code chunk now scores against that
    tail, instead of against an average in which it was one voice of three.

    The query itself goes through ``provider.embed``, not
    :func:`windows_of`: a query needs exactly one vector to search with, and
    for a query long enough to window, the provider's mean over its windows is
    the sensible single vector. The asymmetry is intentional — stored text is
    being *indexed*, where more rows is better, and a query is being *reduced*
    to one point.
    """
    policy.check(provider.model, provider.name)
    matrix = load_matrix(st, space_of(provider))
    if len(matrix) == 0:
        return []
    vector = provider.embed([query])[0]
    # Over-fetch chunks so that collapsing to sessions still fills the limit:
    # the top 20 chunks are frequently 3 sessions.
    hits = cosine_topk(matrix, vector, k=min(limit * 10, len(matrix)))
    best: dict[str, float] = {}
    for (session_id, _ordinal, _window), score in hits:
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

    Since vectors are stored per *window*, this mean is over windows, which
    makes it length-weighted: a chunk that needed three windows contributes
    three times as much as one that fitted in a single pass. That is a change
    from the per-chunk mean and it is the better of the two for this question
    — "what is this session about" should weigh a long dense passage above a
    short one, and the previous equal-weighting-per-chunk was an artefact of
    the storage shape rather than a decision anyone made.
    """
    np = require_numpy()
    matrix = load_matrix(st, model)
    if len(matrix) == 0:
        return []

    by_session: dict[str, list[int]] = {}
    for row, (sid, _ordinal, _window) in enumerate(matrix.keys):
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

    # Imported here, not at module scope: these pull model runtimes, which are
    # not default dependencies (docs/specs/006, "imported lazily").
    #
    # MLX first, then ONNX. On Apple silicon MLX runs on the GPU and is the
    # faster of the two, so it keeps priority where it works; ONNX Runtime is
    # what makes semantic search exist at all on Windows and Linux, where MLX
    # cannot run. Order is preference, not capability — either satisfies the
    # protocol, and whichever answers writes its own model id into the vector
    # key, so the two never mix.
    locals_: list[EmbeddingProvider] = []
    try:
        from .embed_mlx import MLXEmbeddingProvider
    except ImportError:
        pass
    else:
        locals_.append(MLXEmbeddingProvider())
    try:
        from .embed_onnx import ONNXEmbeddingProvider
    except ImportError:
        pass
    else:
        locals_.append(ONNXEmbeddingProvider())

    reasons = []
    for local in locals_:
        reason = local.available()
        if reason is None:
            return local
        reasons.append(reason)

    raise EmbeddingUnavailable(
        "no embedding provider is available — "
        + ("; ".join(reasons) if reasons else "the [semantic] extra is not installed")
        + ". Install a local one with `uv sync --extra semantic`, or a plugin "
          "that contributes an EmbeddingProvider."
    )
