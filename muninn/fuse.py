"""Reciprocal-rank fusion: combining two ranked lists that share no units.

Lexical search returns bm25 scores. Semantic search returns cosine similarities.
They are **not commensurable** — bm25 is unbounded and corpus-relative, cosine is
[-1, 1] and query-relative — and any attempt to scale one into the other invents
a weighting nobody can defend and everybody forgets is arbitrary. RRF needs only
*ranks*, which both lists genuinely have:

    score(s) = Σ over lists  1 / (k + rank(s))          k = 60, rank 1-based

That is the whole algorithm. It is here in its own module, with no dependency on
numpy or on the store, because a scoring rule that decides what a person sees
first should be readable in one sitting and testable against hand-computed
numbers.

## Why k = 60

It is the value the original RRF paper used and the one every implementation
since has inherited, and its effect is to flatten the difference between the top
few ranks: 1/(60+1) and 1/(60+2) differ by 1.6%, so a result ranked first in one
list and third in the other beats one ranked second in both — but not by much.
That is the intended behaviour. A small k would make rank 1 dominate and turn
fusion into "whichever engine was more confident"; a large k would flatten
everything into a tie.

**Do not tune it against a handful of queries.** If it is wrong for real ones,
the useful output is the queries and what they returned, not a different
constant — see docs/specs/006-hybrid-retrieval.md's guardrail.
"""
from __future__ import annotations

from typing import Iterable, Sequence

DEFAULT_K = 60


def rrf(lists: Iterable[Sequence[str]], k: int = DEFAULT_K) -> list[tuple[str, float]]:
    """Fuse ranked id lists into one, highest score first.

    Each input is a sequence of ids in rank order, best first. Ids may appear in
    any subset of the lists — an id in only one list still scores, which is what
    makes fusion useful rather than an intersection: the whole point is that
    lexical and semantic search find *different* things.

    Ties break on the id, so a fused ordering is deterministic. Two results with
    identical scores are genuinely indistinguishable to this function, and
    letting dict order decide would make the same query return different orders
    across runs.
    """
    scores: dict[str, float] = {}
    for ranked in lists:
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def fuse_ids(lists: Iterable[Sequence[str]], limit: int | None = None,
             k: int = DEFAULT_K) -> list[str]:
    """:func:`rrf` without the scores, for callers that only need the order."""
    fused = [key for key, _ in rrf(lists, k=k)]
    return fused[:limit] if limit is not None else fused
